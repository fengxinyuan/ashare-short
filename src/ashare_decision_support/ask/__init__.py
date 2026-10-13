"""按当前时段装配事实，组织问题、规则决策卡与研究报告。

复用日常任务的落盘状态，盘中实时价位按需采集；缺失和陈旧数据保留说明。
深挖模式依次解析问题、建立研究计划并逐步组织报告。结论和价位来自规则，
显式开启的模型只解释已有事实。命令说明见 `docs/USAGE.md`。
"""

from ashare_decision_support.ask.context import (
    PHASE_INTRADAY,
    PHASE_OFF_HOURS,
    PHASE_POST_CLOSE,
    PHASE_PRE_OPEN,
    AskContext,
    assemble,
    current_phase,
    load_known_names,
    resolve_phase,
)
from ashare_decision_support.ask.plan import ResearchPlan, Step, build_plan
from ashare_decision_support.ask.question import QuestionSpec, parse_question
from ashare_decision_support.ask.report import render_report
from ashare_decision_support.ask.runner import run_ask
from ashare_decision_support.ask.verdict import Decision, assess, from_context, rank, summarise

__all__ = [
    "PHASE_INTRADAY",
    "PHASE_OFF_HOURS",
    "PHASE_POST_CLOSE",
    "PHASE_PRE_OPEN",
    "AskContext",
    "Decision",
    "QuestionSpec",
    "ResearchPlan",
    "Step",
    "assemble",
    "assess",
    "build_plan",
    "current_phase",
    "from_context",
    "load_known_names",
    "parse_question",
    "rank",
    "render_report",
    "resolve_phase",
    "run_ask",
    "summarise",
]
