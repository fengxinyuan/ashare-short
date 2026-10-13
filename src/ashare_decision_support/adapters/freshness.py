"""采样所选分钟线接口的时效与更新情况，供免费和付费数据源共同使用。

交易时段比较两次采样的时间戳；非交易时段只报告距最近数据的时间差，
不据此判断盘中延迟。有限次采样不能证明全天的稳定性。
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
    ahead: list[str] = []
    for symbol, bars in first.items():
        last = bars[-1]["timestamp"] if bars else None
        if isinstance(last, datetime):
            raw_lag = (moment - last).total_seconds() / 60.0
            lag_by_symbol[symbol] = round(max(0.0, raw_lag), 1)
            if raw_lag < 0:
                ahead.append(symbol)
        else:
            lag_by_symbol[symbol] = None
    if ahead:
        notes.append(
            f"{len(ahead)} 只标的分钟线时间戳晚于采样时刻，延迟按 0 分钟显示；"
            "可能使用分钟区间结束时间，请结合连续更新判断"
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


def _verdict(
    session_active: bool, median_lag: float | None, advanced: dict[str, bool | None]
) -> str:
    if not session_active:
        return (
            "非交易时段，测不出盘中时效。请在交易日 09:30-11:30 或 13:00-15:00 重跑；"
            "此时返回的 lag 只反映距上一个收盘的自然时间，不代表数据延迟。"
        )
    if median_lag is None:
        return "拿不到任何分钟数据，无法评估盘中时效；请检查所选接口的连接、权限和配额。"

    moved = [value for value in advanced.values() if value is not None]
    did_move = bool(moved) and any(moved)

    if median_lag <= 2 and did_move:
        return f"中位延迟 {median_lag} 分钟且样本内出新数据，本次采样确认分钟线正在更新。"
    if median_lag <= UNUSABLE_LAG_MINUTES and did_move:
        return (
            f"中位延迟 {median_lag} 分钟，数据在动但偏慢。做 5 分钟以上级别的判断够用，"
            "短周期判断仍需核对最新数据。"
        )
    if not did_move:
        return (
            "采样窗口内数据**没有前进**，本轮未确认分钟线更新；"
            "请检查源数据时点，并用 --source 对比可用的分钟线接口。"
        )
    return (
        f"中位延迟 {median_lag} 分钟，超过 {UNUSABLE_LAG_MINUTES} 分钟可用线——"
        "盘中决策不可靠，请检查源数据时点并对比其他可用接口。"
    )
