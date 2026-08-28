"""兑现核对：建议发出后 N 个交易日，实际发生了什么。

核对只用**相对值**。建议里的绝对价来自不复权分钟线，日线是前复权的，两者不能
直接比；百分比对复权免疫，所以把百分比套到前复权序列上是唯一正确的做法。

顺带得到的性质是对的：前复权抹掉除息跳空，MFE/MAE 因此是全收益口径——一只票
除息当天跌 3%，不该记成策略亏了 3%。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ashare_decision_support.adapters.base import fetch_daily_bars
from ashare_decision_support.journal.records import KIND_RECOMMENDATION, Outcome
from ashare_decision_support.journal.store import Journal
from ashare_decision_support.numeric import finite
from ashare_decision_support.schedule.timezone import beijing_from_epoch, beijing_today

DEFAULT_HORIZON_SESSIONS = 5

# 日历天数要比交易日多给，覆盖周末与假期。
CALENDAR_SLACK = 3.0


def verify_pending(
    journal: Journal,
    adapter: Any,
    today: date | None = None,
    horizon: int = DEFAULT_HORIZON_SESSIONS,
    min_age_days: int = 1,
) -> dict[str, Any]:
    """核对所有待兑现的建议，把结果写回台账。"""
    now = today or beijing_today()
    pending = journal.pending(now, min_age_days=min_age_days)
    if not pending:
        return {"checked": 0, "resolved": 0, "outcomes": [], "notes": ["没有待核对的建议"]}

    symbols = sorted({row["symbol"] for row in pending if row.get("symbol")})
    earliest = min(_as_date(row["trade_date"]) for row in pending)
    start = earliest - timedelta(days=10)
    notes: list[str] = []
    try:
        bars = fetch_daily_bars(adapter, symbols, start, now, notes)
    except Exception as exc:
        return {
            "checked": len(pending),
            "resolved": 0,
            "outcomes": [],
            "notes": [f"取日线失败，本次不核对：{type(exc).__name__}: {exc}"],
        }

    outcomes: list[Outcome] = []
    for row in pending:
        rows = bars.get(row["symbol"]) or []
        outcome = _resolve(row, rows, now, horizon)
        if outcome is None:
            continue
        outcomes.append(outcome)

    journal.record_many(list(outcomes))
    skipped = len(pending) - len(outcomes)
    if skipped:
        notes.append(f"{skipped} 条建议数据不足或未满 {horizon} 个交易日，留待下次核对")
    return {
        "checked": len(pending),
        "resolved": len(outcomes),
        "outcomes": [item.to_dict() for item in outcomes],
        "notes": notes,
    }


def _resolve(row: dict, bars: list[dict], today: date, horizon: int) -> Outcome | None:
    trade_date = _as_date(row.get("trade_date"))
    if trade_date is None or not bars:
        return None

    dated = _dated_bars(bars)
    anchor = next(
        (bar for bar, stamp in reversed(dated) if stamp <= trade_date),
        None,
    )
    if anchor is None or not anchor.get("close"):
        return None
    base = float(anchor["close"])

    forward = [bar for bar, stamp in dated if stamp > trade_date][:horizon]
    if not forward:
        return None

    # 未满窗口就只在到期后才定案，避免用 1 天的数据去代表 5 天的结论。
    mature = len(forward) >= horizon or (today - trade_date).days > horizon * CALENDAR_SLACK
    if not mature:
        return None

    entry_high = _level(row.get("entry_high_pct"), base)
    stop = _level(row.get("stop_pct"), base)
    targets = [_level(pct, base) for pct in (row.get("target_pcts") or [])]
    first_target = min((level for level in targets if level is not None), default=None)

    entered = False
    stop_hit = False
    target_hit = False
    first_hit: str | None = None
    highs: list[float] = []
    lows: list[float] = []

    for bar in forward:
        high, low = bar.get("high"), bar.get("low")
        if high is None or low is None:
            continue
        highs.append(float(high))
        lows.append(float(low))
        if entry_high is not None and float(low) <= entry_high:
            entered = True
        if stop is not None and float(low) <= stop:
            stop_hit = True
            if first_hit is None:
                first_hit = "stop"
        if first_target is not None and float(high) >= first_target:
            target_hit = True
            if first_hit is None:
                first_hit = "target"

    if not highs or not lows:
        return None

    last_close = next(
        (float(bar["close"]) for bar in reversed(forward) if bar.get("close")), None
    )

    # T+1 口径。forward[0] 就是建议发出后的第一个交易日——「买入后第二天卖」
    # 这条策略的全部收益都落在这一根 bar 里，而 5 日 MFE/MAE 会把它稀释掉。
    # 开盘单独取：持仓过夜的收益里隔夜跳空常常比日内择时贡献更大，两者不拆开
    # 就无法归因。
    t1 = forward[0]
    return Outcome(
        symbol=row["symbol"],
        trade_date=str(trade_date),
        source=row.get("source") or "unknown",
        resolved_on=str(today),
        sessions=len(forward),
        entered=entered,
        stop_hit=stop_hit,
        target_hit=target_hit,
        first_hit=first_hit,
        mfe_pct=round(max(highs) / base - 1.0, 6),
        mae_pct=round(min(lows) / base - 1.0, 6),
        close_pct=None if last_close is None else round(last_close / base - 1.0, 6),
        next_open_pct=_relative(t1.get("open"), base),
        next_high_pct=_relative(t1.get("high"), base),
        next_low_pct=_relative(t1.get("low"), base),
        next_close_pct=_relative(t1.get("close"), base),
        score=row.get("score"),
        sector=row.get("sector"),
        trend_state=row.get("trend_state"),
        wyckoff_bias=row.get("wyckoff_bias"),
        playbook=row.get("playbook"),
        rec_kind=row.get("kind") or KIND_RECOMMENDATION,
        dip=row.get("dip") or {},
        market_state=row.get("market_state") or {},
        trade_flow=row.get("trade_flow") or {},
        adjudication=row.get("adjudication") or {},
        macro_context=row.get("macro_context") or {},
    )


def _relative(value: Any, base: float) -> float | None:
    number = finite(value)
    if number is None or not base:
        return None
    return round(number / base - 1.0, 6)


def _level(pct: Any, base: float) -> float | None:
    number = finite(pct)
    return None if number is None else base * (1.0 + number)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            if timestamp > 1e12:
                timestamp /= 1000.0
            return beijing_from_epoch(timestamp).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None
    try:
        return date.fromisoformat(text[:10].replace("/", "-"))
    except (TypeError, ValueError):
        return None


def _bar_date(bar: dict[str, Any]) -> date | None:
    """Read the date contract shared by daily adapters.

    Most adapters return normalized rows with ``timestamp``. Older fixtures and
    a few external sources use ``date`` or Chinese aliases, so verification must
    accept the same field family as the rest of the daily-bar pipeline.
    """
    for key in ("timestamp", "date", "datetime", "日期", "时间"):
        stamp = _as_date(bar.get(key))
        if stamp is not None:
            return stamp
    return None


def _dated_bars(bars: list[dict]) -> list[tuple[dict, date]]:
    dated: list[tuple[dict, date]] = []
    for bar in bars:
        stamp = _bar_date(bar)
        if stamp is not None:
            dated.append((bar, stamp))
    dated.sort(key=lambda item: item[1])
    return dated
