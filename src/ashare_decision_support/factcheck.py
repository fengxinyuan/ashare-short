"""输出闸门的共用件：文字里的数字要能追溯，措辞与立场不得越过确定性层。

**放在包根**，理由同 `fanout.py` 与 `numeric.py`：它约束的不是写作，也不是裁决，
是**一件横跨三层的事**——凡是让模型出文字的地方，文字里的每个数字都要能在喂进去
的事实里找到出处。今天有三道这样的闸门：

| 位置 | 用在哪 | 严到什么程度 |
|---|---|---|
| `factcheck.py:unverified_numbers` | 简报改写、深挖分步、多空辩论、舆情通读、问答 | 允许「按你写的精度四舍五入后相等」，并认 万/亿/% 后缀 |
| `advice/adjudicate.py:offending_numbers` | 价位裁决 | 只认**数值相等** |
| `advice/external.py:_untraceable_numbers` | 外部裁决包 | 只认 token 原样出现 |

**三种严格程度是有意的，不该统一。** 散文改写本来就会重排数字，把
12198154.379999977 写成 12198154.38 是正常表述；同一件事发生在挂单价上，
22.9512 写成 22.95 就是另一个价了。所以另外两处的判定留在各自那层，只有散文那一档
在本模块——它的调用方有五个，放在 `writer/llm.py` 里等于让「写简报」这一层替另外
四条链路保管闸门。

收口之前 `ORDINAL_CEILING = 20` 有两份定义（`writer/llm.py` 和
`advice/adjudicate.py`），而 `advice/external.py` 从**两个不同模块**分别拿这道闸门
的两个常量。两份 20 漂成 20 和 15 不会有任何测试变红，只会让同一个「第 18 档」
在简报里放行、在裁决里作废。

## 数字之外的两条：`prose_violations`

数字这一条是全仓唯一被真正执行的输出约束。而六份提示词里还写着另外两类要求，
**一条都没有人查**：

| 提示词里写的 | 写在哪 | 此前谁在查 |
|---|---|---|
| 不得给概率 / 胜率 / 预期收益率 | `writer/llm.py` 第 4 条、`ask/runner.py` 第 4 条、`chat/session.py` 第 4 条、`agents/debate.py` 第 3 条、`advice/adjudicate.py` 第 4 条 | **没有人** |
| 不得改变结论 | `writer/llm.py` 第 1 条 | **没有人** |

「不得产出概率/胜率/预期收益率」不是措辞偏好，是 `DECISIONS.md` §七 的一条固化
决定（没有拟合模型能支撑它们），而且它同时印在给用户看的纪律段里。一条写在提示词
里、没人执行的约束，和没有这条约束是一回事——这套仓库对文档里的口径已经反复用
`scripts/` 下的审计脚本得出过同一个结论，提示词不该是例外。

所以这里补两条，判定全部留在确定性层已经算出来的读数上，闸门自己**不看行情**：

- **措辞**：出现 `CLAIM_MARKERS` 里的词，且不是被否定着说的（「系统不产出胜率」
  是纪律段的原话，必须放行）。
- **立场**：`OutputGuard` 带着情绪周期给出的仓位上限与当前时段。上限为 0 那一档
  （冰点）说的是**今天不出手**，此时任何买入措辞都是在改结论；上限已经给出时，
  「满仓 / 重仓 / 梭哈」同样是在改结论。时段不是盘中时，「立即买入」这类即时动作
  指令没有可执行的对象。

三条闸门的处置口径一致：**整段丢弃，回落到确定性层的输出**（同 `agents/debate.py`
的注释——一段半可信的论证比没有更糟，读的人分不清该信哪一半）。

## 为什么有 `review_output` 这个单一入口

补上措辞与立场之后，每个调用点要做的事从「查一件」变成「查三件」，而**漏掉其中
一件不会有任何测试变红**——这正是那两条能在五个调用点上同时缺席几个月的原因：
每一处都写着 `unverified_numbers(...)`，看起来都在过闸。

所以过闸走一个入口：`review_output(text, facts_text, guard)` 一次把三件都查完，
返回两类越界。**话术仍然留给调用方**（`chat` 说「已丢弃，换个问法」，简报说「改用
模板输出」），共用的是「要查哪几件、按什么顺序」，同 `jobs/sources.py` 开头那条纪律。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# 小到这个程度的整数是条目序号与档位数（「第 3 档」「5 档买盘」），模型正当地会
# 写出来，而它们本来就不在事实里。
ORDINAL_CEILING = 20

# 千分位是 token 的一部分，不是三个数：把 "12,198,154.38" 切成 12 / 198 / 154.38
# 会让每一个带分隔符的数字都看起来是编的。
NUMBER_PATTERN = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")

# 中文财经行文会就地换算量级。带上后缀，前面那个数就能按对应量级去和事实比，
# 于是 "0.12亿" 对的是 12198154.38，而不是被判成凭空捏造。
UNIT_SCALES: dict[str, float] = {"万": 1e4, "亿": 1e8, "%": 0.01, "％": 0.01}


def fact_numbers(facts_text: str) -> tuple[set[str], set[float]]:
    """事实里出现过的数字，两种形态一起给。

    - **token**：去掉千分位后的原样字符串，用于「原文里有没有这么写过」；
    - **value**：能转成 float 的那些，用于按精度或量级折算后比对。

    两者要一起返回而不是各算各的：调用方先查 token 命中（便宜且最准），未命中
    才落到数值比对，两步之间用的必须是同一次扫描的结果。
    """
    tokens = {token.replace(",", "") for token in NUMBER_PATTERN.findall(facts_text)}
    values: set[float] = set()
    for token in tokens:
        try:
            values.add(float(token))
        except ValueError:
            continue
    return tokens, values


def unverified_numbers(generated: str, facts_text: str) -> list[str]:
    """Numbers in `generated` that cannot be traced back to `facts_text`.

    Three ways a number is accepted, in increasing order of leniency:

    1. it appears verbatim in the facts;
    2. some fact value equals it once rounded to the precision the model wrote
       it at — this is what lets 8.1 be written as "8.10" and
       12198154.379999977 as "12198154.38";
    3. the same, after applying the 万 / 亿 / % suffix that follows it.

    Without (2) and (3) the gate fires on every response, because rewriting
    prose *is* reformatting numbers. What it still cannot accept is a figure
    with no basis in the input at any scale or precision, which is the failure
    mode that matters: an invented target price or a made-up count.

    Thousands separators are part of the token, not three separate numbers:
    tokenising "12,198,154.38" as 12 / 198 / 154.38 made every grouped figure
    look fabricated.
    """
    fact_tokens, fact_values = fact_numbers(facts_text)
    offenders: list[str] = []
    for match in NUMBER_PATTERN.finditer(generated):
        raw_token = match.group()
        token = raw_token.replace(",", "")
        if token in fact_tokens or token in facts_text:
            continue
        if "." not in token and int(token) <= ORDINAL_CEILING:
            continue

        value = float(token)
        decimals = len(token.partition(".")[2])
        scales = [1.0]
        suffix = generated[match.end() : match.end() + 1]
        if suffix in UNIT_SCALES:
            scales.append(UNIT_SCALES[suffix])

        if any(
            round(fact / scale, decimals) == value
            for fact in fact_values
            for scale in scales
        ):
            continue
        offenders.append(raw_token)
    return list(dict.fromkeys(offenders))

# ---- 第二条口径：措辞 ------------------------------------------------------

# 这些词本身就是一个「系统给不出的数」。前六个是量化承诺，后面几个是口语化的
# 同一件事——「翻倍」和「预期收益率 100%」在读的人那里是一个意思。
CLAIM_MARKERS: tuple[str, ...] = (
    "胜率",
    "概率",
    "预期收益率",
    "预期收益",
    "期望收益",
    "年化收益",
    "必涨",
    "必跌",
    "稳赚",
    "包赚",
    "保本",
    "翻倍",
    "百分百",
)

# 否定词。**这一条不是宽容，是必需**：「系统不产出概率、胜率、预期收益率」是
# 纪律段的原话，`notify/render.py` 每天把它印进简报、`ask/report.py` 每次把它写进
# 报告——它就在喂给模型的事实里，模型照抄回来是正确行为。不认否定的话，这道闸门
# 拦下的第一段文字会是系统自己那句免责声明。
#
# **逐个列出来，不拿「不」当通配。** 中文里的否定是一个封闭词类，而「不」在形容词
# 前面根本不是否定：「行情不错，可以满仓」里的那个「不」否定的是行情，用单字匹配
# 会把这句话整个放行——那正是这道闸门最该拦的一句。同类的还有不少 / 不低 / 不足 /
# 不断 / 不确定。
NEGATION_HINTS: tuple[str, ...] = (
    "不建议",
    "不宜",
    "不要",
    "不应",
    "不该",
    "不能",
    "不可",
    "不得",
    "不必",
    "不用",
    "不会",
    "不再",
    "不是",
    "不做",
    "不给",
    "不出",
    "不产出",
    "不参与",
    "不存在",
    "无法",
    "无需",
    "没有",
    "未能",
    "暂不",
    "先不",
    "别",
    "勿",
    "禁止",
    "严禁",
    "避免",
    "拒绝",
    "谈不上",
    "算不上",
)

# 往前看几个字找否定词。中文里否定词和被否定的词之间通常只隔一两个成分
# （「不产出」「没有可靠的」「无法给出」），12 个字够用；再长就会把上一句的
# 否定词算进来，那是另一句话。
NEGATION_LOOKBACK = 12


# ---- 第三条口径：立场 ------------------------------------------------------

# 重仓措辞。仓位上限一旦算出来（哪怕是最宽松的 0.80 那一档），这几个词说的都是
# 另一件事，而**改变结论**正是提示词第 1 条禁止的。
HEAVY_POSITION_MARKERS: tuple[str, ...] = (
    "满仓",
    "重仓",
    "全仓",
    "梭哈",
    "顶格",
    "追高",
    "越跌越买",
)

# 买入措辞。只在上限为 0（冰点：**不出手**，`strategy/regime.py:_POSITION_CAP`）
# 那一档才算越界——别的档位下「低吸」本来就是允许的打法，拦它等于把闸门变成噪声。
ENTRY_MARKERS: tuple[str, ...] = ("买入", "加仓", "建仓", "低吸", "抄底", "进场", "介入")

# 即时动作。盘中之外没有可执行的对象：22:00 问出来的「立即买入」，指的是一个
# 已经不存在的时刻。
IMMEDIATE_ACTION_MARKERS: tuple[str, ...] = (
    "立即买入",
    "马上买入",
    "立刻买入",
    "现在买入",
    "立即卖出",
    "马上卖出",
    "立刻卖出",
    "现在卖出",
    "立即加仓",
    "马上加仓",
    "立即减仓",
    "马上减仓",
)

# 计划前缀。「开盘后立即卖出」「跌破 X 立即减仓」是**计划**，不是此刻的指令，
# 而盘前简报的正事就是写计划——这几个词和否定词走同一条豁免。
SCHEDULED_HINTS: tuple[str, ...] = (
    "开盘",
    "竞价",
    "明日",
    "明天",
    "次日",
    "盘中",
    "触发",
    "跌破",
    "站上",
    "若",
    "如果",
    "一旦",
)

# 风险语境。**这一条是被空头角色逼出来的**：`agents/debate.py` 的空头就是来提示
# 风险的，「此时追高风险极大」「重仓需谨慎」用的正是主张重仓时的那几个词，而它
# 说的是反面。否定词在这类句子里出现在**词的后面**，所以往前看永远看不到。
RISK_HINTS: tuple[str, ...] = ("风险", "危险", "谨慎", "警惕", "回撤", "套牢", "亏损", "教训")

# 往后看几个字找风险语境。比否定那一档短：中文里「追高风险」这类搭配是紧挨着的，
# 放宽会把下一个分句的风险提示算进来，那时闸门就等于关掉了。
RISK_LOOKAHEAD = 6

# 唯一能执行即时动作的时段。取值同 `ask/context.py:PHASE_INTRADAY`，此处不 import
# 那个模块：`ask/` 依赖包根，反过来会成环。
TRADABLE_PHASE = "intraday"


@dataclass(frozen=True)
class OutputGuard:
    """确定性层已经给出的两个结论，闸门只拿它们当尺子，自己不看行情。

    三个字段都可能是 None，含义是**这一项今天没算出来**，那就不判——同
    `DECISIONS.md` §七「没数据 ≠ 中性」：给一个默认上限会让闸门凭空拦下正常文字。
    """

    phase: str | None = None
    position_cap: float | None = None
    cycle: str | None = None

    @classmethod
    def from_facts(cls, facts: Any, phase: str | None = None) -> OutputGuard:
        """从喂给模型的那份事实里取尺子。认不出来就是一把空尺子。

        四种形状都要认，因为几条链路各写各的：`jobs/morning_facts.py:market_state`
        给的是 `regime` + `regime_position_cap` 两个平铺字段，`jobs/post_close.py`
        给的是 `regime` 一整个 dict，盘前简报那份 facts 里只有 `position_factor`，
        而 `ask/plan.py` 的分步事实**用中文键**（那一层是挑几个字段改写给模型看的，
        `"仓位系数"` 就是 `position_factor`）。

        中文键这一条是补出来的：漏掉它时，深挖那条链路的每一步论证都只查得到措辞
        与时段，立场那一条静默失效——而**失效的方向恰好是放行**。

        `position_factor` 排在最后，因为它是**生效值**而不是上限（手工值可以高过
        上限，`strategy/positioning.py` 会为此单独报警）。但它同样是确定性层当天
        给出的那个数，文字与它矛盾一样是在改结论。
        """
        if not isinstance(facts, dict):
            return cls(phase=phase)
        regime = facts.get("regime") or facts.get("情绪周期")
        cycle = regime.get("cycle") if isinstance(regime, dict) else regime
        cap = _first_float(
            facts.get("regime_position_cap"),
            regime.get("position_cap") if isinstance(regime, dict) else None,
            facts.get("position_factor"),
            facts.get("仓位系数"),
        )
        return cls(
            phase=phase,
            position_cap=cap,
            cycle=cycle if isinstance(cycle, str) else None,
        )

    def stance_conflicts(self, text: str) -> list[str]:
        """文字里和上面那把尺子矛盾的地方。"""
        found: list[str] = []
        cap = self.position_cap
        if cap is not None and cap <= 0:
            where = f"情绪周期 {self.cycle} 的" if self.cycle else ""
            for marker in ENTRY_MARKERS + HEAVY_POSITION_MARKERS:
                if _asserted(text, marker):
                    found.append(f"{where}仓位上限 {cap:.2f} 说今天不出手，文字却在说「{marker}」")
        elif cap is not None:
            for marker in HEAVY_POSITION_MARKERS:
                if _asserted(text, marker):
                    found.append(f"仓位系数只有 {cap:.2f}，文字却在说「{marker}」")
        if self.phase and self.phase != TRADABLE_PHASE:
            for marker in IMMEDIATE_ACTION_MARKERS:
                if _asserted(text, marker, exempt=SCHEDULED_HINTS):
                    found.append(f"当前不是盘中（{self.phase}），文字却给出即时动作「{marker}」")
        return found


def prose_violations(text: str, guard: OutputGuard | None = None) -> list[str]:
    """一段模型产出的文字里越过确定性层的地方。空列表就是过闸。

    和 `unverified_numbers` 并列而不是合并成一个函数：那一条的严格程度按层不同
    （本模块开头那张表），这一条对所有出文字的地方都一样严。两条一起用的入口是
    `review_output`。
    """
    found = [f"预测性措辞「{marker}」" for marker in CLAIM_MARKERS if _asserted(text, marker)]
    if guard is not None:
        found.extend(guard.stance_conflicts(text))
    return list(dict.fromkeys(found))


# ---- 单一入口 --------------------------------------------------------------

# 越界项列进话术时最多带几条。三条足够说明是哪一类问题，再多就没人读了，而完整
# 清单在返回值里，调用方要写进日志随时能取。
REPORTED_VIOLATIONS = 3


@dataclass(frozen=True)
class OutputReview:
    """一段模型文字的过闸结果。两类分开列，因为调用方的话术不同。"""

    numbers: tuple[str, ...] = ()
    prose: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.numbers and not self.prose

    def reason(self, limit: int = REPORTED_VIOLATIONS) -> str:
        """为什么丢弃这一段，一句话。空串表示没有越界。

        措辞刻意用「数据中不存在」而不是「无法追溯」：这句话会原样出现在简报的
        notes 与问答的回执里，读它的人要立刻知道是**哪一类**问题——一个编出来的
        数字，和一句越过口径的话，处置一样但成因完全不同。
        """
        parts: list[str] = []
        if self.numbers:
            parts.append(f"含数据中不存在的数字 {'、'.join(self.numbers[:limit])}")
        if self.prose:
            parts.append(f"越过确定性层（{'；'.join(self.prose[:limit])}）")
        return "；".join(parts)


def review_output(
    text: str, facts_text: str | None = None, guard: OutputGuard | None = None
) -> OutputReview:
    """让模型出文字的地方，**都从这里过闸**。

    单一入口不是为了少写两行，是因为漏掉其中一件不会有任何测试变红：措辞与立场
    那两条能在五个调用点上同时缺席几个月，正是因为每一处都写着
    `unverified_numbers(...)`，看起来都在过闸。

    `facts_text=None` 表示**数字那一档由调用方自己把关**——价位裁决与外部裁决包
    各有一道更严的（本模块开头那张表），它们只把措辞与立场交给这里，不能改用这边
    宽松的那一档。

    `guard` 为 None 时不判立场——判不了的时候不判（同 `OutputGuard` 的注释）。
    """
    return OutputReview(
        numbers=tuple(unverified_numbers(text, facts_text)) if facts_text is not None else (),
        prose=tuple(prose_violations(text, guard)),
    )


def _asserted(text: str, marker: str, exempt: tuple[str, ...] = ()) -> bool:
    """`marker` 至少出现过一次，且那一次不是被否定、被当作计划或被当作风险说出来的。

    三种豁免的方向不同，所以窗口也不同：否定与计划前缀在词的**前面**（「不建议
    重仓」「开盘后立即卖出」），风险语境在词的**后面**（「重仓风险极大」）。
    """
    hints = NEGATION_HINTS + exempt
    start = 0
    while (index := text.find(marker, start)) >= 0:
        before = text[max(0, index - NEGATION_LOOKBACK) : index]
        after = text[index + len(marker) : index + len(marker) + RISK_LOOKAHEAD]
        if not any(hint in before for hint in hints) and not any(
            hint in after for hint in RISK_HINTS
        ):
            return True
        start = index + len(marker)
    return False


def _first_float(*values: Any) -> float | None:
    """第一个能当浮点用的值。布尔不算——`True` 会静默变成 1.0。"""
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None
