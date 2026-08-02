"""时间表与守护进程。

守护进程而不是 cron，理由只有一个但足够：**盘中监控需要跨轮次的状态**。告警要按
跃迁触发、要去重、要记住止损钉在哪里，cron 每次新起进程会把这些全丢掉。其余任务
本来无所谓，但拆成两套调度不值得。

三条运行时保证：

- **幂等**：守护进程按短周期查表，没有完成记录就会在同一分钟内重复触发。跑过的
  任务当天不再跑（`state/runstate.py`）。
- **失败隔离**：单个任务抛异常只记一条失败并推一条通知，进程继续。
- **重启恢复**：13:00 重启的进程不会重发盘前简报，也不会跳过下午的任务。
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock_time
from typing import Any, Callable

from ashare_decision_support.notify.base import Notifier
from ashare_decision_support.schedule.calendar import TradingCalendar
from ashare_decision_support.state import STATUS_FAILED, STATUS_OK, DaemonState

# 守护进程查表周期。比最短任务间隔小一个量级即可，纯等待不耗资源。
TICK_SECONDS = 30

# 单个任务当天最多重试次数。失败的任务下一 tick 会重试，但不能无限重试到收盘。
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class ScheduledTask:
    key: str
    at: clock_time
    label: str
    run: Callable[[], Any]
    # 长任务（监控循环）自己管理运行时长，调度器只负责在窗口开始时启动一次。
    blocking: bool = False


@dataclass
class DaemonReport:
    day: date
    ran: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def run_daemon(
    tasks: list[ScheduledTask],
    notifiers: list[Notifier] | None = None,
    calendar: TradingCalendar | None = None,
    day: date | None = None,
    now_fn: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
    tick_seconds: int = TICK_SECONDS,
    max_ticks: int | None = None,
    state: DaemonState | None = None,
) -> DaemonReport:
    """按时间表跑一天。`max_ticks` 让测试不必等真实时间。"""
    today = day or now_fn().date()
    report = DaemonReport(day=today)
    cal = calendar or TradingCalendar()

    if not cal.is_trading_day(today):
        report.notes.append(cal.explain(today))
        report.skipped = [task.key for task in tasks]
        return report
    report.notes.append(cal.explain(today))

    run_state = state or DaemonState.load(today)
    ordered = sorted(tasks, key=lambda task: task.at)
    ticks = 0

    while True:
        if max_ticks is not None and ticks >= max_ticks:
            break
        moment = now_fn()
        ticks += 1

        pending = [
            task
            for task in ordered
            if moment.time() >= task.at
            and not run_state.is_done(task.key)
            and run_state.attempts(task.key) < MAX_ATTEMPTS
        ]
        if not pending:
            remaining = [task for task in ordered if not run_state.is_done(task.key)]
            if not remaining or all(
                run_state.attempts(task.key) >= MAX_ATTEMPTS for task in remaining
            ):
                break
            if max_ticks is None:
                sleep(tick_seconds)
                continue
            if ticks >= (max_ticks or 0):
                break
            sleep(tick_seconds)
            continue

        task = pending[0]
        try:
            summary = task.run()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            run_state.record(task.key, STATUS_FAILED, moment, error=detail)
            report.failed.append(task.key)
            report.notes.append(f"{task.label} 失败（第 {run_state.attempts(task.key)} 次）：{detail}")
            _publish(
                notifiers or [],
                f"任务失败：{task.label}",
                f"**{task.label}** 执行失败\n\n```\n{detail}\n```\n\n"
                f"第 {run_state.attempts(task.key)}/{MAX_ATTEMPTS} 次尝试。"
                f"其余任务不受影响，守护进程继续运行。\n\n"
                f"```\n{traceback.format_exc(limit=3)}\n```",
            )
        else:
            run_state.record(task.key, STATUS_OK, moment, summary=_summarize(summary))
            report.ran.append(task.key)

        if max_ticks is None:
            sleep(1)

    report.skipped = [
        task.key
        for task in ordered
        if not run_state.is_done(task.key) and task.key not in report.failed
    ]
    return report


def _summarize(value: Any) -> Any:
    """任务返回值可能很大（整份简报），状态文件只留标量。"""
    if not isinstance(value, dict):
        return None
    return {
        key: item
        for key, item in value.items()
        if isinstance(item, (int, float, str, bool)) and key != "content"
    }


def _publish(notifiers: list[Notifier], title: str, content: str) -> None:
    for notifier in notifiers:
        try:
            notifier.send(title, content)
        except Exception:
            continue
