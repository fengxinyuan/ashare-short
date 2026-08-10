"""一次问答。两条路：

```text
--deep 关   装配上下文 → 一次调用 → 一道闸门 → 一段话        （原来的行为，最省）
--deep 开   解析问题 → 列研究计划 → 逐步研究 → 综合报告 → 可视化
```

深挖那条路的关键不是「多问几次」，是**每一步只看自己那一小块事实，并过自己那一道
闸门**。原来一次调用产出整个答案，一个追溯不到的数字下去，整段丢弃——三只票十五个
小节一起没了，你拿到的是一堆原始 JSON。现在丢的只是那一步的论证，决策卡、排序、
价位轴全部由规则产生，一个字都不会少。

没配 key 时深挖路径**照跑**：计划照列、卡照出、表和价位轴照画，只是每张卡下面写明
论证段缺失。这不是降级兜底，这是这套系统一贯的分层——数字只在确定性层产生，模型
只组织语言。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.ask.context import (
    PHASE_INTRADAY,
    PHASE_OFF_HOURS,
    PHASE_POST_CLOSE,
    PHASE_PRE_OPEN,
    AskContext,
    assemble,
    load_known_names,
    resolve_phase,
)
from ashare_decision_support.ask.plan import ResearchPlan, Step, build_plan
from ashare_decision_support.ask.question import (
    INTENT_ENTRY,
    INTENT_EXIT,
    INTENT_MARKET,
    INTENT_REVIEW,
    INTENT_SCREEN,
    QuestionSpec,
    parse_question,
)
from ashare_decision_support.ask.report import render_report
from ashare_decision_support.ask.verdict import from_context, rank
from ashare_decision_support.chat.session import SYSTEM_PROMPT
from ashare_decision_support.fanout import gather
from ashare_decision_support.schedule.timezone import beijing_now
from ashare_decision_support.writer.llm import unverified_numbers

# 时段说明拼在系统提示后面。同一个问题在盘前和盘中要的是不同的答案——盘前问
# 「这只怎么样」问的是要不要挂单，盘中问的是现在这个价位该不该动。
PHASE_HINTS = {
    "pre_open": "现在是开盘前。价位是昨收口径的结构位，不是实时报价，回答时要说清这一点。",
    "intraday": "现在是盘中。优先回答实时价位与当日告警，不要引用隔夜的旧结论。",
    "post_close": "现在是盘后。优先回答今天实际发生了什么、台账兑现情况。",
    "off_hours": "现在是非交易时段，上下文里全是旧数据。任何价位都要标明是哪一天的。",
}

# 问题里认不出意图时，按时刻给一个默认关注点。
PHASE_INTENTS = {
    PHASE_PRE_OPEN: [INTENT_MARKET, INTENT_SCREEN],
    PHASE_INTRADAY: [INTENT_ENTRY, INTENT_EXIT],
    PHASE_POST_CLOSE: [INTENT_REVIEW, INTENT_MARKET],
    PHASE_OFF_HOURS: [INTENT_MARKET, INTENT_REVIEW],
}

STEP_SYSTEM_PROMPT = """你是一名 A 股短线交易助理，正在做一份分步研究里的**其中一步**。

你只能看到这一步的【事实】，也只回答这一步的子问题。

严格约束：
1. 只依据【事实】回答。事实里没有的，直接说「这一步的数据里没有」，不要推测、不要用常识补。
2. 不得出现【事实】中没有的数字。价格、评分、涨跌幅、百分比一律照抄。
3. 规则已经算好的结论标签、排序、价位，你不得改动，只能解释它们建立在哪些读数上。
4. 不得给出「概率」「胜率」「预期收益率」——系统没有拟合模型。
5. 不得说「建议买入/卖出」。陈述触发条件、失效条件与读数，动作由用户自己决定。
6. 直接给判断，但不要像流水账。可以按“判断 → 证据 → 风险/观察条件”的顺序说完，最多五句。
7. 多个读数能合并就合并，别按字段顺序逐条复述；像研究员笔记，不像字段导出。"""

# 分步之间彼此不依赖，每步一次网络往返，所以并行。串行等于把几段延迟加起来。
DEFAULT_WORKERS = 4


def run_ask(
    question: str | None = None,
    symbols: list[str] | None = None,
    output_dir: str | Path | None = None,
    compute_symbols: Callable[[list[str]], dict] | None = None,
    now: datetime | None = None,
    client: Any = None,
    is_trading_day: bool | None = None,
    write: Callable[[str], Any] | None = None,
    deep: bool = False,
    max_workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    """回答一个问题，或者（不给问题时）把上下文原样打印出来。"""
    moment = now or beijing_now()
    emit = write if write is not None else print
    phase = resolve_phase(moment, is_trading_day)
    question_spec = (
        parse_question(
            question,
            known_names=load_known_names(),
            fallback_intents=PHASE_INTENTS.get(phase),
        )
        if question and question.strip()
        else None
    )
    requested_symbols = list(
        dict.fromkeys((symbols or []) + (question_spec.symbols if question_spec else []))
    )

    if deep:
        return _run_deep(
            question=question,
            question_spec=question_spec,
            symbols=requested_symbols or None,
            output_dir=output_dir,
            compute_symbols=compute_symbols,
            moment=moment,
            phase=phase,
            client=client,
            is_trading_day=is_trading_day,
            emit=emit,
            max_workers=max_workers,
        )

    ctx = assemble(
        moment=moment,
        symbols=requested_symbols or None,
        output_dir=output_dir,
        compute_symbols=compute_symbols,
        phase=phase,
        is_trading_day=is_trading_day,
        focus=requested_symbols or None,
    )
    if question_spec and question_spec.notes:
        ctx.notes.extend(question_spec.notes)

    if not question or not question.strip():
        emit(ctx.render())
        return _result(ctx, answer=None, notes=["未提问，只装配上下文"])

    llm = client
    if llm is None:
        emit(ctx.render())
        note = "默认不开模型对话，只给出装配好的原始上下文；显式开启模型请用 `--llm`"
        emit(f"\n（{note}）")
        return _result(ctx, answer=None, notes=[note])
    if not getattr(llm, "configured", False):
        # 没配 key 不是「功能不可用」，是「少了组织语言那一层」。事实照给。
        emit(ctx.render())
        note = "未配置 LLM key，只能给出装配好的原始上下文"
        emit(f"\n（{note}）")
        return _result(ctx, answer=None, notes=[note])

    try:
        reply = llm.chat(_messages(ctx, question))
    except Exception as exc:
        emit(ctx.render())
        note = f"LLM 调用失败（{type(exc).__name__}），只给出原始上下文"
        emit(f"\n（{note}）")
        return _result(ctx, answer=None, notes=[note])

    facts_text = ctx.facts_text()
    offenders = unverified_numbers(reply or "", facts_text)
    if offenders:
        note = f"回答含无法追溯的数字 {offenders[:5]}，整段丢弃"
        emit(ctx.render())
        emit(f"\n（{note}。上面是未经模型的原始数据。）")
        return _result(ctx, answer=None, notes=[note])

    emit(reply)
    return _result(ctx, answer=reply, notes=[])


# ---- 深挖 ----------------------------------------------------------------


def _run_deep(
    question: str | None,
    question_spec: QuestionSpec | None,
    symbols: list[str] | None,
    output_dir: str | Path | None,
    compute_symbols: Callable[[list[str]], dict] | None,
    moment: datetime,
    phase: str,
    client: Any,
    is_trading_day: bool | None,
    emit: Callable[[str], Any],
    max_workers: int,
) -> dict[str, Any]:
    spec = question_spec or parse_question(
        question, known_names=load_known_names(), fallback_intents=PHASE_INTENTS.get(phase)
    )
    # 问题点名的标的排最前，而且它们是唯一需要现算价位的——`focus` 就是那道闸。
    focus = list(dict.fromkeys((symbols or []) + spec.symbols))
    ctx = assemble(
        moment=moment,
        symbols=focus,
        output_dir=output_dir,
        compute_symbols=compute_symbols,
        phase=phase,
        is_trading_day=is_trading_day,
        focus=focus,
    )
    if spec.notes:
        ctx.notes.extend(spec.notes)

    raw, source = from_context(ctx.blocks, focus=focus)
    decisions = rank(raw)
    plan = build_plan(spec, ctx.blocks, decisions, phase=ctx.phase)

    llm = client
    findings = _execute(plan, llm, ctx.phase, max_workers)

    report = render_report(
        spec=spec,
        plan=plan,
        decisions=decisions,
        findings=findings,
        phase_label=ctx.phase_label,
        as_of=ctx.as_of,
        notes=ctx.notes,
        source=source,
        position_factor=_position_factor(ctx.blocks),
    )
    emit(report)

    result = _result(ctx, answer=report, notes=_deep_notes(plan, findings))
    result.update(
        {
            "mode": "deep",
            "question_spec": spec.to_dict(),
            "plan": [step.title for step in plan.steps],
            "steps_run": sum(1 for item in findings.values() if item.get("text")),
            "steps_total": len(plan.steps),
            "decisions": [item.to_dict() for item in decisions],
            "advice_source": source,
        }
    )
    return result


def _execute(
    plan: ResearchPlan, llm: Any | None, phase: str, max_workers: int
) -> dict[str, dict[str, Any]]:
    """并行跑每一步。**一步失败只损失这一步**，其余照出。"""
    steps = plan.runnable
    if not steps:
        return {}
    if llm is None or not getattr(llm, "configured", False):
        note = "未配置 LLM key，本次没有论证段；结论与价位由规则算出，不受影响"
        return {step.key: {"text": "", "note": note} for step in steps}

    done, errors = gather(
        steps, lambda step: _run_step(step, llm, phase), max_workers=max_workers
    )
    findings = {step.key: finding for step, finding in done}
    # 一步调用失败只损失这一步的论证段：它照样在字典里，只是带着说明没有正文。
    for step, exc in errors:
        findings[step.key] = {"text": "", "note": f"调用失败（{type(exc).__name__}）"}
    return findings


def _run_step(step: Step, llm: Any, phase: str) -> dict[str, Any]:
    """跑一步。**调用失败直接抛**，由 `_execute` 那层把它落成这一步的说明。"""
    facts_text = json.dumps(step.facts, ensure_ascii=False, indent=2, default=str)
    hint = PHASE_HINTS.get(phase, "")
    messages = [
        {"role": "system", "content": f"{STEP_SYSTEM_PROMPT}\n\n{hint}"},
        {
            "role": "user",
            "content": f"【事实】\n```json\n{facts_text}\n```\n\n【子问题】{step.question}",
        },
    ]
    reply = (llm.chat(messages) or "").strip()
    if not reply:
        return {"text": "", "note": "模型返回空内容"}

    # 闸门比对的是**这一步的**事实，不是全量上下文。这比原来更严：一段引用了别的
    # 步骤才有的数字，在这里会被拦下，而在全量比对下它能蒙混过关。
    offenders = unverified_numbers(reply, facts_text)
    if offenders:
        return {"text": "", "note": f"含无法追溯的数字 {offenders[:3]}，整段丢弃"}
    return {"text": reply, "note": ""}


def _position_factor(blocks: dict[str, Any]) -> float | None:
    """盘前算出来的仓位系数。取不到就不报——**没有默认值**，编一个 0.5 出来
    比不说更糟：它看起来和真的算出来的那个数一模一样。"""
    morning = blocks.get("盘前主链路")
    if not isinstance(morning, dict):
        return None
    value = morning.get("position_factor")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _deep_notes(plan: ResearchPlan, findings: dict[str, dict]) -> list[str]:
    notes = list(plan.notes)
    for step in plan.steps:
        if not step.supported:
            notes.append(f"{step.title}：{step.gap}")
            continue
        finding = findings.get(step.key) or {}
        if not finding.get("text") and finding.get("note"):
            notes.append(f"{step.title}：{finding['note']}")
    return list(dict.fromkeys(notes))


# ---- 共用 ----------------------------------------------------------------


def _messages(ctx: AskContext, question: str) -> list[dict]:
    hint = PHASE_HINTS.get(ctx.phase, "")
    return [
        {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{hint}"},
        {
            "role": "user",
            "content": f"当前事实：\n```json\n{ctx.facts_text()}\n```\n\n问题：{question}",
        },
    ]


def _result(ctx: AskContext, answer: str | None, notes: list[str]) -> dict[str, Any]:
    return {
        "phase": ctx.phase,
        "phase_label": ctx.phase_label,
        "as_of": ctx.as_of,
        "symbols": ctx.symbols,
        "blocks": sorted(ctx.blocks),
        "answer": answer,
        "context": ctx.render(),
        "notes": ctx.notes + notes,
    }
