"""Intraday structure from minute bars.

Everything here describes *one session* — the most recent one present in the
bars — plus the baseline needed to judge whether that session is unusual.

The session is reconstructed from bar timestamps rather than assumed, because
the last session in the data is not always today: outside trading hours the
feed returns the previous close, and quietly treating that as live is the
mistake that makes an ultra-short signal dangerous. `is_current_session` says
which case you are in, and every consumer is expected to check it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from statistics import fmean

# A-share session boundaries.
MORNING_OPEN = time(9, 30)
SESSION_CLOSE = time(15, 0)

# Minutes after the open that define the "opening drive" window. The first half
# hour is where an ultra-short entry either works or does not.
OPENING_WINDOW_MINUTES = 30
CLOSING_WINDOW_MINUTES = 30

# Prior sessions used as the volume baseline.
VOLUME_BASELINE_SESSIONS = 5


@dataclass(frozen=True)
class IntradaySnapshot:
    symbol: str
    session_date: date
    is_current_session: bool
    bar_count: int
    open_price: float
    last_price: float
    session_high: float
    session_low: float
    vwap: float | None = None
    price_vs_open_pct: float | None = None
    price_vs_vwap_pct: float | None = None
    open_gap_pct: float | None = None
    opening_drive_pct: float | None = None
    closing_drive_pct: float | None = None
    range_position: float | None = None
    volume_ratio: float | None = None
    minutes_since_open: int = 0
    last_timestamp: datetime | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def above_vwap(self) -> bool | None:
        if self.price_vs_vwap_pct is None:
            return None
        return self.price_vs_vwap_pct >= 0


class IntradayAnalyzer:
    """Derives one `IntradaySnapshot` per symbol from minute bars."""

    def __init__(self, today: date | None = None) -> None:
        # Injectable so tests do not depend on the wall clock.
        self.today = today

    def analyze(self, symbol: str, bars: list[dict], prev_close: float | None = None) -> IntradaySnapshot | None:
        sessions = group_by_session(bars)
        if not sessions:
            return None

        session_date = max(sessions)
        session = sessions[session_date]
        closes = [bar["close"] for bar in session if bar.get("close")]
        if not closes:
            return None

        highs = [bar["high"] for bar in session if bar.get("high")] or closes
        lows = [bar["low"] for bar in session if bar.get("low")] or closes
        opens = [bar["open"] for bar in session if bar.get("open")] or closes

        today = self.today or date.today()
        notes: list[str] = []
        is_current = session_date == today
        if not is_current:
            notes.append(f"最新分钟数据为 {session_date}，非当日；结论按该日收盘状态给出，不代表盘中实时")

        open_price = opens[0]
        last_price = closes[-1]
        session_high = max(highs)
        session_low = min(lows)

        vwap = _vwap(session)
        span = session_high - session_low

        return IntradaySnapshot(
            symbol=symbol,
            session_date=session_date,
            is_current_session=is_current,
            bar_count=len(session),
            open_price=open_price,
            last_price=last_price,
            session_high=session_high,
            session_low=session_low,
            vwap=round(vwap, 4) if vwap else None,
            price_vs_open_pct=_pct(open_price, last_price),
            price_vs_vwap_pct=_pct(vwap, last_price) if vwap else None,
            open_gap_pct=_pct(prev_close, open_price) if prev_close else None,
            opening_drive_pct=_window_return(session, OPENING_WINDOW_MINUTES, from_start=True),
            closing_drive_pct=_window_return(session, CLOSING_WINDOW_MINUTES, from_start=False),
            range_position=round((last_price - session_low) / span, 4) if span > 0 else None,
            volume_ratio=_volume_ratio(sessions, session_date),
            minutes_since_open=_minutes_since_open(session[-1]["timestamp"]),
            last_timestamp=session[-1]["timestamp"],
            notes=notes,
        )

    def analyze_many(
        self,
        bars_by_symbol: dict[str, list[dict]],
        prev_closes: dict[str, float] | None = None,
    ) -> dict[str, IntradaySnapshot]:
        closes = prev_closes or {}
        result: dict[str, IntradaySnapshot] = {}
        for symbol, bars in bars_by_symbol.items():
            snapshot = self.analyze(symbol, bars, closes.get(symbol))
            if snapshot is not None:
                result[symbol] = snapshot
        return result


def group_by_session(bars: list[dict]) -> dict[date, list[dict]]:
    """Bucket bars by trading date, dropping anything without a timestamp."""
    sessions: dict[date, list[dict]] = {}
    for bar in bars:
        timestamp = bar.get("timestamp")
        if not isinstance(timestamp, datetime) or bar.get("close") is None:
            continue
        sessions.setdefault(timestamp.date(), []).append(bar)
    for rows in sessions.values():
        rows.sort(key=lambda bar: bar["timestamp"])
    return sessions


def _vwap(session: list[dict]) -> float | None:
    """Amount-weighted where the feed provides amount, volume-weighted otherwise."""
    amount_total = 0.0
    volume_total = 0.0
    for bar in session:
        volume = bar.get("volume")
        if not volume:
            continue
        amount = bar.get("amount")
        volume_total += volume
        amount_total += amount if amount else bar["close"] * volume
    if volume_total <= 0:
        return None
    return amount_total / volume_total


def _window_return(session: list[dict], minutes: int, from_start: bool) -> float | None:
    """Return across the first (or last) `minutes` of the session."""
    if len(session) < 2:
        return None
    anchor = session[0]["timestamp"] if from_start else session[-1]["timestamp"]
    if from_start:
        window = [bar for bar in session if (bar["timestamp"] - anchor).total_seconds() <= minutes * 60]
    else:
        window = [bar for bar in session if (anchor - bar["timestamp"]).total_seconds() <= minutes * 60]
    if len(window) < 2:
        return None
    start = window[0].get("open") or window[0]["close"]
    return _pct(start, window[-1]["close"])


def _volume_ratio(sessions: dict[date, list[dict]], session_date: date) -> float | None:
    """Session volume against the mean of the preceding sessions."""
    ordered = sorted(sessions)
    index = ordered.index(session_date)
    baseline_dates = ordered[max(0, index - VOLUME_BASELINE_SESSIONS) : index]
    if not baseline_dates:
        return None

    def total(day: date) -> float:
        return sum(bar.get("volume") or 0.0 for bar in sessions[day])

    baseline = fmean([total(day) for day in baseline_dates])
    if baseline <= 0:
        return None
    current = total(session_date)
    # A partial session compared against full ones reads low by construction;
    # scale the baseline to the fraction of the day elapsed.
    elapsed = _minutes_since_open(sessions[session_date][-1]["timestamp"])
    full_day = 240
    if 0 < elapsed < full_day:
        baseline *= elapsed / full_day
    return round(current / baseline, 4) if baseline > 0 else None


def _minutes_since_open(timestamp: datetime) -> int:
    """Trading minutes elapsed, skipping the 11:30-13:00 break."""
    moment = timestamp.time()
    if moment <= MORNING_OPEN:
        return 0
    morning_end = time(11, 30)
    afternoon_start = time(13, 0)
    if moment <= morning_end:
        return int((_seconds(moment) - _seconds(MORNING_OPEN)) // 60)
    if moment <= afternoon_start:
        return 120
    capped = min(moment, SESSION_CLOSE)
    return 120 + int((_seconds(capped) - _seconds(afternoon_start)) // 60)


def _seconds(moment: time) -> int:
    return moment.hour * 3600 + moment.minute * 60 + moment.second


def _pct(base: float | None, value: float) -> float | None:
    if not base or base <= 0:
        return None
    return round(value / base - 1.0, 6)
