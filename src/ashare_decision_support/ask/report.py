"""把决策卡、分步论证与可视化组织成一份报告。**先结论，再结构，再细节。**

顺序是刻意的：

```text
先看结论 → 速览表 → 价位轴 → 逐只决策卡 → 环境与其他 → 研究过程 → 纪律
```

前半段全部由规则产生，不经过模型：模型挂了、没配 key、输出被闸门丢了，这几段
一个字都不会少。模型的产出只出现在每张卡下面的「论证」里，缺了就写明缺了，卡上的
结论与价位不受影响。

这是「有重点」和「可验证」的同一件事的两面：能被丢弃的东西，本来就不该是结论。
"""

from __future__ import annotations

from typing import Any

from ashare_decision_support.ask.plan import STEP_SYNTHESIS, ResearchPlan
from ashare_decision_support.ask.question import INTENT_SCREEN, QuestionSpec
from ashare_decision_support.ask.verdict import (
    LABEL_ACTIONABLE,
    LABEL_BLOCKED,
    Decision,
    summarise,
)
from ashare_decision_support.ask.visual import comparison_table, holding_line, price_ladder
from ashare_decision_support.notify.render import DISCLAIMER

DISCIPLINE = [
    "价位是**结构位**，不是预测；触发前不动，触发后按纪律执行，不临场改止损。",
    "盈亏比不足 1.5 的机会直接放弃——这是规则层已经替你判掉的那一档。",
    "系统不产出概率、胜率、预期收益率：没有拟合模型能支撑它们。",
    "系统不下单、不撤单、不接管账户；以上全部是分析输出，执行由你自己决定。",
]

# 你在问题里说明的交易约束，原样回写进纪律段。系统没有别的地方能知道这些前提，
# 所以它只复述，不据此改任何数字。
CONSTRAINT_LINES = {
    "保守": "你说明了按保守口径：只有「可执行」那一档值得动，「需确认」的等触发。",
    "激进": "你说明了按激进口径：系统仍只报结构位，激进与否体现在仓位，不体现在价位。",
    "不过夜": "你说明了不持仓过夜：收盘前未触及目标的仓位按纪律处理，不靠隔夜跳空找补。",
    "次日兑现": "你说明了次日兑现：台账的 `next_open/high/low/close` 口径对应的正是这个持有期。",
}


def render_report(
    spec: QuestionSpec,
    plan: ResearchPlan,
    decisions: list[Decision],
    findings: dict[str, Any],
    phase_label: str,
    as_of: str,
    notes: list[str] | None = None,
    source: str | None = None,
    position_factor: float | None = None,
    screen: dict[str, Any] | None = None,
    screen_health: str | None = None,
) -> str:
    lines: list[str] = [f"# 【{phase_label}】{as_of}"]
    if spec.raw:
        lines.append(f"**问题**　{spec.raw}")
    lines.append(f"**解析**　{spec.describe()}")
    lines.append("")

    lines.append("## 先看结论")
    lines.append(_verdict_line(decisions, screening=INTENT_SCREEN in spec.intents))
    lines.extend([f"价位来源：{source}"] if source else [])
    lines.append("")
    lines.extend(_screening_section(spec, screen, screen_health))
    if decisions:
        lines.append("## 速览表")
        lines.append(comparison_table(decisions))
        lines.append("")
        synthesis = (findings.get(STEP_SYNTHESIS) or {}).get("text")
        if synthesis:
            lines.append("排序说明：")
            lines.append(synthesis.strip())
            lines.append("")
        lines.append("## 价位轴")
        for decision in decisions:
            lines.append(f"### {decision.symbol}{' ' + decision.name if decision.name else ''}")
            lines.append("```")
            lines.extend(price_ladder(decision))
            lines.append("```")
        lines.append("")

    if decisions:
        lines.append("## 逐只决策卡")
        for decision in decisions:
            lines.extend(_decision_block(decision, findings))
        lines.append("")

    context_sections = _context_sections(plan, findings)
    if context_sections:
        lines.append("## 环境与其他")
        lines.extend(context_sections)
        lines.append("")

    lines.extend(_research_section(plan, findings))

    lines.append("## 纪律")
    lines.extend(f"- {item}" for item in DISCIPLINE)
    if position_factor is not None:
        lines.append(
            f"- 今日仓位系数 {position_factor}（盘前主链路算出的**建议值**，封顶 0.80；"
            "实际仓位以你自己的设定为准）。"
        )
    for constraint in spec.constraints:
        if constraint in CONSTRAINT_LINES:
            lines.append(f"- {CONSTRAINT_LINES[constraint]}")
    lines.append("")

    boundary = list(notes or []) + list(spec.notes)
    if boundary:
        lines.append("## 数据边界")
        lines.extend(f"- {note}" for note in boundary)
        lines.append("")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


# 段落


def _verdict_line(decisions: list[Decision], screening: bool = False) -> str:
    """一句话结论。**先说有没有可执行的，再说是哪几只。**"""
    if not decisions:
        if screening:
            return "本轮没有经过当日价位验证的个股决策卡；先核对下方筛选结果与数据日期。"
        return "没有任何标的能被评估——上下文里没有可用的买卖点数据。"
    actionable = [item for item in decisions if item.label == LABEL_ACTIONABLE]
    blocked = [item for item in decisions if item.label == LABEL_BLOCKED]
    head = summarise(decisions)
    if actionable:
        names = "、".join(item.symbol for item in actionable)
        tail = f"**此刻条件已满足的是 {names}**。"
    else:
        tail = "**此刻没有条件已满足的标的**，其余都在等触发或已被阻断。"
    if blocked:
        tail += f"　被阻断 {len(blocked)} 只，理由见各自卡上的阻断项。"
    return f"{head}。{tail}"


def _screening_section(
    spec: QuestionSpec, screen: dict[str, Any] | None, status: str | None
) -> list[str]:
    if INTENT_SCREEN not in spec.intents:
        return []
    return [*_screening_lines(screen, status), ""]


def _screening_lines(screen: dict[str, Any] | None, status: str | None) -> list[str]:
    lines = ["## 选股结果"]
    if not isinstance(screen, dict):
        lines.append("没有本地筛选结果。先运行 `ashare-ds strategy-screen --profile all`。")
        return lines
    stamp = screen.get("as_of") or "日期未知"
    if status != "当日":
        lines.append(
            f"最近一次筛选截止 {stamp}（{status or '时效未知'}），不能作为今天下午的候选。"
            "请重新运行 `ashare-ds strategy-screen --profile all`，再用 `ashare-ds advise <代码>` "
            "核对当日价位和风险。"
        )
        return lines
    candidates = screen.get("diversified_shortlist") or screen.get("candidates") or []
    if not candidates:
        lines.append(f"筛选截止 {stamp}，本次没有符合参数条件的候选。")
        return lines
    names = "、".join(
        f"{item.get('symbol', '—')} {item.get('name', '')}".strip()
        for item in candidates[:8]
        if isinstance(item, dict)
    )
    lines.append(
        f"筛选截止 {stamp}；参数筛出的前列为 {names or '—'}。"
        "这是形态观察名单，尚未通过当日分时、盘口与风险核对；"
        "逐只运行 `ashare-ds advise <代码>` 后再判断。"
    )
    return lines


def _decision_block(decision: Decision, findings: dict[str, Any]) -> list[str]:
    lines = [f"### {decision.headline()}"]
    holding = holding_line(decision)
    if holding:
        lines.append(f"- **持仓**　{holding}")
    if decision.triggers:
        lines.append("- **触发条件**　" + "；".join(decision.triggers))
    if decision.invalidations:
        lines.append("- **失效条件**　" + "；".join(decision.invalidations))
    if decision.blockers:
        lines.append("- **阻断项**　" + "；".join(decision.blockers))
    if decision.readings:
        lines.append("- **关键读数**　" + "　".join(decision.readings))

    finding = findings.get(f"symbol:{decision.symbol}")
    lines.append("")
    if finding and finding.get("text"):
        lines.append(finding["text"].strip())
    else:
        reason = (finding or {}).get("note") or "本次没有对这只票单独做论证"
        # 论证缺失是一条状态，不是空白。空白读起来像「没什么可说的」，
        # 而实际情况是「那一段被丢弃了，上面的数不受影响」。
        lines.append(f"> 论证段缺失：{reason}。上面的结论与价位由规则算出，不受影响。")
    lines.append("")
    return lines


def _context_sections(plan: ResearchPlan, findings: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for step in plan.steps:
        # 逐只论证进决策卡，排序解释紧跟排序表——两者都不该在这里再出现一遍。
        if step.key.startswith("symbol:") or step.key == STEP_SYNTHESIS:
            continue
        finding = findings.get(step.key)
        if not step.supported:
            lines.append(f"### {step.title}")
            lines.append(f"> {step.gap}")
            lines.append("")
            continue
        if not finding or not finding.get("text"):
            continue
        lines.append(f"### {step.title}")
        lines.append(finding["text"].strip())
        lines.append("")
    return lines


def _process_lines(plan: ResearchPlan, findings: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for step in plan.steps:
        if not step.supported:
            lines.append(f"- {step.title} — ⊘ 跳过：{step.gap}")
            continue
        finding = findings.get(step.key) or {}
        if finding.get("text"):
            lines.append(f"- {step.title} — ✅")
        else:
            lines.append(f"- {step.title} — ⚠️ {finding.get('note') or '未执行'}")
    lines.extend(f"- （{note}）" for note in plan.notes)
    return lines


def _research_section(plan: ResearchPlan, findings: dict[str, Any]) -> list[str]:
    if not plan.runnable and not plan.notes:
        return []
    return ["## 研究过程", *_process_lines(plan, findings), ""]
