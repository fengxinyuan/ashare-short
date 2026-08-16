"""把整份新闻清单交给大模型，读出**传导链**：某件事影响哪个品种、通过什么机制。

**为什么加这一层。** 在它之前，舆情的唯一出路是 `llm_classifier.py`：每条快讯被
压成 `(板块, 方向, 事件)` 三个标签，再由规则层折算成板块分里封顶 ±12 的一项。
那条路解决的是「板块排序要不要动」，它做得很好，但它丢掉了新闻里**唯一不能被压成
标签的东西**——跨市场、跨品种的因果链。

逐条打标签答不了的，正是这类问题：

| 事件 | 传导 | 落到 A 股 |
|---|---|---|
| 中东冲突升级 | 原油供给风险溢价 | 石油石化、油运；航空反向 |
| 韩股 SK海力士涨停、美股存储指数 +17% | 存储周期共振 | 半导体、存储、PCB |
| 海外消费龙头财报超预期 | 全球消费需求读数 | 食品饮料、家电的情绪锚 |
| 美联储转鸽 | 无风险利率下行 | 成长股估值、红利股相对走弱 |

这些链条里，**原因和结果分属不同的新闻条目**，甚至分属不同的市场。按条打标签时，
「以色列袭击伊朗」会被归到「国防军工」，「原油涨 3%」会被归到「石油石化」，而
**把两者连起来的那句话不会出现在任何输出里**。连接需要跨条目的语义综合，所以它
该交给模型整段去读。

2026-08-02 实测当日语料 1205 条去重快讯的领域分布：地缘 113、国外炒作热点 99、
美股大科技 66、白宫与美国政治 64、韩股日股 52、美联储 36、美股指数盘面 27。
覆盖是够的——缺的一直是把它们连起来的那一步。

## 这一层能做什么、不能做什么

**能**：说出传导链（事件 → 机制 → 受影响品种 → 方向），点出主线与风险，给持仓
与持仓股单独标注属于它们自己的消息。

**不能**：产生任何新数字。约束方式和 `writer/llm.py` 完全一样——复用它的
`factcheck.py` 那道闸门，输出里每一个数字都必须能在**喂进去的标题原文**里找到。
模型可以引用「韩国综指涨 17.91%」，因为那句话就在语料里；它编不出「A 股半导体今日
大概率涨 3%」，因为 3% 无处可查，那一条会被整条丢弃。

**不进打分。** 这一层的产物是给人读的段落，不参与板块分、不参与选股、不写台账。
系统的数字仍然只从确定性层来。把它接进打分，等于让模型的叙述反过来改自己的输入。

## 关键词只用来分组，不用来判断

`_ROUTES` 决定一条快讯被放进提示词的哪一节，仅此而已。分错节最多让模型在「海外
市场」那段读到一条政策新闻，它照样能读懂；而让关键词去判断**含义**才是
`llm_classifier.py` 文档里说的那种错法。机械的部分给规则，语义的部分给模型。

## 失败契约

没有 key、网络挂了、返回不是 JSON、结论引用不到原文——任何一种都返回空读数加一条
note，调用方照常出简报。和这个包里其余模块同一条约定。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ashare_decision_support.factcheck import review_output
from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.news.dedup import parse_published

# 单次喂给模型的市场快讯上限。240 条标题约 8k token，一次装得下。再多就是当天的
# 长尾（同一条消息的第 4 个转发版本），对判断没有增量。
MAX_MARKET_HEADLINES = 240

# 每只持仓股最多带几条自己的消息。个股这段是要模型点名回答的，条数少而精
# 比堆满更有用。
MAX_SYMBOL_HEADLINES = 4

# 标题截断。和 `llm_classifier.TITLE_LIMIT` 不同——那边只做分类，60 字够；这边要
# 读传导链，「韩国首尔综指涨超17%，隔夜美股科技股大幅反弹。SK海力士涨幅扩大至24%」
# 砍到 60 字就把后半句（也就是传导的那一半）砍掉了。
TITLE_LIMIT = 110

READ_MAX_TOKENS = 4000
READ_TEMPERATURE = 0.2

# 分节路由。命中多条时按此处顺序取第一个——顺序即优先级。海外与地缘排在最前，
# 因为跨市场传导是这一层存在的理由；A 股自身的盘面快讯量最大但边际信息最少，
# 所以它是兜底节。
_ROUTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "海外市场",
        (
            "美股", "纳斯达克", "纳指", "道琼斯", "标普", "费城半导体", "罗素",
            "韩国", "首尔", "KOSPI", "三星", "海力士", "韩元",
            "日经", "东证", "日本央行", "欧洲央行", "富时", "DAX", "斯托克",
            "恒生", "港股", "中概", "新兴市场",
        ),
    ),
    (
        "美国政治与货币",
        (
            "白宫", "特朗普", "美国总统", "国会", "参议院", "众议院", "关税",
            "贝森特", "美联储", "鲍威尔", "FOMC", "点阵图", "美债", "非农",
            "美国政府", "商务部长", "行政令",
        ),
    ),
    (
        "海外公司",
        (
            "微软", "亚马逊", "谷歌", "Alphabet", "Meta", "苹果", "英伟达",
            "特斯拉", "博通", "AMD", "OpenAI", "甲骨文", "台积电", "美光",
            "闪迪", "西部数据", "希捷", "可口可乐", "耐克", "星巴克", "麦当劳",
            "礼来", "诺和诺德", "波音", "马斯克",
        ),
    ),
    (
        "地缘与安全",
        (
            "伊朗", "中东", "以色列", "俄乌", "俄罗斯", "乌克兰", "哈马斯",
            "台海", "南海", "制裁", "禁运", "军演", "冲突", "袭击",
        ),
    ),
    (
        "大宗与汇率",
        (
            "原油", "布伦特", "WTI", "黄金", "白银", "铜", "铝", "铁矿",
            "碳酸锂", "人民币", "美元指数", "比特币", "以太坊", "稳定币", "天然气",
        ),
    ),
    (
        "国内政策",
        (
            "国务院", "发改委", "央行", "财政部", "证监会", "政治局", "工信部",
            "商务部", "国资委", "印发", "出台", "国常会", "部署",
            "降准", "降息", "逆回购", "MLF", "LPR", "新闻联播",
        ),
    ),
    (
        "宏观数据",
        ("PMI", "CPI", "PPI", "社融", "GDP", "进出口", "失业率", "信贷", "外汇储备", "统计局"),
    ),
)
_FALLBACK_SECTION = "A股与产业"

# 每节的取样配额，见 `_pick_market_docs`。`_FALLBACK_SECTION` 不在这里——它拿剩余
# 额度，因为它条数最多而边际信息最少。
SECTION_QUOTAS: dict[str, int] = {
    "海外市场": 45,
    "美国政治与货币": 30,
    "海外公司": 30,
    "地缘与安全": 25,
    "大宗与汇率": 20,
    "国内政策": 35,
    "宏观数据": 15,
}

SYSTEM_PROMPT = """你是一名 A 股超短线交易员的盘前研究助理。你读到的是过去
72 小时、覆盖海外市场／美国政治／海外公司／地缘／大宗／国内政策的新闻标题清单。

你最重要的任务是找出**传导链**：某件事，通过什么机制，影响 A 股的哪个品种。
链条的两端经常分布在不同的新闻条目、甚至不同的市场里，把它们连起来正是你的价值。

传导链的例子（只是示范形式，不要照抄结论）：
- 中东冲突升级 →（原油供给风险溢价）→ 石油石化、油运偏多；航空、航运成本端偏空
- 韩股存储涨停 + 美股存储指数大涨 →（存储周期共振）→ A 股半导体、存储、PCB 偏多
- 海外消费龙头财报超预期 →（全球消费需求读数改善）→ 食品饮料、家电情绪偏多
- 美联储转鸽 →（无风险利率下行）→ 成长股估值偏多、红利股相对走弱

输出这几件事：
1. chains：传导链，最多 6 条，按重要性排序。每条给
   trigger（触发事件）、mechanism（传导机制，不超过 20 字）、
   targets（受影响的 A 股板块或品种，最多 3 个）、direction（bullish/bearish/mixed）。
2. mainlines：今天最可能被资金追的主线，最多 4 条。
3. risks：今天最需要提防的风险，最多 3 条。
4. holdings：针对【持仓】清单里的标的，说有没有属于它自己的消息。
   没有消息的票不要提，不要为了凑数硬编。
5. summary：不超过 80 字的一句话判断，像盘前研究结论，不要像清单复述。

铁律（违反会导致该条或整份被丢弃）：
- **不得出现清单里没有的数字。** 可以引用「韩国综指涨17.91%」，因为清单里有这句话；
  不得写「预计高开2%」「上涨概率70%」这类清单里查不到的数字。
- **不得给出买卖建议、目标价、概率。** 只说事实与传导逻辑。
- 每一条结论都必须给 evidence：**原样复制**所依据标题的前 12 个字符，可以给多条。
  凭空判断而引用不到原文的，不要输出。

输出**纯 JSON 对象**，不要 markdown 代码块，不要任何解释文字：
{"chains":[{"trigger":"...","mechanism":"...","targets":["石油石化"],
            "direction":"bullish","evidence":["标题前十二字"]}],
 "mainlines":[{"text":"...","evidence":["..."],"sectors":["..."]}],
 "risks":[{"text":"...","evidence":["..."]}],
 "holdings":[{"symbol":"600519.SH","text":"...","evidence":["..."]}],
 "summary":"..."}
没有内容的分节给空数组。"""

# 模型回抄的锚点长度。和 `llm_classifier.ANCHOR_CHARS` 同一套做法：只信可以拿原文
# 核对的东西，不信模型自报的序号。这里给 12 而不是 8，因为标题更长、前缀更容易撞车。
ANCHOR_CHARS = 12
MIN_ANCHOR_CHARS = 5

DIRECTIONS = ("bullish", "bearish", "mixed")

_NORM_PATTERN = re.compile(r"[\s　【】\[\]（）()《》「」“”\"':：,，.。!！?？、|]+")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# 纯文本型分节（chains 与 holdings 结构不同，单独处理）。
TEXT_SECTIONS = ("mainlines", "risks")


@dataclass(frozen=True)
class Chain:
    """一条传导链：事件 → 机制 → 品种 → 方向。"""

    trigger: str
    mechanism: str = ""
    targets: tuple[str, ...] = ()
    direction: str = "mixed"
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "mechanism": self.mechanism,
            "targets": list(self.targets),
            "direction": self.direction,
            "evidence": list(self.evidence),
        }

    def line(self) -> str:
        arrow = f"{self.trigger}"
        if self.mechanism:
            arrow += f" →（{self.mechanism}）"
        if self.targets:
            arrow += f"→ {'、'.join(self.targets)}"
        label = {"bullish": "偏多", "bearish": "偏空", "mixed": "多空分歧"}.get(
            self.direction, self.direction
        )
        return f"{arrow}　**{label}**"


@dataclass(frozen=True)
class ReadItem:
    """一条结论，连同它引用的原文标题。"""

    text: str
    evidence: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    symbol: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": self.text, "evidence": list(self.evidence)}
        if self.sectors:
            payload["sectors"] = list(self.sectors)
        if self.symbol:
            payload["symbol"] = self.symbol
        return payload


@dataclass
class DailyRead:
    """一次盘前舆情通读的结果。`ok=False` 时其余字段为空，调用方照常出简报。"""

    ok: bool = False
    summary: str = ""
    chains: list[Chain] = field(default_factory=list)
    mainlines: list[ReadItem] = field(default_factory=list)
    risks: list[ReadItem] = field(default_factory=list)
    holdings: list[ReadItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    headline_count: int = 0
    section_counts: dict[str, int] = field(default_factory=dict)

    @property
    def item_count(self) -> int:
        return (
            len(self.chains)
            + sum(len(getattr(self, name)) for name in TEXT_SECTIONS)
            + len(self.holdings)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "headline_count": self.headline_count,
            "section_counts": dict(self.section_counts),
            "chains": [item.to_dict() for item in self.chains],
            **{name: [i.to_dict() for i in getattr(self, name)] for name in TEXT_SECTIONS},
            "holdings": [item.to_dict() for item in self.holdings],
            "notes": list(self.notes),
        }

    def markdown(self) -> str:
        """给简报用的段落。空读数返回空串，调用方直接跳过这一节。"""
        if not self.ok or not self.item_count:
            return ""
        blocks: list[str] = []
        if self.summary:
            blocks.append("## 舆情判断")
            blocks.append(f"> {self.summary}")
        if self.chains:
            lines = ["## 传导链"]
            for chain in self.chains:
                lines.append(f"- {chain.line()}")
                if chain.evidence:
                    lines.append(f"  - 依据：{' / '.join(chain.evidence)}")
            blocks.append("\n".join(lines))
        for title, items in (("今天先盯什么", self.mainlines), ("需要防什么", self.risks)):
            if not items:
                continue
            lines = [f"## {title}"]
            for item in items:
                tag = f"（{'、'.join(item.sectors)}）" if item.sectors else ""
                lines.append(f"- {item.text}{tag}")
                if item.evidence:
                    lines.append(f"  - 依据：{' / '.join(item.evidence)}")
            blocks.append("\n".join(lines))
        if self.holdings:
            lines = ["## 持仓"]
            for item in self.holdings:
                lines.append(f"- **{item.symbol or ''}** {item.text}")
                if item.evidence:
                    lines.append(f"  - 依据：{' / '.join(item.evidence)}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


def build_client(provider: str | None = None, model: str | None = None) -> Any:
    """按**通读**任务调好参数的客户端。

    这个工厂此前不存在，于是 `jobs/morning.py` 借了分类器的 `build_client()`——
    上面那两个常量因此一次都没生效过。两个任务对模型的要求本来就不同：分类要
    的是照抄清单里的标签（温度 0.1），通读要的是把跨条目的因果链说出来，稍微
    松一点更合适。
    """
    from ashare_decision_support.writer.llm import DEFAULT_PROVIDER, LlmClient

    return LlmClient(
        provider=provider or DEFAULT_PROVIDER,
        model=model,
        max_tokens=READ_MAX_TOKENS,
        temperature=READ_TEMPERATURE,
    )


class DailyNewsReader:
    """把语料交给模型通读一遍。构造与调用都不抛异常。"""

    name = "daily-news-reader"

    def __init__(self, client: Any, cache: Any = None) -> None:
        self.client = client
        self.cache = cache

    @property
    def configured(self) -> bool:
        return bool(getattr(self.client, "configured", False))

    def read(
        self,
        docs: list[NewsDoc],
        trade_date: date | None = None,
        symbol_docs: dict[str, list[NewsDoc]] | None = None,
        names: dict[str, str | None] | None = None,
    ) -> DailyRead:
        if not docs:
            return DailyRead(notes=["没有可读的舆情，本次不出通读"])
        if not self.configured:
            return DailyRead(notes=["未配置 LLM key，本次不出舆情通读"])

        cached = self._read_cache(trade_date)
        if cached is not None:
            cached.notes.append("舆情通读命中当日缓存，未重复调用模型")
            return cached

        picked, counts = _pick_market_docs(docs)
        prompt, corpus = _build_prompt(picked, symbol_docs or {}, names or {})

        try:
            raw = self.client.chat(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ]
            )
        except Exception as exc:
            return DailyRead(notes=[f"舆情通读调用失败（{type(exc).__name__}），本次跳过"])

        payload = _parse_json_object(raw or "")
        if not payload:
            return DailyRead(notes=["舆情通读返回的不是 JSON，本次丢弃"])

        result = _harvest(payload, picked, symbol_docs or {})
        result.headline_count = len(picked)
        result.section_counts = counts
        if not result.item_count:
            result.ok = False
            result.notes.append("舆情通读没有一条结论能引用到原文，整份丢弃")
            return result

        # 闸门：数字要能在喂进去的标题里找到，措辞不得越过确定性层。口径与写稿层
        # 一致（`factcheck.review_output`）。措辞那一条数字闸门看不见——「大概率
        # 反弹」不带任何数字。这里不判立场：通读读的是标题，手上没有情绪周期那把尺子。
        review = review_output(_all_text(result), corpus)
        if not review.ok:
            return DailyRead(
                notes=[f"舆情通读{review.reason()}，整份丢弃（这一层不允许产生新数字）"]
            )

        result.ok = True
        result.notes.append(
            f"舆情通读：{len(picked)} 条标题（"
            + "、".join(f"{name} {n}" for name, n in counts.items() if n)
            + f"），产出传导链 {len(result.chains)} 条、结论 {result.item_count} 条，"
            "全部附带原文依据（这一层只读不算，不参与打分）"
        )
        self._write_cache(trade_date, result)
        return result

    # ---- 缓存 ------------------------------------------------------------

    def _read_cache(self, trade_date: date | None) -> DailyRead | None:
        if self.cache is None or trade_date is None:
            return None
        try:
            payload = self.cache.read(trade_date, "daily_news_read")
        except Exception:
            return None
        if not isinstance(payload, dict) or not payload.get("ok"):
            return None
        return DailyRead(
            ok=True,
            summary=str(payload.get("summary") or ""),
            headline_count=int(payload.get("headline_count") or 0),
            section_counts=dict(payload.get("section_counts") or {}),
            chains=[
                Chain(
                    trigger=str(row.get("trigger") or ""),
                    mechanism=str(row.get("mechanism") or ""),
                    targets=tuple(row.get("targets") or ()),
                    direction=str(row.get("direction") or "mixed"),
                    evidence=tuple(row.get("evidence") or ()),
                )
                for row in (payload.get("chains") or [])
                if isinstance(row, dict) and row.get("trigger")
            ],
            **{
                name: [
                    ReadItem(
                        text=str(row.get("text") or ""),
                        evidence=tuple(row.get("evidence") or ()),
                        sectors=tuple(row.get("sectors") or ()),
                        symbol=row.get("symbol"),
                    )
                    for row in (payload.get(name) or [])
                    if isinstance(row, dict) and row.get("text")
                ]
                for name in (*TEXT_SECTIONS, "holdings")
            },
        )

    def _write_cache(self, trade_date: date | None, result: DailyRead) -> None:
        if self.cache is None or trade_date is None:
            return
        try:
            self.cache.write(trade_date, "daily_news_read", result.to_dict())
        except Exception:
            return


# ---- 提示词组装 ------------------------------------------------------------


def route_of(doc: NewsDoc) -> str:
    """这条快讯放进哪一节。只影响排版，不影响含义——见模块开头。"""
    text = doc.text
    for section, keywords in _ROUTES:
        if any(word in text for word in keywords):
            return section
    return _FALLBACK_SECTION


def _pick_market_docs(docs: list[NewsDoc]) -> tuple[list[NewsDoc], dict[str, int]]:
    """按分节配额取样，每节内部再按新鲜度排。返回 (选中的文档, 各节条数)。

    **不能只按新鲜度取前 N 条。** 实测 2026-08-02 语料 1205 条里 A 股自身的盘面
    快讯占了绝大多数，纯按时间取前 240 条会把海外那几节挤到个位数——而当天最重要的
    一条传导链（韩国综指单日 +17.91%、SK海力士首尔涨停、美股存储指数 +17%）发生在
    07-31，正好落在窗外。**这一层存在的理由就是读跨市场传导，让它读不到海外等于白做。**

    所以每一节都有配额：小众但关键的节（宏观数据往往只有几条）全要，A 股那节量最大
    而边际信息最少，用它填剩下的额度。
    """
    def has_stamp(doc: NewsDoc) -> bool:
        return parse_published(doc.published_at) is not None

    def sort_key(doc: NewsDoc) -> tuple[int, str]:
        stamp = parse_published(doc.published_at)
        # 无时间戳的沉到本节末尾，但不丢弃——见 `dedup.py` 对缺时间戳的处理。
        return (0, stamp.isoformat()) if stamp else (1, "")

    buckets: dict[str, list[NewsDoc]] = {}
    for doc in docs:
        buckets.setdefault(route_of(doc), []).append(doc)
    for items in buckets.values():
        items.sort(key=lambda d: sort_key(d)[1], reverse=True)
        items.sort(key=lambda d: 0 if has_stamp(d) else 1)

    picked: list[NewsDoc] = []
    counts: dict[str, int] = {}
    for section, quota in SECTION_QUOTAS.items():
        taken = buckets.get(section, [])[:quota]
        counts[section] = len(taken)
        picked.extend(taken)

    remaining = max(0, MAX_MARKET_HEADLINES - len(picked))
    taken = buckets.get(_FALLBACK_SECTION, [])[:remaining]
    counts[_FALLBACK_SECTION] = len(taken)
    picked.extend(taken)
    return picked[:MAX_MARKET_HEADLINES], counts


def _build_prompt(
    docs: list[NewsDoc],
    symbol_docs: dict[str, list[NewsDoc]],
    names: dict[str, str | None],
) -> tuple[str, str]:
    """返回 (提示词, 供数字闸门比对的原文语料)。"""
    buckets: dict[str, list[str]] = {}
    for doc in docs:
        buckets.setdefault(route_of(doc), []).append(doc.title[:TITLE_LIMIT])

    parts: list[str] = []
    for section in [name for name, _ in _ROUTES] + [_FALLBACK_SECTION]:
        titles = buckets.get(section)
        if not titles:
            continue
        listing = "\n".join(f"- {title}" for title in titles)
        parts.append(f"【{section}】共 {len(titles)} 条\n{listing}")

    if symbol_docs:
        lines: list[str] = []
        for symbol, items in symbol_docs.items():
            if not items:
                continue
            label = names.get(symbol) or ""
            heads = "；".join(doc.title[:TITLE_LIMIT] for doc in items[:MAX_SYMBOL_HEADLINES])
            lines.append(f"- {symbol} {label}：{heads}")
        if lines:
            parts.append("【持仓】\n" + "\n".join(lines))

    body = "\n\n".join(parts)
    return (
        f"{body}\n\n按 system 里的格式输出 JSON。evidence 必须原样复制标题前 12 个字符。",
        body,
    )


# ---- 解析与核对 ------------------------------------------------------------


def _harvest(
    payload: dict,
    docs: list[NewsDoc],
    symbol_docs: dict[str, list[NewsDoc]],
) -> DailyRead:
    """把模型的 JSON 变成结论，逐条核对引用。引用不到原文的整条丢弃。"""
    corpus = [_norm(doc.title) for doc in docs]
    corpus += [_norm(doc.title) for items in symbol_docs.values() for doc in items]
    known_symbols = set(symbol_docs)

    result = DailyRead()
    dropped = 0

    def cited(row: dict) -> tuple[str, ...]:
        return tuple(
            item for item in _as_list(row.get("evidence"))
            if isinstance(item, str) and _cites(item, corpus)
        )[:3]

    def build(row: Any, allow_symbol: bool = False) -> ReadItem | None:
        nonlocal dropped
        if not isinstance(row, dict):
            return None
        text = str(row.get("text") or "").strip()
        if not text:
            return None
        evidence = cited(row)
        if not evidence:
            # 引用不到原文的判断没法核对，和 `llm_classifier` 丢掉对不上锚点的
            # 那一条同一个理由：宁可少一条结论，不要一条查不到出处的结论。
            dropped += 1
            return None
        symbol = None
        if allow_symbol:
            symbol = row.get("symbol") if row.get("symbol") in known_symbols else None
            if symbol is None:
                dropped += 1
                return None
        return ReadItem(
            text=text,
            evidence=evidence,
            sectors=tuple(
                str(name) for name in _as_list(row.get("sectors")) if isinstance(name, str)
            )[:3],
            symbol=symbol,
        )

    chains: list[Chain] = []
    for row in _as_list(payload.get("chains")):
        if not isinstance(row, dict):
            continue
        trigger = str(row.get("trigger") or "").strip()
        if not trigger:
            continue
        evidence = cited(row)
        if not evidence:
            dropped += 1
            continue
        direction = row.get("direction")
        chains.append(
            Chain(
                trigger=trigger,
                mechanism=str(row.get("mechanism") or "").strip()[:24],
                targets=tuple(
                    str(t) for t in _as_list(row.get("targets")) if isinstance(t, str)
                )[:3],
                direction=direction if direction in DIRECTIONS else "mixed",
                evidence=evidence,
            )
        )
    result.chains = chains

    for name in TEXT_SECTIONS:
        items = [build(row) for row in _as_list(payload.get(name))]
        setattr(result, name, [item for item in items if item is not None])
    result.holdings = [
        item
        for item in (build(row, allow_symbol=True) for row in _as_list(payload.get("holdings")))
        if item is not None
    ]

    summary = payload.get("summary")
    result.summary = summary.strip()[:120] if isinstance(summary, str) else ""
    if dropped:
        result.notes.append(f"舆情通读丢弃 {dropped} 条引用不到原文的结论")
    return result


def _cites(anchor: str, corpus: list[str]) -> bool:
    """模型回抄的前缀能不能在原文里找到。"""
    needle = _norm(anchor)[:ANCHOR_CHARS]
    if len(needle) < MIN_ANCHOR_CHARS:
        return False
    return any(title.startswith(needle) or needle in title for title in corpus)


def _all_text(result: DailyRead) -> str:
    parts = [result.summary]
    parts.extend(f"{c.trigger} {c.mechanism} {' '.join(c.targets)}" for c in result.chains)
    for name in (*TEXT_SECTIONS, "holdings"):
        parts.extend(item.text for item in getattr(result, name))
    return " ".join(part for part in parts if part)


def _parse_json_object(text: str) -> dict:
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return {}
        candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _norm(text: str) -> str:
    return _NORM_PATTERN.sub("", text or "")


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []
