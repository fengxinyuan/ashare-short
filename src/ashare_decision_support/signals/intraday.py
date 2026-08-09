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

# 一个完整交易日的分钟数（上午 120 + 下午 120）。`_minutes_since_open` 的上界。
FULL_SESSION_MINUTES = 240


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
    # 现价相对**昨收**——也就是行情软件上那个「涨幅」。此前只能由
    # `open_gap_pct` 与 `price_vs_open_pct` 两个数复合还原，每个调用方各还原一次
    # 就是各错一次的机会。尾盘打法的第一道筛（涨幅 3%–5%）直接读它。
    day_change_pct: float | None = None
    opening_drive_pct: float | None = None
    closing_drive_pct: float | None = None
    # 与 `closing_drive_pct` 成对：那个说尾盘这段涨了多少，这个说是放着量涨的还是
    # 缩着量涨的。单看涨幅，「尾盘缓拉吸筹」与「尾盘急拉做 K 线」长得一模一样。
    closing_volume_ratio: float | None = None
    range_position: float | None = None
    volume_ratio: float | None = None
    session_volume: float = 0.0
    session_amount: float | None = None
    minutes_since_open: int = 0
    last_timestamp: datetime | None = None
    notes: list[str] = field(default_factory=list)


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
        session_volume = sum(
            float(bar.get("volume") or 0.0)
            for bar in session
        )
        session_amount = sum(
            float(bar.get("amount") or ((bar.get("close") or 0.0) * (bar.get("volume") or 0.0)))
            for bar in session
            if bar.get("volume")
        )

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
            closing_volume_ratio=_closing_volume_ratio(sessions, session_date),
            range_position=round((last_price - session_low) / span, 4) if span > 0 else None,
            volume_ratio=_volume_ratio(sessions, session_date),
            session_volume=round(session_volume, 4),
            session_amount=round(session_amount, 4) if session_amount > 0 else None,
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
        amount_total += amount or bar["close"] * volume
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
    """Session volume against the preceding sessions **at the same point of day**.

    这里原先拿基线日的*全天*量按 `elapsed / 240` 线性折算。A 股成交显著前置——
    开盘那几分钟经常单根就占掉全天两成——所以线性折算等于假设成交在 240 分钟里
    均匀发生，早盘会系统性高估。

    2026-08-04 牧原股份实测：10:50（elapsed=80）读数 **2.36**，当日收盘实际
    64.8 万手 / 前 5 日均量 59.7 万手 = **1.09**。高估了一倍以上，而这个数还被
    `advice/` 写成「分钟量能比前 5 日 2.36，资金参与偏强」喂进了结论文案。

    改成拿基线日在**同一分钟数**上的累计量作分母。基线日的分钟线本来就在手上，
    不需要外部的日内分布曲线，而且自动带上了这只票自己的成交节奏。

    **这个数仍然是个外推，不是全天定论。** 它等价于「假设余下时段按常态节奏走」
    的全天量比——今天恰好是反例：开盘砸完之后整个下午近乎停摆，10:50 的 1.7 倍
    最终落到 1.09 倍。所以盘中读数只说「到此刻为止的量偏大」，不说「今天会放量」。
    """
    ordered = sorted(sessions)
    index = ordered.index(session_date)
    baseline_dates = ordered[max(0, index - VOLUME_BASELINE_SESSIONS) : index]
    if not baseline_dates:
        return None

    current_session = sessions[session_date]
    elapsed = _minutes_since_open(current_session[-1]["timestamp"])
    current = sum(bar.get("volume") or 0.0 for bar in current_session)

    def total(day: date, cutoff: int | None) -> float:
        rows = sessions[day]
        if cutoff is not None:
            rows = [bar for bar in rows if _minutes_since_open(bar["timestamp"]) <= cutoff]
        return sum(bar.get("volume") or 0.0 for bar in rows)

    # `elapsed == 0`（开盘第一根）也要走 cutoff：拿基线日的第一根比，读数才是 1.0。
    # 原实现里的 `0 < elapsed` 守卫是给线性公式防除零的（`baseline *= 0/240` 会归零），
    # cutoff 口径下这个风险由下面的 `> 0` 过滤兜住，守卫反而会让首根回退到全天基线。
    cutoff = elapsed if elapsed < FULL_SESSION_MINUTES else None
    # 基线日在该时点前没有任何成交（数据缺口、停牌半天）时整条丢掉，而不是让一个
    # 0 把均值拖下去——那会把量比放大成一个看起来很吓人的数。
    baselines = [value for day in baseline_dates if (value := total(day, cutoff)) > 0]
    if not baselines:
        return None
    baseline = fmean(baselines)
    return round(current / baseline, 4) if baseline > 0 else None


def _closing_volume_ratio(sessions: dict[date, list[dict]], session_date: date) -> float | None:
    """最近 `CLOSING_WINDOW_MINUTES` 分钟的成交量，对前几日**同一分钟区间**的均值。

    这是 `closing_drive_pct` 缺的那一半。§2.3 那张尾盘意图表的四行里，头两行的
    涨幅可以完全一样——「缓步拉升 + 量同步放大」是吸筹，「急拉冲高 + 量萎缩」是
    为次日出货做 K 线——**区别全在量**。没有这个读数，两者在系统里不可分。

    ## 分母为什么不用「当日分钟均量」

    A 股日内成交是 U 形的，尾盘天然抬升。拿全天分钟均量做分母，几乎每只票都会读出
    大于 1 的值，阈值因此失去分辨力。这和 `_volume_ratio` 当初弃用 `elapsed / 240`
    线性折算是**同一个错误的两面**：都是拿一条「日内均匀分布」的假设，去比一个本来
    就不均匀的东西。改用前几日同一分钟区间，基线自动带上这只票自己的尾盘节奏。

    ## 窗口锚在最后一根 K 线，不是 15:00

    盘中 14:00 调用，得到的是 13:30–14:00 那一段。**它是不是「尾盘」由调用方按时钟
    判断**——同 `is_current_session` 的契约，也同 `closing_drive_pct` 现有的行为。
    未来的 `Playbook.TAIL_END` 应当由 `schedule/timetable.py` 的时段来保证这件事，
    而不是靠这里猜。

    窗口不足 `CLOSING_WINDOW_MINUTES` 时返回 None：开盘 10 分钟时「最近 30 分钟」
    并不存在，给一个数会让它冒充尾盘读数。
    """
    ordered = sorted(sessions)
    index = ordered.index(session_date)
    baseline_dates = ordered[max(0, index - VOLUME_BASELINE_SESSIONS) : index]
    if not baseline_dates:
        return None

    current_session = sessions[session_date]
    elapsed = _minutes_since_open(current_session[-1]["timestamp"])
    if elapsed < CLOSING_WINDOW_MINUTES:
        return None
    start = elapsed - CLOSING_WINDOW_MINUTES

    def window(day: date) -> float:
        return sum(
            bar.get("volume") or 0.0
            for bar in sessions[day]
            if start < _minutes_since_open(bar["timestamp"]) <= elapsed
        )

    # 当日窗口零成交按「无读数」处理，不按「尾盘萎缩到 0」。交易时段里连续 30 分钟
    # 没有成交，现实中是停牌或数据缺口，不是行情——而 0.0 会直接落进「急拉缩量 =
    # 诱多」那一行。基线侧丢零值是同一条理由（见 `_volume_ratio`）。
    current = window(session_date)
    if current <= 0:
        return None
    baselines = [value for day in baseline_dates if (value := window(day)) > 0]
    if not baselines:
        return None
    baseline = fmean(baselines)
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
