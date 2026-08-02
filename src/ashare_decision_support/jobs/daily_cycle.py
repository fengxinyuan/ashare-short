"""把全天时间表接到具体任务上——`daemon` 命令的实现。

每个任务的构造都延迟到执行时刻（`adapter_factory` 而不是 `adapter`）：08:00 的
网络故障不该让 08:20 的任务在构造阶段就崩掉。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

from ashare_decision_support.jobs.advise import run_advise
from ashare_decision_support.jobs.monitor import run_monitor
from ashare_decision_support.jobs.morning import run_morning
from ashare_decision_support.jobs.post_close import run_post_close
from ashare_decision_support.jobs.review import run_review
from ashare_decision_support.monitor import DEFAULT_LEVELS
from ashare_decision_support.news import AkshareNewsSource, NewsCache
from ashare_decision_support.schedule import (
    TASK_MONITOR,
    TASK_MORNING,
    TASK_NEWS,
    TASK_POSTCLOSE,
    TASK_PREOPEN,
    TASK_REVIEW,
    TradingCalendar,
    build_timetable,
    describe,
    run_daemon,
)
from ashare_decision_support.state import CandidatePool, Watchlist


def run_daily_cycle(
    adapter_factory: Callable[[], Any],
    output_dir: str | Path = "briefs",
    trade_date: date | None = None,
    only: list[str] | None = None,
    dry_run: bool = False,
    writer_factory: Callable[[], Any] | None = None,
    debate_factory: Callable[[], Any] | None = None,
    notifier_factory: Callable[[str], list] | None = None,
    top_sectors: int = 5,
    candidates_per_sector: int = 3,
    max_constituents: int | None = None,
    alert_levels: tuple[str, ...] = DEFAULT_LEVELS,
    calendar: TradingCalendar | None = None,
    **daemon_kwargs: Any,
) -> dict[str, Any]:
    today = trade_date or date.today()
    output = Path(output_dir)
    writer = writer_factory or (lambda: None)
    debate = debate_factory or (lambda: None)
    notifiers = notifier_factory or (lambda slug: None)

    def news_task() -> dict:
        """只把当日舆情灌进缓存，让 08:20 的主链路直接命中，不重复拉。"""
        source = AkshareNewsSource(cache=NewsCache(output))
        docs, notes = source.fetch_market(today)
        announcements, notice_notes = source.fetch_announcements(today)
        return {
            "docs": len(docs),
            "announcements": len(announcements),
            "notes": notes + notice_notes,
        }

    def _cached_news() -> Any | None:
        """08:00 已经把当日舆情写进缓存，后面的任务复用同一份，不再重复拉。"""
        try:
            return AkshareNewsSource(cache=NewsCache(output))
        except RuntimeError:
            return None

    def morning_task() -> dict:
        return run_morning(
            adapter=adapter_factory(),
            output_dir=output,
            trade_date=today,
            top_sectors=top_sectors,
            candidates_per_sector=candidates_per_sector,
            max_constituents=max_constituents,
            writer=writer(),
            debate=debate(),
            notifiers=notifiers("morning"),
        )

    def preopen_task() -> dict:
        symbols = list(
            dict.fromkeys(Watchlist().symbols() + CandidatePool(today=today).symbols())
        )
        if not symbols:
            return {"skipped": "自选股与候选池均为空"}
        return run_advise(
            adapter=adapter_factory(),
            symbols=symbols,
            output_dir=output,
            trade_date=today,
            writer=writer(),
            news_source=_cached_news(),
            notifiers=notifiers("preopen"),
        )

    def monitor_task() -> dict:
        return run_monitor(
            adapter=adapter_factory(),
            trade_date=today,
            alert_levels=alert_levels,
            news_source=_cached_news(),
            notifiers=notifiers("alerts"),
        )

    def postclose_task() -> dict:
        symbols = Watchlist().symbols()
        if not symbols:
            return {"skipped": "自选股为空"}
        return run_post_close(
            adapter=adapter_factory(),
            symbols=symbols,
            output_dir=str(output),
            trade_date=today,
            debate=debate(),
        )

    def review_task() -> dict:
        return run_review(
            adapter=adapter_factory(), trade_date=today, notifiers=notifiers("review")
        )

    tasks = build_timetable(
        {
            TASK_NEWS: news_task,
            TASK_MORNING: morning_task,
            TASK_PREOPEN: preopen_task,
            TASK_MONITOR: monitor_task,
            TASK_POSTCLOSE: postclose_task,
            TASK_REVIEW: review_task,
        },
        only=only,
    )

    cal = calendar or TradingCalendar()
    if dry_run:
        return {
            "trade_date": str(today),
            "dry_run": True,
            "trading_day": cal.is_trading_day(today),
            "calendar": cal.explain(today),
            "timetable": describe(tasks, today),
            "tasks": [task.key for task in tasks],
        }

    report = run_daemon(
        tasks,
        notifiers=notifiers("daemon") or [],
        calendar=cal,
        day=today,
        **daemon_kwargs,
    )
    return {
        "trade_date": str(today),
        "trading_day": cal.is_trading_day(today),
        "ran": report.ran,
        "failed": report.failed,
        "skipped": report.skipped,
        "notes": report.notes,
    }
