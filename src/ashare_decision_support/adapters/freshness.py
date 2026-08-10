"""Measure how fresh the free intraday feed actually is.

Written to answer one question that cannot be answered outside trading hours:
**does the Sina minute feed update during the session, and by how much does it
lag?** The whole free-tier case for intraday advice rests on it — if the answer
is "it lags badly" or "it does not move", the paid TickFlow tier is the only
route to requirements 2 and 3, and there is no point building further on the
free path.

Run it during a session (09:30-15:00 on a trading day). Outside those hours it
says so and refuses to guess.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from ashare_decision_support.schedule.clock import (
    AFTERNOON_CLOSE,
    AFTERNOON_OPEN,
    MORNING_CLOSE,
    MORNING_OPEN,
)
from ashare_decision_support.schedule.timezone import beijing_naive_now

# Two samples this far apart. Long enough that a live feed must have produced a
# new 1-minute bar, short enough to run while you watch.
DEFAULT_GAP_SECONDS = 90

# Lag beyond which the feed cannot support an intraday entry decision.
UNUSABLE_LAG_MINUTES = 5


def in_session(moment: datetime) -> bool:
    current = moment.time()
    if moment.weekday() >= 5:
        return False
    return (MORNING_OPEN <= current <= MORNING_CLOSE) or (
        AFTERNOON_OPEN <= current <= AFTERNOON_CLOSE
    )


def check_intraday_freshness(
    intraday_adapter: Any,
    symbols: list[str],
    period: str = "1",
    gap_seconds: int = DEFAULT_GAP_SECONDS,
    now: datetime | None = None,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Sample the feed twice and report lag plus whether it advanced."""
    moment = now or beijing_naive_now()
    session_active = in_session(moment)

    first, notes = intraday_adapter.get_minute_bars(symbols, period=period)
    lag_by_symbol: dict[str, float | None] = {}
    for symbol, bars in first.items():
        last = bars[-1]["timestamp"] if bars else None
        lag_by_symbol[symbol] = (
            round((moment - last).total_seconds() / 60.0, 1) if isinstance(last, datetime) else None
        )

    advanced: dict[str, bool | None] = {}
    if session_active and gap_seconds > 0:
        sleep(gap_seconds)
        second, _ = intraday_adapter.get_minute_bars(symbols, period=period)
        for symbol, bars in first.items():
            new_bars = second.get(symbol) or []
            if not bars or not new_bars:
                advanced[symbol] = None
                continue
            advanced[symbol] = new_bars[-1]["timestamp"] > bars[-1]["timestamp"]

    lags = [value for value in lag_by_symbol.values() if value is not None]
    median_lag = sorted(lags)[len(lags) // 2] if lags else None

    return {
        "checked_at": moment.isoformat(timespec="seconds"),
        "in_session": session_active,
        "period": period,
        "gap_seconds": gap_seconds if session_active else 0,
        "lag_minutes": lag_by_symbol,
        "median_lag_minutes": median_lag,
        "advanced_within_gap": advanced,
        "verdict": _verdict(session_active, median_lag, advanced),
        "notes": notes,
    }


def _verdict(session_active: bool, median_lag: float | None, advanced: dict[str, bool | None]) -> str:
    if not session_active:
        return (
            "非交易时段，测不出盘中时效。请在交易日 09:30-11:30 或 13:00-15:00 重跑；"
            "此时返回的 lag 只反映距上一个收盘的自然时间，不代表数据延迟。"
        )
    if median_lag is None:
        return "拿不到任何分钟数据，免费分钟线在本机不可用——需求 2/3 只能走付费档或 QMT。"

    moved = [value for value in advanced.values() if value is not None]
    did_move = bool(moved) and any(moved)

    if median_lag <= 2 and did_move:
        return f"中位延迟 {median_lag} 分钟且样本内出新数据——免费分钟线可支撑盘中判断，暂不需要付费。"
    if median_lag <= UNUSABLE_LAG_MINUTES and did_move:
        return (
            f"中位延迟 {median_lag} 分钟，数据在动但偏慢。做 5 分钟以上级别的判断够用，"
            "抢分时买点不够——按你的持仓周期决定是否付费。"
        )
    if not did_move:
        return (
            "采样窗口内数据**没有前进**——免费分钟线盘中不更新，"
            "需求 2/3 无法基于它实现，建议开通付费档或等 QMT。"
        )
    return (
        f"中位延迟 {median_lag} 分钟，超过 {UNUSABLE_LAG_MINUTES} 分钟可用线——"
        "盘中决策不可靠，建议开通付费档。"
    )
