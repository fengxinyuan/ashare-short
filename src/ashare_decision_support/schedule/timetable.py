"""全天时间表——把调度器和具体任务接起来的唯一一处。

```text
08:00  news       隔夜舆情采集，落当日缓存
08:20  morning    主链路：舆情 → 大盘 → 板块 → 选股 → 候选池 → 台账
09:15  preopen    自选股 + 候选池执行卡（昨收口径，明确标注）
09:35  monitor    盘中循环，跑到 14:55（长任务，自己管时长）
15:10  postclose  自选股打分、次日矩阵
15:40  review     T-1..T-5 建议兑现核对
```

任务的构造全部延迟到执行时刻：08:20 才创建 TickFlow 适配器，意味着 08:00 的网络
故障不会让 08:20 的任务在构造阶段就崩掉。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from datetime import time as clock_time
from typing import Any

from ashare_decision_support.schedule.daemon import ScheduledTask
from ashare_decision_support.schedule.timezone import beijing_today

TASK_NEWS = "news"
TASK_MORNING = "morning"
TASK_PREOPEN = "preopen"
TASK_MONITOR = "monitor"
TASK_POSTCLOSE = "postclose"
TASK_REVIEW = "review"

DEFAULT_TIMES: dict[str, clock_time] = {
    TASK_NEWS: clock_time(8, 0),
    TASK_MORNING: clock_time(8, 20),
    TASK_PREOPEN: clock_time(9, 15),
    TASK_MONITOR: clock_time(9, 35),
    TASK_POSTCLOSE: clock_time(15, 10),
    TASK_REVIEW: clock_time(15, 40),
}

LABELS: dict[str, str] = {
    TASK_NEWS: "舆情采集",
    TASK_MORNING: "盘前主链路",
    TASK_PREOPEN: "盘前执行卡",
    TASK_MONITOR: "盘中监控",
    TASK_POSTCLOSE: "盘后复盘",
    TASK_REVIEW: "兑现核对",
}


def build_timetable(
    runners: dict[str, Callable[[], Any]],
    times: dict[str, clock_time] | None = None,
    only: list[str] | None = None,
) -> list[ScheduledTask]:
    """把 {task_key: 可调用} 变成有序时间表。未提供 runner 的任务自动省略。"""
    schedule = {**DEFAULT_TIMES, **(times or {})}
    wanted = set(only) if only else set(runners)
    return [
        ScheduledTask(
            key=key,
            at=schedule[key],
            label=LABELS.get(key, key),
            run=runner,
            blocking=key == TASK_MONITOR,
        )
        for key, runner in runners.items()
        if key in schedule and key in wanted
    ]


def describe(tasks: list[ScheduledTask], day: date | None = None) -> str:
    header = f"交易日 {day or beijing_today()} 时间表"
    rows = [f"  {task.at:%H:%M}  {task.key:<10} {task.label}" for task in sorted(tasks, key=lambda t: t.at)]
    return "\n".join([header, *rows])
