"""调度层：时间表 · 交易日历 · 守护进程。"""

from ashare_decision_support.schedule.calendar import TradingCalendar
from ashare_decision_support.schedule.daemon import (
    MAX_ATTEMPTS,
    DaemonReport,
    ScheduledTask,
    run_daemon,
)
from ashare_decision_support.schedule.timetable import (
    DEFAULT_TIMES,
    LABELS,
    TASK_MONITOR,
    TASK_MORNING,
    TASK_NEWS,
    TASK_POSTCLOSE,
    TASK_PREOPEN,
    TASK_REVIEW,
    build_timetable,
    describe,
)

__all__ = [
    "DEFAULT_TIMES",
    "LABELS",
    "MAX_ATTEMPTS",
    "TASK_MONITOR",
    "TASK_MORNING",
    "TASK_NEWS",
    "TASK_POSTCLOSE",
    "TASK_PREOPEN",
    "TASK_REVIEW",
    "DaemonReport",
    "ScheduledTask",
    "TradingCalendar",
    "build_timetable",
    "describe",
    "run_daemon",
]
