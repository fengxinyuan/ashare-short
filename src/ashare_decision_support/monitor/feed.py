"""The master switch: is the minute feed current enough to act on?

When a human runs `advise` and sees the "非当日数据" banner, they apply
judgement. An automated push has no such step, so **an alert computed from
yesterday's bars is worse than no alert at all** — it looks exactly like a live
signal and there is nothing in the message to say otherwise.

Every monitor pass therefore starts here, not at the data fetch. The gate can
say three things:

    fresh     act normally
    degraded  keep alerting, but stamp the lag on every message
    stale     stop producing signals; keep polling to detect recovery

"Stop producing signals" is not "stop running". The loop stays alive, keeps
checking, and announces recovery — a monitor that exits on a data hiccup is one
you find out about at 14:50.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median

FRESH_LAG_MINUTES = 5.0
DEGRADED_LAG_MINUTES = 15.0

VERDICT_FRESH = "fresh"
VERDICT_DEGRADED = "degraded"
VERDICT_STALE = "stale"
VERDICT_CLOSED = "closed"


@dataclass(frozen=True)
class FeedHealth:
    verdict: str
    lag_minutes: float | None
    session_date: date | None
    latest: datetime | None
    symbols_ok: int
    symbols_requested: int
    reason: str
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True when alerts derived from this data are safe to publish."""
        return self.verdict in (VERDICT_FRESH, VERDICT_DEGRADED)

    @property
    def failure_ratio(self) -> float:
        if not self.symbols_requested:
            return 0.0
        return 1.0 - self.symbols_ok / self.symbols_requested


def assess_feed(
    bars_by_symbol: dict[str, list[dict]],
    requested: int,
    now: datetime,
    previous_latest: datetime | None = None,
    in_session: bool = True,
) -> FeedHealth:
    """Judge one pass's data. `previous_latest` is the prior pass's newest bar."""
    latest_per_symbol = [
        bars[-1]["timestamp"]
        for bars in bars_by_symbol.values()
        if bars and isinstance(bars[-1].get("timestamp"), datetime)
    ]
    ok = len(bars_by_symbol)

    if not latest_per_symbol:
        return FeedHealth(
            verdict=VERDICT_STALE if in_session else VERDICT_CLOSED,
            lag_minutes=None,
            session_date=None,
            latest=None,
            symbols_ok=ok,
            symbols_requested=requested,
            reason=(
                "没有任何标的返回可用分钟线"
                if in_session
                else "当前不在交易时段，未取得分钟线；开盘后重新检查"
            ),
        )

    latest = max(latest_per_symbol)
    # Median of the *lags*, not of the timestamps: averaging two datetimes is a
    # TypeError, so a median over an even number of symbols would crash every
    # pass. Median rather than max because one symbol that happens to update
    # promptly must not vouch for a feed that is stale for everything else.
    lag = round(
        max(0.0, median((now - stamp).total_seconds() / 60.0 for stamp in latest_per_symbol)), 1
    )
    session_date = latest.date()

    if not in_session:
        return FeedHealth(
            verdict=VERDICT_CLOSED,
            lag_minutes=lag,
            session_date=session_date,
            latest=latest,
            symbols_ok=ok,
            symbols_requested=requested,
            reason=f"当前不在交易时段，最新数据为 {session_date}，不产出盘中信号",
        )

    if session_date != now.date():
        return FeedHealth(
            verdict=VERDICT_STALE,
            lag_minutes=lag,
            session_date=session_date,
            latest=latest,
            symbols_ok=ok,
            symbols_requested=requested,
            reason=f"最新分钟线属于 {session_date}，不是当日数据",
        )

    # Not advancing is the failure mode a lag threshold alone misses: a feed
    # frozen at 10:31 reads as 4-minute lag at 10:35 and 6 minutes at 10:37,
    # crossing the threshold slowly instead of failing loudly.
    if previous_latest is not None and latest <= previous_latest:
        return FeedHealth(
            verdict=VERDICT_STALE,
            lag_minutes=lag,
            session_date=session_date,
            latest=latest,
            symbols_ok=ok,
            symbols_requested=requested,
            reason=f"连续两轮最新时间戳都停在 {latest:%H:%M}，数据源未推进",
        )

    if lag <= FRESH_LAG_MINUTES:
        verdict, reason = VERDICT_FRESH, f"最新数据 {latest:%H:%M}，滞后 {lag:.1f} 分钟"
    elif lag <= DEGRADED_LAG_MINUTES:
        verdict, reason = (
            VERDICT_DEGRADED,
            f"最新数据 {latest:%H:%M}，滞后 {lag:.1f} 分钟，告警照发但请核对时间戳",
        )
    else:
        verdict, reason = (
            VERDICT_STALE,
            f"最新数据 {latest:%H:%M}，滞后 {lag:.1f} 分钟，超过 {DEGRADED_LAG_MINUTES:.0f} 分钟阈值",
        )

    notes = []
    if requested and ok < requested:
        notes.append(f"{requested - ok}/{requested} 只标的本轮取数失败")
    return FeedHealth(
        verdict=verdict,
        lag_minutes=lag,
        session_date=session_date,
        latest=latest,
        symbols_ok=ok,
        symbols_requested=requested,
        reason=reason,
        notes=notes,
    )
