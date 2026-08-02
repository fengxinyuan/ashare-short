"""盘中监控任务：把 `monitor/` 那套接到自选股、候选池与推送通道上。

`monitor/` 负责判断（时效闸门、跃迁触发、降频），这里负责取名单、建会话、决定
跑一轮还是跑一天，以及**把当日公告喂进去**——`risk_notice` 那条 WARN 触发器在
代码里一直存在，但在此之前没有任何调用点给它数据，等于结构上永不触发。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ashare_decision_support.adapters.freshness import in_session
from ashare_decision_support.adapters.tickflow_intraday import build_intraday_adapter
from ashare_decision_support.monitor import DEFAULT_LEVELS, MonitorSession, run_monitor_loop
from ashare_decision_support.monitor.session import MONITOR_PERIOD
from ashare_decision_support.news import AkshareNewsSource, NewsCache
from ashare_decision_support.notify import ConsoleNotifier
from ashare_decision_support.notify.base import Notifier
from ashare_decision_support.notify.render import render_alerts, render_feed_suspension
from ashare_decision_support.state import CandidatePool, Watchlist


def run_monitor(
    adapter: Any,
    symbols: list[str] | None = None,
    period: str = MONITOR_PERIOD,
    once: bool = False,
    max_passes: int | None = None,
    alert_levels: tuple[str, ...] = DEFAULT_LEVELS,
    notifiers: list[Notifier] | None = None,
    intraday_adapter: Any | None = None,
    trade_date: date | None = None,
    now: datetime | None = None,
    news_source: Any | None = None,
    news_cache_dir: str | None = None,
) -> dict[str, Any]:
    today = trade_date or date.today()
    moment = now or datetime.now()
    channels = notifiers if notifiers is not None else [ConsoleNotifier()]

    # 显式给了标的就只盯它；否则自选股 + 当日候选池。
    if symbols:
        watch, candidates = list(symbols), []
    else:
        watch = Watchlist().symbols()
        pool = CandidatePool(today=today)
        candidates = [item for item in pool.symbols() if item not in set(watch)]

    if not watch and not candidates:
        return {
            "trade_date": str(today),
            "watch": 0,
            "candidates": 0,
            "passes": 0,
            "alerts": 0,
            "notes": ["自选股与候选池都为空，先用 `ashare-ds watchlist add <代码>` 加入标的"],
        }

    source = intraday_adapter or build_intraday_adapter(adapter)
    session = MonitorSession(
        adapter=adapter,
        intraday_adapter=source,
        session_date=today,
        levels=alert_levels,
        period=period,
    )
    notes = session.prime(watch + candidates)

    news = news_source if news_source is not None else _default_news_source(news_cache_dir)
    blocked, risk_notes = _refresh_risk(session, news, today)
    notes.extend(risk_notes)

    if once or max_passes == 1:
        result = session.poll(watch, candidates, moment, in_session=in_session(moment))
        notes.extend(result.notes)
        if result.alerts:
            content = render_alerts(result.alerts, result.health)
            for channel in channels:
                channel.send(f"盘中告警 {len(result.alerts)} 条", content)
        elif not result.health.usable:
            for channel in channels:
                channel.send("分时监控状态", render_feed_suspension(result.health))
        return {
            "trade_date": str(today),
            "watch": len(watch),
            "candidates": len(candidates),
            "passes": 1,
            "alerts": len(result.alerts),
            "risk_blocked": blocked,
            "feed_verdict": result.health.verdict,
            "feed_reason": result.health.reason,
            "as_of": result.health.latest.isoformat(timespec="seconds") if result.health.latest else None,
            "alert_list": [_alert_dict(item) for item in result.alerts],
            "notes": notes,
        }

    stats = run_monitor_loop(
        session=session,
        # 每轮重读，这样盘中 `watchlist add` 立刻生效，不用重启监控。
        watch_symbols=lambda: (list(symbols) if symbols else Watchlist().symbols()),
        candidate_symbols=lambda: ([] if symbols else CandidatePool(today=today).symbols()),
        notifiers=channels,
        max_passes=max_passes,
        # 盘中会新挂临时公告，所以不能只在开盘时读一次；但公告源是整日全量拉取，
        # 每轮都拉是浪费，走慢周期。
        refresh_risk=(lambda: _refresh_risk(session, news, today)[1]) if news is not None else None,
    )
    return {
        "trade_date": str(today),
        "watch": len(watch),
        "candidates": len(candidates),
        "passes": stats.passes,
        "alerts": stats.alerts,
        "risk_blocked": blocked,
        "bad_passes": stats.bad_passes,
        "suspensions": stats.suspensions,
        "interval_seconds": stats.interval_seconds,
        "notes": notes + stats.notes,
    }


def _default_news_source(cache_dir: str | None) -> Any | None:
    try:
        return AkshareNewsSource(cache=NewsCache(cache_dir) if cache_dir else None)
    except RuntimeError:
        return None


def _refresh_risk(session: MonitorSession, news_source: Any | None, today: date) -> tuple[int, list[str]]:
    """把当日公告里的否决项喂给监控会话。

    取不到公告不是错误，是降级：说清楚这一轮没有公告否决输入，然后继续跑。沉默地
    跑一个永远不触发的告警，比明说「这条输入缺了」糟糕得多。
    """
    if news_source is None or not hasattr(news_source, "fetch_announcements"):
        return 0, ["公告源不可用，本次监控不含突发风险公告告警"]
    try:
        announcements, notes = news_source.fetch_announcements(today)
    except Exception as exc:
        return 0, [f"公告拉取失败（{type(exc).__name__}），本次监控不含突发风险公告告警"]

    blocked = session.refresh_risk(announcements)
    messages = list(notes or [])
    if blocked:
        messages.append(f"当日公告命中否决项 {blocked} 只，命中的标的会推一条风险告警")
    return blocked, messages


def _alert_dict(alert) -> dict:
    return {
        "symbol": alert.symbol,
        "name": alert.name,
        "level": alert.level,
        "trigger": alert.trigger,
        "headline": alert.headline,
        "detail": alert.detail,
        "as_of": alert.as_of.isoformat(timespec="seconds") if alert.as_of else None,
        "lag_minutes": alert.lag_minutes,
        "watchlist": alert.watchlist,
    }
