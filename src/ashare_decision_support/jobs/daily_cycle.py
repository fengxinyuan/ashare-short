"""把全天时间表接到具体任务上——`daemon` 命令的实现。

每个任务的构造都延迟到执行时刻（`adapter_factory` 而不是 `adapter`）：08:00 的
网络故障不该让 08:20 的任务在构造阶段就崩掉。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.global_market import GlobalMarketAdapter
from ashare_decision_support.adapters.security_master import SecurityMasterAdapter
from ashare_decision_support.jobs.advise import run_advise
from ashare_decision_support.jobs.monitor import run_monitor
from ashare_decision_support.jobs.morning import (
    WIDE_MARKET_LIMIT,
    MorningConfig,
    Sources,
    run_morning,
)
from ashare_decision_support.jobs.post_close import run_post_close
from ashare_decision_support.jobs.review import run_review
from ashare_decision_support.jobs.sources import default_news_source
from ashare_decision_support.journal import Journal
from ashare_decision_support.monitor import DEFAULT_LEVELS, SECTOR_EVERY
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
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.sectors import static_taxonomies
from ashare_decision_support.state import Positions


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
    sectors: bool = True,
    sector_every: int = SECTOR_EVERY,
    calendar: TradingCalendar | None = None,
    progress: Callable[[str], Any] | None = None,
    use_llm_news: bool = False,
    **daemon_kwargs: Any,
) -> dict[str, Any]:
    today = trade_date or beijing_today()
    output = Path(output_dir)
    writer = writer_factory or (lambda: None)
    debate = debate_factory or (lambda: None)
    notifiers = notifier_factory or (lambda slug: None)

    def news_task() -> dict:
        """只把当日舆情灌进缓存，让 08:20 的主链路直接命中，不重复拉。"""
        source = _cached_news()
        docs, notes = source.fetch_market(today, limit=WIDE_MARKET_LIMIT)
        announcements, notice_notes = source.fetch_announcements(today)
        return {
            "docs": len(docs),
            "announcements": len(announcements),
            "notes": notes + notice_notes,
        }

    def _cached_news() -> Any:
        """08:00 已经把当日舆情写进缓存，后面的任务复用同一份，不再重复拉。"""
        return default_news_source(output)

    def morning_task() -> dict:
        industry, concept = static_taxonomies()
        return run_morning(
            Sources(
                adapter=adapter_factory(),
                writer=writer(),
                debate=debate(),
                notifiers=notifiers("morning"),
                global_market_adapter=GlobalMarketAdapter(),
                industry_taxonomy=industry,
                concept_taxonomy=concept,
            ),
            output,
            config=MorningConfig(
                top_sectors=top_sectors,
                candidates_per_sector=candidates_per_sector,
                max_constituents=max_constituents,
                use_llm_news=use_llm_news,
            ),
            trade_date=today,
            progress=progress,
        )

    def preopen_task() -> dict:
        positions = Positions()
        symbols = positions.symbols()
        if not symbols:
            return {"skipped": "持仓表为空，09:15 这一轮没有要算价位的票"}
        return run_advise(
            adapter=adapter_factory(),
            symbols=symbols,
            positions=positions,
            output_dir=output,
            trade_date=today,
            writer=writer(),
            news_source=_cached_news(),
            notifiers=notifiers("preopen"),
            security_master_adapter=SecurityMasterAdapter(),
            # 09:15 这一轮和 CLI 的 `advise` 同源同口径，一样要落台账。
            journal=Journal(),
            progress=progress,
        )

    def monitor_task() -> dict:
        return run_monitor(
            adapter=adapter_factory(),
            trade_date=today,
            alert_levels=alert_levels,
            news_source=_cached_news(),
            output_dir=output,
            notifiers=notifiers("alerts"),
            sectors=sectors,
            sector_every=sector_every,
        )

    def postclose_task() -> dict:
        symbols = Positions().symbols()
        if not symbols:
            return {"skipped": "持仓表为空，盘后没有要打分的票"}
        return run_post_close(
            adapter=adapter_factory(),
            symbols=symbols,
            output_dir=str(output),
            trade_date=today,
            debate=debate(),
            notifiers=notifiers("postclose"),
            security_master_adapter=SecurityMasterAdapter(),
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
