"""研究计划：把一个问题拆成有限个**受支持的**子问题，每个只带自己那一小块事实。

原来的 `ask` 是一次调用产出整个答案：一份巨大的 JSON 进去，一段话出来。两个后果，
第二个更要命：

1. **没有重点。** 模型要在整包上下文里自己决定什么重要，而它没有依据可以决定。
2. **全有或全无。** 数字闸门是整段丢弃的（`factcheck.py:unverified_numbers`）。
   三只票十五个小节，只要有一个数追溯不到，十五个小节一起消失，你拿到的是一堆
   原始 JSON。

拆成步骤同时解决这两件事：每步只回答一个子问题、只看自己那一小块事实、**过自己
那一道闸门**。一步被丢弃只损失那一步，其余照出——这和 `agents/base.py` 那条
「单个源挂了不能拖垮跑批」是同一个要求，只不过这次挂掉的是模型不是数据源。

**计划本身是确定性的。** 哪几步、每步看什么，由「意图 × 手上有什么数据」决定，
不问模型。不受支持的子问题（比如问基本面）明确列成一条 `gap`——它不进模型，
报告里照样出现，写明缺的是什么。写「本系统没有这项数据」比让模型编一段像样的
营收分析安全得多，也比留白诚实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.ask.question import (
    INTENT_ENTRY,
    INTENT_EXIT,
    INTENT_GAP,
    INTENT_MARKET,
    INTENT_REVIEW,
    INTENT_RISK,
    INTENT_SCREEN,
    QuestionSpec,
)
from ashare_decision_support.ask.verdict import Decision, rank

# 逐标的的研究步骤上限。每一步是一次模型调用，问十只票就是十次往返——而超过
# 四只之后，报告本身也不再是「有重点」的了。被截掉的**必须报出来**，
# 静默截断读起来和「全都看过了」一模一样。
MAX_SYMBOL_STEPS = 4

# 一次问答的模型调用总上限（含综合那一步）。
MAX_STEPS = 8

STEP_MARKET = "market"
STEP_RISK = "risk"
STEP_JOURNAL = "journal"
STEP_SYNTHESIS = "synthesis"

# 本系统拿不到的东西。问到它们时明写，不让模型用常识补——`docs/DATA_SOURCES.md`
# 实测过：TickFlow Pro 档 `financials.*` 无权限，量化研究档给的是行情、分钟线、
# 五档与除权因子，没有一条财务数据。
UNSUPPORTED = {
    "基本面": "营收、净利润、扣非、应收账款、现金流：本系统没有财务数据源",
    "估值": "市盈率、市净率、市值分位：本系统没有估值数据源",
    "筹码": "股东户数、筹码平均成本、融资融券余额：本系统没有这几路数据",
}


@dataclass(frozen=True)
class Step:
    """一个子问题。`facts` 既是模型能看到的全部，也是这一步闸门的比对基准。"""

    key: str
    title: str
    question: str
    facts: dict[str, Any] = field(default_factory=dict)
    supported: bool = True
    gap: str = ""


@dataclass
class ResearchPlan:
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def runnable(self) -> list[Step]:
        return [step for step in self.steps if step.supported]

    @property
    def gaps(self) -> list[Step]:
        return [step for step in self.steps if not step.supported]

    def render(self) -> str:
        """研究过程本身要能看见——它说明了这次到底查了什么、没查什么。"""
        total = len(self.steps)
        lines = [f"研究计划（{total} 步）"]
        for index, step in enumerate(self.steps, start=1):
            mark = "" if step.supported else "　⊘ 跳过"
            lines.append(f"  {index}/{total} {step.title}{mark}")
            if step.gap:
                lines.append(f"        {step.gap}")
        lines.extend(f"（{note}）" for note in self.notes)
        return "\n".join(lines)


def build_plan(
    spec: QuestionSpec,
    blocks: dict[str, Any],
    decisions: list[Decision],
    phase: str = "",
) -> ResearchPlan:
    """按「意图 × 手上有什么」列步骤。不问模型，不取数。"""
    plan = ResearchPlan()
    intents = set(spec.intents)

    _unsupported_steps(plan, spec)
    if intents & {INTENT_MARKET, INTENT_SCREEN} or not decisions:
        _market_step(plan, blocks)

    ordered = rank(decisions)
    budget = min(MAX_SYMBOL_STEPS, max(0, MAX_STEPS - len(plan.steps) - 1))
    for decision in ordered[:budget]:
        plan.steps.append(_symbol_step(decision, spec, blocks))
    dropped = ordered[budget:]
    if dropped:
        # 排在后面的仍然进报告的速览表与排序，只是不单独做一步论证。
        plan.notes.append(
            f"逐只论证只做前 {budget} 只；{'、'.join(item.symbol for item in dropped)} "
            "仍进速览表与排序，但本次没有单独的论证段"
        )

    if INTENT_RISK in intents or any(item.blockers for item in decisions):
        _risk_step(plan, decisions)
    if INTENT_REVIEW in intents or phase == "post_close":
        _journal_step(plan, blocks)
    if len(decisions) > 1:
        _synthesis_step(plan, ordered)

    if len(plan.runnable) > MAX_STEPS:
        keep = plan.runnable[:MAX_STEPS]
        plan.notes.append(f"步骤超过上限 {MAX_STEPS}，本次只跑前 {MAX_STEPS} 步")
        plan.steps = keep + plan.gaps
    return plan


# 各步


def _unsupported_steps(plan: ResearchPlan, spec: QuestionSpec) -> None:
    """问到系统没有的东西，就在计划里立一条明确的空缺。

    这一条不进模型。它存在的唯一目的是让报告里出现「这一层没有数据」，而不是
    出现一段读起来很像基本面分析的东西。
    """
    text = spec.raw
    for topic, gap in UNSUPPORTED.items():
        hit = topic in text or any(
            word in text
            for word in {
                "基本面": ("营收", "净利润", "业绩", "扣非", "财报", "毛利", "应收"),
                "估值": ("市盈率", "PE", "估值", "市净率", "贵不贵"),
                "筹码": ("筹码", "股东户数", "融资融券", "北向", "主力成本"),
            }[topic]
        )
        if hit:
            plan.steps.append(
                Step(
                    key=f"gap:{topic}",
                    title=f"{topic}：本系统无此数据源",
                    question="",
                    supported=False,
                    gap=gap,
                )
            )


def _market_step(plan: ResearchPlan, blocks: dict[str, Any]) -> None:
    morning = blocks.get("盘前主链路")
    if not isinstance(morning, dict):
        plan.steps.append(
            Step(
                key=STEP_MARKET,
                title="大盘与板块环境",
                question="",
                supported=False,
                gap="盘前主链路尚无落盘结果，本次没有大盘方向与板块排序可引用",
            )
        )
        return

    health = blocks.get("数据健康") or {}
    morning_status = next(
        (item.get("判定") for item in health.get("块", []) if item.get("名称") == "盘前主链路"),
        None,
    )
    if morning_status != "当日" or morning.get("execution_ready") is not True:
        plan.steps.append(
            Step(
                key=STEP_MARKET,
                title="大盘与板块环境",
                question="",
                supported=False,
                gap="盘前主链路不是当日可执行结果，不能据此判断当前大盘；请运行 `ashare-ds morning` 刷新",
            )
        )
        return

    facts = {
        "交易日": morning.get("trade_date"),
        "大盘": morning.get("market"),
        "仓位系数": morning.get("position_factor"),
        "仓位系数依据": morning.get("position_factor_reasons"),
        "涨停池": morning.get("limit_pool"),
        # 板块只带前八：排序靠后的那些进不了任何决定，带进来只是稀释注意力。
        "板块排序": (morning.get("sectors") or [])[:8],
    }
    plan.steps.append(
        Step(
            key=STEP_MARKET,
            title="大盘方向与板块排序",
            question=(
                "用这份读数说清三件事：今天大盘方向的判断依据是什么、"
                "排在前面的板块强在哪一项读数上、仓位系数为什么是这个数。"
                "覆盖率不足时直接说数据不足，不要给方向。"
            ),
            facts=facts,
        )
    )


def _symbol_step(decision: Decision, spec: QuestionSpec, blocks: dict[str, Any]) -> Step:
    payload = _symbol_payload(blocks, decision.symbol)
    facts = {
        "决策卡": decision.to_dict(),
        "分时读数": (payload or {}).get("intraday"),
        "盘口": (payload or {}).get("order_book"),
        "候选价位": (payload or {}).get("candidates"),
        "情景": (payload or {}).get("scenarios"),
        "依据": (payload or {}).get("reasons"),
        "提示": (payload or {}).get("warnings"),
        "个股舆情": (payload or {}).get("news"),
        "当日告警": _alerts_for(blocks, decision.symbol),
    }
    title = f"{decision.symbol}{' ' + decision.name if decision.name else ''}"
    return Step(
        key=f"symbol:{decision.symbol}",
        title=f"{title} — {decision.label}",
        question=_symbol_question(decision, spec),
        facts=facts,
    )


def _symbol_question(decision: Decision, spec: QuestionSpec) -> str:
    intents = set(spec.intents)
    base = (
        f"这张卡的结论是「{decision.label}」，它由规则算出，你不得改动它。"
        "只做一件事：说清这个结论建立在哪几个读数上，以及触发条件与失效条件里"
        "哪一条最可能先发生。三到五句，每句引用一个具体读数。"
    )
    if INTENT_GAP in intents:
        return (
            base + "问题问的是低开/跳空：额外说明开盘价与买入带、止损位的相对位置，"
            "以及现在的量比和盘口是否支持「跌到位了」——支持不了就直说支持不了。"
        )
    if INTENT_EXIT in intents or decision.side == "sell":
        return (
            base + "问题问的是卖出侧：额外说明现价距生效止损与目标各有多远，"
            "以及浮动盈亏处在什么位置。"
        )
    if INTENT_ENTRY in intents:
        return (
            base + "问题问的是买点：额外说明这个买入带的出处，以及它凭什么比另一个候选价更值得等。"
        )
    return base


def _risk_step(plan: ResearchPlan, decisions: list[Decision]) -> None:
    blocked = {
        item.symbol: {"名称": item.name, "结论": item.label, "阻断项": item.blockers}
        for item in decisions
        if item.blockers
    }
    if not blocked:
        plan.steps.append(
            Step(
                key=STEP_RISK,
                title="风险与阻断项",
                question="",
                supported=False,
                gap="本次没有任何标的带阻断项——这不代表没有风险，只代表系统这几路读数没测到",
            )
        )
        return
    plan.steps.append(
        Step(
            key=STEP_RISK,
            title=f"风险与阻断项（{len(blocked)} 只）",
            question=(
                "逐条说明这些阻断项分别挡住了什么动作："
                "是「此刻做不成」（封板、停牌），还是「不该做」（风险公告、数据不新）。"
                "两者的处理方式不同，必须分开说。不得弱化，不得给缓解建议。"
            ),
            facts={"带阻断项的标的": blocked},
        )
    )


def _journal_step(plan: ResearchPlan, blocks: dict[str, Any]) -> None:
    stats = blocks.get("台账统计")
    daily = blocks.get("每日归因复盘")
    if not isinstance(daily, dict) and (not isinstance(stats, dict) or not stats.get("samples")):
        plan.steps.append(
            Step(
                key=STEP_JOURNAL,
                title="台账与兑现",
                question="",
                supported=False,
                gap="台账里还没有已兑现的记录，任何胜率类问题现在都答不了",
            )
        )
        return
    plan.steps.append(
        Step(
            key=STEP_JOURNAL,
            title="每日复盘与台账兑现" if isinstance(daily, dict) else "台账兑现情况",
            question=(
                "只陈述这份统计说了什么：样本量、先到目标与先到止损的比例、"
                "入选与对照组的差值。样本不足的分组必须标明样本不足。"
                "不得由此推出任何关于今天的结论——样本量不够时它只是记录。"
                "有每日复盘时，说明原计划、过程证据、缺口和次日复查项；"
                "生成的告警不能当作实际成交，时间相关不能当作新闻因果。"
            ),
            facts={
                key: value
                for key, value in (("台账统计", stats), ("每日归因复盘", daily))
                if isinstance(value, dict)
            },
        )
    )


def _synthesis_step(plan: ResearchPlan, ordered: list[Decision]) -> None:
    plan.steps.append(
        Step(
            key=STEP_SYNTHESIS,
            title="综合排序与取舍",
            question=(
                "下面这个顺序由规则算出（先按结论分档，再按盈亏比、评分），**你不得改动它**。"
                "解释三件事：排第一的和排最后的差别落在哪个读数上；"
                "如果只做一只，哪一只的失效条件最明确；"
                "有没有哪两只的依据其实是同一条（同板块、同一个消息），"
                "同时做等于把一次判断押两遍。不得新增标的，不得给概率。"
            ),
            facts={
                "规则排序": [
                    {
                        "序": index,
                        "symbol": item.symbol,
                        "名称": item.name,
                        "结论": item.label,
                        "盈亏比": item.rrr,
                        "评分": item.score,
                        "触发条件": item.triggers,
                        "阻断项": item.blockers,
                    }
                    for index, item in enumerate(ordered, start=1)
                ]
            },
        )
    )


# 取块


def _symbol_payload(blocks: dict[str, Any], symbol: str) -> dict | None:
    from ashare_decision_support.ask.verdict import ADVICE_BLOCKS

    for name in ADVICE_BLOCKS:
        block = blocks.get(name)
        if not isinstance(block, dict):
            continue
        for item in block.get("symbols") or []:
            if isinstance(item, dict) and item.get("symbol") == symbol:
                return item
    return None


def _alerts_for(blocks: dict[str, Any], symbol: str) -> list[dict]:
    block = blocks.get("当日告警")
    if not isinstance(block, dict):
        return []
    return [
        row
        for row in block.get("已触发") or []
        if isinstance(row, dict) and row.get("symbol") == symbol
    ]
