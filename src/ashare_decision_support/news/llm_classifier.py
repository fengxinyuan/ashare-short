"""大模型舆情归因：LLM 只出**标签**，不出任何数字。

**为什么需要它。** 关键词匹配的板块归因会明确地错。`sectors/keywords.py` 里
「光伏」挂在电力设备下，但一条「光伏组件出口退税取消」同时打击基础化工（硅料）与
机械设备（光伏设备），关键词表里没有这层联系。反过来，「宁德时代与某车企签约」
会被「汽车」和「电力设备」双双命中，其中一个是错的。词表能加词，加不出语义。

**它在架构里的位置没有变。** 系统的铁律是「所有数字都在确定性层产生」。分类器
输出的是三样东西：这条消息属于哪几个板块、方向是偏多还是偏空、属于哪类事件——
全是**标签**。标签到分数的映射仍然是确定性层里那套固定权重（板块级封顶 ±12、
样本量不足 8 条按比例折权），LLM 碰不到。

**失败就退回词典。** 网络挂了、返回不是 JSON、板块名不在给定清单里——任何一种
情况都退回 `LexiconSentiment`，并在简报里写明这一轮用的是哪条路径。分类结果按日
缓存，重跑当天不重复计费。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.fanout import gather
from ashare_decision_support.llmreply import anchor_text, parse_array
from ashare_decision_support.news.base import NewsDoc, SentimentAnalyzer, SentimentScore
from ashare_decision_support.news.dedup import dedup_key

DIRECTION_BULLISH = "bullish"
DIRECTION_BEARISH = "bearish"
DIRECTION_NEUTRAL = "neutral"
DIRECTIONS = (DIRECTION_BULLISH, DIRECTION_BEARISH, DIRECTION_NEUTRAL)

# 一次请求塞多少条标题。太小则请求数暴涨，太大则输出会被 max_tokens 截断——
# 实测 30 条的返回已达 2638 字符，贴着默认 2000 token 上限，6/7 批因此整批作废。
# 20 条 + 下面那个更大的 max_tokens 是实测能稳定跑完的组合。
BATCH_SIZE = 20

# 分类任务的客户端参数。温度压到 0.1：这是分类，不是写作，不需要多样性。
CLASSIFIER_MAX_TOKENS = 4000
CLASSIFIER_TEMPERATURE = 0.1

# 并发请求数。四家供应商都在 0.5~0.8 秒量级，4 路足够，再高只是给限流找事。
MAX_WORKERS = 4

# 单条消息最多归到几个板块。不设限时模型会把一条宏观新闻挂到十几个板块上，
# 于是每个板块都"有舆情"，等于没有归因。
MAX_SECTORS_PER_DOC = 3

# 标题截断长度。分类靠标题就够，正文只会撑爆 token 且带来更多幻觉空间。
TITLE_LIMIT = 60

# 让模型回抄多少个字符。给 8，但**不要求它正好给 8**——模型经常连标点一起抄、
# 或者只抄 5 个字。所以对齐用「归一化后前缀包含」而不是定长相等：抄多抄少都能对上，
# 抄的是别条才对不上。
ANCHOR_CHARS = 8

# 锚点归一化后的最短长度。太短会撞车（多条都以"央行"开头），宁可丢这一条。
MIN_ANCHOR_CHARS = 4

# 事件类型清单。**必须和 SYSTEM_PROMPT 里给模型的那份逐字一致**，因为下面要拿它
# 做校验：清单外的值一律丢弃。
#
# 校验这一项的理由和校验板块名完全相同。2026-08-02 实测 314 条归因里有 17 条
# （5.4%）带着清单外的自创类型——销量 10、事件 3、高管变动 / 组织调整 / 产品创新 /
# 品牌建设各 1——而它们会原样印进简报的「关键词：技术突破、品牌建设、需求变化」。
# 板块名和方向早就在校验了，事件类型漏了，于是它成了幻觉唯一还能走通的那条路。
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "政策",
        "业绩",
        "订单",
        "产能",
        "价格",
        "资本运作",
        "监管处罚",
        "股东行为",
        "技术突破",
        "需求变化",
        "其他",
    }
)

SYSTEM_PROMPT = """你是 A 股新闻的分类器。你的输出被程序解析，不是给人读的。

给你一份【板块清单】和一批【新闻标题】，对每条标题判断三件事：
1. sectors：它实质利好或利空哪些板块。**只能从【板块清单】里原样挑**，最多 3 个。
   判断不了、或只是宏观消息不针对具体板块，就给空数组 []。
2. direction：对这些板块是 bullish（偏多）、bearish（偏空）还是 neutral（中性）。
   只是陈述事实、没有方向含义的，一律 neutral。
3. event：事件类型，从这些里选一个：政策、业绩、订单、产能、价格、资本运作、
   监管处罚、股东行为、技术突破、需求变化、其他。
4. why：**不超过 12 个字**，说清这条消息**通过什么机制**影响这些板块。
   sectors 为空时 why 也给空字符串。

关于 sectors 的补充要求（这一条最容易做错）：
- 只有当消息对 A 股公司的经营或估值存在**明确传导链**时才归板块。
- 行业产品价格、原材料价格、库存、供给收缩、停产检修、运价、能源成本等，
  即使没有点名上市公司，只要能明确影响某个行业利润，也可以归到对应板块；
  why 要写清楚传导机制，例如「纸浆下跌改善纸企成本」。
- 海外灾害、国际时政、体育娱乐、纯宏观数据等，若无法落到具体行业传导链，
  一律给空数组 []，不要硬找关联。宁可漏，不要凑。

严格要求：
- 输出**纯 JSON 数组**，不要 markdown 代码块，不要任何解释文字。
- 数组长度必须等于输入标题数，顺序一一对应。
- 每个元素必须包含 "t"：**原样复制该条标题的前 8 个字符**，用于对齐，不得改写。
- 每个元素形如
  {"i": 序号, "t": "标题前八字", "sectors": ["板块名"], "direction": "bullish",
   "event": "政策", "why": "国产替代加速"}
- 板块名必须与【板块清单】中的字符串完全一致，不得改写、不得自创。
- 不要输出任何数字化的判断（涨跌幅、概率、目标价）——那不是你的工作。"""


def build_client(provider: str | None = None, model: str | None = None) -> Any:
    """按分类任务调好参数的客户端。调用方不必知道该把 max_tokens 设成多少。"""
    from ashare_decision_support.writer.llm import DEFAULT_PROVIDER, LlmClient

    return LlmClient(
        provider=provider or DEFAULT_PROVIDER,
        model=model,
        max_tokens=CLASSIFIER_MAX_TOKENS,
        temperature=CLASSIFIER_TEMPERATURE,
    )


@dataclass(frozen=True)
class DocLabel:
    """一条消息的分类结果。三个字段都是标签，没有一个是数字。"""

    sectors: tuple[str, ...] = ()
    direction: str = DIRECTION_NEUTRAL
    event: str | None = None
    # 模型自述的传导机制。它会被原样印进简报——归因错了，读的人一眼就能看出来，
    # 而不是只看到一条不相干的标题挂在板块下面。
    why: str | None = None

    @property
    def polarity(self) -> float:
        """方向标签到极性的**固定**映射。不是模型给的强度，是常量。"""
        if self.direction == DIRECTION_BULLISH:
            return 1.0
        if self.direction == DIRECTION_BEARISH:
            return -1.0
        return 0.0

    @property
    def is_directional(self) -> bool:
        return self.direction in (DIRECTION_BULLISH, DIRECTION_BEARISH)


@dataclass
class ClassifyResult:
    labels: dict[str, DocLabel] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    calls: int = 0
    ok: bool = False

    def attribution(self) -> dict[str, tuple[str, ...]]:
        """全部标签，**包括空归属**。

        只返回非空的曾经是个 bug：模型明确判定「这条不属于任何板块」的条目会从字典
        里消失，于是下游 `get()` 拿到 None，退回关键词重新归因一遍——模型说「不」
        变成了没说。空元组和键不存在必须是两件事。
        """
        return {key: label.sectors for key, label in self.labels.items()}


class LlmNewsClassifier:
    """把一批快讯分类到板块与方向。失败即退回，不抛异常。"""

    name = "llm-news-classifier"

    def __init__(
        self,
        client: Any,
        batch_size: int = BATCH_SIZE,
        max_workers: int = MAX_WORKERS,
        cache: Any = None,
    ) -> None:
        self.client = client
        self.batch_size = max(1, batch_size)
        self.max_workers = max(1, max_workers)
        self.cache = cache

    @property
    def configured(self) -> bool:
        return bool(getattr(self.client, "configured", False))

    def classify(
        self,
        docs: list[NewsDoc],
        sectors: list[str],
        trade_date: Any = None,
    ) -> ClassifyResult:
        if not docs or not sectors:
            return ClassifyResult(notes=[], ok=False)
        if not self.configured:
            return ClassifyResult(
                notes=["未配置 LLM key，舆情归因走关键词词典"], ok=False
            )

        cached = self._read_cache(trade_date)
        if cached is not None:
            return ClassifyResult(
                labels=cached,
                notes=[f"舆情归因命中当日缓存（{len(cached)} 条），未重复调用模型"],
                ok=True,
            )

        # 去重后再分类：同一条消息被两个源转发时只该付一次钱。
        unique: dict[str, NewsDoc] = {}
        for doc in docs:
            key = dedup_key(doc)
            if key and key not in unique:
                unique[key] = doc

        items = list(unique.items())
        batches = [
            items[index : index + self.batch_size]
            for index in range(0, len(items), self.batch_size)
        ]

        allowed = set(sectors)
        done, errors = gather(
            batches,
            lambda batch: self._classify_batch(batch, sectors, allowed),
            max_workers=self.max_workers,
        )

        labels: dict[str, DocLabel] = {}
        for _batch, mapping in done:
            labels.update(mapping)

        # 「没打通」和「答了但没法用」分成两条 note。合成一句「N 批失败」的话，
        # 排查时两边都得翻一遍：前者查网络与配额，后者查提示词与 max_tokens。
        voided = sum(1 for _batch, mapping in done if not mapping)

        notes: list[str] = []
        if errors:
            kinds = "、".join(sorted({type(exc).__name__ for _, exc in errors}))
            notes.append(
                f"舆情归因 {len(errors)}/{len(batches)} 批调用失败（{kinds}），"
                "这部分退回关键词词典"
            )
        if voided:
            notes.append(
                f"舆情归因 {voided}/{len(batches)} 批输出无法对齐或解析，整批作废，"
                "这部分退回关键词词典"
            )
        if labels:
            directional = sum(1 for label in labels.values() if label.is_directional)
            attributed = sum(1 for label in labels.values() if label.sectors)
            notes.append(
                f"大模型舆情归因：{len(labels)} 条去重快讯，{attributed} 条归到板块、"
                f"{directional} 条有明确方向（模型只出标签，分值仍由规则层给）"
            )
            self._write_cache(trade_date, labels)

        return ClassifyResult(
            labels=labels, notes=notes, calls=len(batches), ok=bool(labels)
        )

    # ---- internals -------------------------------------------------------

    def _classify_batch(
        self,
        batch: list[tuple[str, NewsDoc]],
        sectors: list[str],
        allowed: set[str],
    ) -> dict[str, DocLabel]:
        """这一批的标签。**调用失败直接抛**，由 `gather` 交还给上面写 note；
        返回空字典是另一回事——模型答了，但答的东西对不上，整批作废。"""
        listing = "\n".join(
            f"{index}. {doc.title[:TITLE_LIMIT]}" for index, (_, doc) in enumerate(batch)
        )
        user = (
            f"【板块清单】\n{'、'.join(sectors)}\n\n"
            f"【新闻标题】共 {len(batch)} 条\n{listing}\n\n"
            f"输出长度为 {len(batch)} 的 JSON 数组。"
        )
        raw = self.client.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ]
        )

        rows = parse_array(raw or "")
        if not rows:
            return {}

        # 对齐靠**回抄的标题前缀**，不靠模型自报的序号。
        #
        # 实测：模型的 `i` 会整体偏一位，于是「法国森林火灾」拿到了「动画电影票房
        # 超预期」的归因，整批标签错行——而且在加上 `why` 之前完全看不出来，每条
        # 都"看着挺合理"。序号是模型说的，标题前缀是可以拿原文核对的，只信后者。
        normalized = [(anchor_text(doc.title), key) for key, doc in batch]

        mapping: dict[str, DocLabel] = {}
        unmatched = 0
        for _position, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            key = _match_anchor(row.get("t"), normalized)
            if key is None:
                # 对不上就丢这一条。硬按位置贴上去，就是把错行的标签当成对的。
                unmatched += 1
                continue
            # 板块名必须原样命中清单。模型自创的板块直接丢——把它映射到"最像的"
            # 那个，等于让幻觉悄悄进入归因。
            picked = tuple(
                name for name in _as_list(row.get("sectors"))
                if isinstance(name, str) and name in allowed
            )[:MAX_SECTORS_PER_DOC]
            direction = row.get("direction")
            why = row.get("why")
            why = why.strip()[:20] if isinstance(why, str) and why.strip() else None
            # 归了板块却说不出机制的，按未归因处理：说不出理由的关联通常就是硬凑的。
            if picked and why is None:
                picked = ()
            # 事件类型同样必须原样命中清单，理由见 `EVENT_TYPES`。清单外的值置空
            # 而不是丢掉整条——归属和方向仍然是可用的，只有这个标签不可信。
            event = row.get("event")
            event = event if isinstance(event, str) and event in EVENT_TYPES else None
            mapping[key] = DocLabel(
                sectors=picked,
                direction=direction if direction in DIRECTIONS else DIRECTION_NEUTRAL,
                event=event,
                why=why,
            )

        # 半数以上对不上说明模型没按格式回抄，这批的对齐整体不可信，整批作废。
        if unmatched * 2 >= len(rows):
            return {}
        return mapping

    def _read_cache(self, trade_date: Any) -> dict[str, DocLabel] | None:
        if self.cache is None or trade_date is None:
            return None
        try:
            payload = self.cache.read(trade_date, "llm_news_labels")
        except Exception:
            return None
        if not isinstance(payload, list):
            return None
        labels: dict[str, DocLabel] = {}
        for row in payload:
            if not isinstance(row, dict) or not row.get("key"):
                continue
            # 缓存也要过一遍清单：昨天写进去的自创事件类型不该因为走了缓存这条路
            # 就绕过校验。
            event = row.get("event")
            labels[row["key"]] = DocLabel(
                sectors=tuple(row.get("sectors") or ()),
                direction=row.get("direction") or DIRECTION_NEUTRAL,
                event=event if isinstance(event, str) and event in EVENT_TYPES else None,
                why=row.get("why"),
            )
        return labels or None

    def _write_cache(self, trade_date: Any, labels: dict[str, DocLabel]) -> None:
        if self.cache is None or trade_date is None:
            return
        try:
            self.cache.write(
                trade_date,
                "llm_news_labels",
                [
                    {
                        "key": key,
                        "sectors": list(label.sectors),
                        "direction": label.direction,
                        "event": label.event,
                        "why": label.why,
                    }
                    for key, label in labels.items()
                ],
            )
        except Exception:
            return


class LabelBackedSentiment(SentimentAnalyzer):
    """用分类标签给舆情打分，标签缺失的那条退回词典。

    极性来自 `DocLabel.polarity` 这个常量映射；强度固定为 1.0（有方向）或 0.0
    （中性）。**模型不提供强度**——让它输出「有多强」就是让它输出数字，那是这层
    架构不允许的事。所以证据强弱只由条数体现，再由上游的样本量斜坡与封顶控制。
    """

    name = "llm-labelled"

    def __init__(self, labels: dict[str, DocLabel], fallback: SentimentAnalyzer) -> None:
        self.labels = labels
        self.fallback = fallback

    def score_text(self, text: str) -> SentimentScore:
        return self.fallback.score_text(text)

    def score_docs(self, docs: list[NewsDoc]) -> SentimentScore:
        if not docs:
            return SentimentScore(polarity=0.0, intensity=0.0)

        labelled: list[tuple[DocLabel, NewsDoc]] = []
        unlabelled: list[NewsDoc] = []
        for doc in docs:
            label = self.labels.get(dedup_key(doc))
            if label is None:
                unlabelled.append(doc)
            else:
                labelled.append((label, doc))

        if not labelled:
            return self.fallback.score_docs(docs)

        weight_total = 0.0
        weighted_polarity = 0.0
        bullish: list[str] = []
        bearish: list[str] = []
        for label, doc in labelled:
            if not label.is_directional:
                continue
            weight = max(0.0, doc.weight)
            if weight <= 0:
                continue
            weight_total += weight
            weighted_polarity += label.polarity * weight
            tag = label.event or ("利好" if label.polarity > 0 else "利空")
            (bullish if label.polarity > 0 else bearish).append(tag)

        # 有标签但没有一条带方向 → 词典也不会更有把握，直接报中性。
        if weight_total <= 0:
            return SentimentScore(polarity=0.0, intensity=0.0)

        # 未分类的那部分仍走词典，两边按各自的权重合并。
        if unlabelled:
            residual = self.fallback.score_docs(unlabelled)
            residual_weight = residual.intensity * len(unlabelled)
            if residual_weight > 0:
                weighted_polarity += residual.polarity * residual_weight
                weight_total += residual_weight
                bullish.extend(residual.bullish_hits)
                bearish.extend(residual.bearish_hits)

        return SentimentScore(
            polarity=max(-1.0, min(1.0, weighted_polarity / weight_total)),
            intensity=min(1.0, weight_total / max(len(docs), 1)),
            bullish_hits=tuple(dict.fromkeys(bullish))[:6],
            bearish_hits=tuple(dict.fromkeys(bearish))[:6],
        )


# ---- parsing -------------------------------------------------------------

def _match_anchor(anchor: Any, normalized: list[tuple[str, str]]) -> str | None:
    """用模型回抄的标题前缀找回它说的是哪一条。

    唯一命中才算数：两条标题共享同一前缀时无法判定是哪条，丢掉比猜错好。
    """
    if not isinstance(anchor, str) or not anchor.strip():
        return None
    needle = anchor_text(anchor)[:ANCHOR_CHARS]
    if len(needle) < MIN_ANCHOR_CHARS:
        return None
    hits = [key for title, key in normalized if title.startswith(needle)]
    return hits[0] if len(hits) == 1 else None


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        return [value]
    return []
