"""One monitoring session: prime once, then poll repeatedly.

Daily structure — trend, support, resistance, Wyckoff phase — changes once per
day, so it is computed at `prime()` and reused. Each `poll()` then only fetches
minute bars, which is the part that has to be fast.

The session owns the freshness gate and the alert state. It emits alerts; it
does not decide when to run or where to send them (that is `loop.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ashare_decision_support.advice.engine import AdviceEngine, StockAdvice
from ashare_decision_support.monitor.feed import FeedHealth, assess_feed
from ashare_decision_support.monitor.holdings import HoldingView
from ashare_decision_support.monitor.triggers import DEFAULT_LEVELS, Alert, TriggerEngine
from ashare_decision_support.screens import AnnouncementScreen
from ashare_decision_support.signals import DailyTechnicalAnalyzer, WyckoffAnalyzer
from ashare_decision_support.signals.intraday import IntradayAnalyzer
from ashare_decision_support.state.alerts import AlertState
from ashare_decision_support.symbols import bare_code, normalize_symbol

DAILY_LOOKBACK_DAYS = 240

# Minute granularity for the monitor. 1-minute bars give ~8 sessions of
# history, which covers the 5-session volume baseline with room to spare.
MONITOR_PERIOD = "1"

# Bars into the session before stop and targets are frozen. Levels derived from
# the first few minutes describe a range that barely exists yet; a quarter of an
# hour is enough for the session to have a shape.
ARM_MIN_BARS = 15


@dataclass
class PassResult:
    """What one poll produced."""

    health: FeedHealth
    alerts: list[Alert] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    polled: int = 0
    verdict_changed: bool = False


class MonitorSession:
    def __init__(
        self,
        adapter: Any,
        intraday_adapter: Any,
        session_date: date,
        levels: tuple[str, ...] = DEFAULT_LEVELS,
        period: str = MONITOR_PERIOD,
        alert_state: AlertState | None = None,
    ) -> None:
        self.adapter = adapter
        self.intraday_adapter = intraday_adapter
        self.session_date = session_date
        self.period = period
        self.engine = TriggerEngine(levels=levels)
        self.advice_engine = AdviceEngine()
        self.state = alert_state or AlertState.load(session_date)

        self.snapshots: dict[str, Any] = {}
        self.wyckoff: dict[str, Any] = {}
        self.names: dict[str, str | None] = {}
        self.prev_closes: dict[str, float] = {}
        self.risk_notes: dict[str, str] = {}
        # 持仓视图。空字典意味着「这一轮没有任何持仓」，而不是「还没加载」——
        # 加载失败会写进 note，不会静默退化成「你什么都没持有」。
        self.holdings: dict[str, HoldingView] = {}

        self._previous_latest: datetime | None = None
        self._last_verdict: str | None = None

    # ---- once per session -------------------------------------------------

    def load_positions(self, watchlist: Any) -> list[str]:
        """读入持仓状态。**已持仓的票要盯的是卖点，不是买点。**

        持仓由人手工维护（`watchlist hold`）——系统不下单、不接管账户，不可能
        自己知道你买了什么。传 None 表示这一轮不带持仓上下文，所有标的都按
        「未持仓、盯买点」处理。
        """
        self.holdings = {}
        if watchlist is None:
            return []
        notes: list[str] = []
        for entry in getattr(watchlist, "entries", []) or []:
            view = HoldingView.from_entry(entry, today=self.session_date)
            if view is None:
                continue
            self.holdings[view.symbol] = view
            if not view.cost_price:
                notes.append(f"{view.symbol} 标为持仓但没有成本价，只能报绝对价位，给不出浮盈判断")
        if self.holdings:
            notes.append(
                f"持仓 {len(self.holdings)} 只，按卖点口径监控"
                "（止损 / 目标 / 结构破坏 / 时间止损）"
            )
        return notes

    def prime(self, symbols: list[str]) -> list[str]:
        """Fetch and analyse daily structure for every symbol we will watch."""
        codes = [normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)]
        if not codes:
            return ["监控列表为空"]

        notes: list[str] = []
        start = self.session_date - timedelta(days=DAILY_LOOKBACK_DAYS)
        try:
            bars = _fetch_daily(self.adapter, codes, start, self.session_date)
        except Exception as exc:
            return [f"日线结构取数失败，本轮监控只能依据分时结构：{type(exc).__name__}: {exc}"]

        usable = {symbol: rows for symbol, rows in bars.items() if rows}
        missing = [symbol for symbol in codes if symbol not in usable]
        if missing:
            notes.append(f"无日线结构的标的：{'、'.join(missing[:8])}")

        self.snapshots = DailyTechnicalAnalyzer().analyze_many(usable)
        self.wyckoff = {signal.symbol: signal for signal in WyckoffAnalyzer().analyze_many(usable)}
        self.prev_closes = {
            symbol: rows[-2]["close"]
            for symbol, rows in usable.items()
            if len(rows) >= 2 and rows[-2].get("close")
        }
        self.names = _security_names(self.adapter, codes)
        return notes

    def refresh_risk(self, announcements: list[dict]) -> int:
        """Re-read blocking announcements. Called on a slow cycle, not per poll."""
        risks = AnnouncementScreen().evaluate(announcements or [])
        self.risk_notes = {
            code: "；".join(risk.blocking)
            for code, risk in risks.items()
            if getattr(risk, "is_blocked", False)
        }
        return len(self.risk_notes)

    # ---- per poll ---------------------------------------------------------

    def poll(
        self,
        watch_symbols: list[str],
        candidate_symbols: list[str],
        now: datetime,
        in_session: bool = True,
    ) -> PassResult:
        watch = [normalize_symbol(symbol) for symbol in watch_symbols if normalize_symbol(symbol)]
        candidates = [
            normalize_symbol(symbol)
            for symbol in candidate_symbols
            if normalize_symbol(symbol) and normalize_symbol(symbol) not in set(watch)
        ]
        symbols = watch + candidates
        if not symbols:
            health = assess_feed({}, 0, now, self._previous_latest, in_session)
            return PassResult(health=health, notes=["监控列表为空"])

        bars, fetch_notes = self.intraday_adapter.get_minute_bars(symbols, period=self.period)
        health = assess_feed(bars, len(symbols), now, self._previous_latest, in_session)
        if health.latest is not None:
            self._previous_latest = health.latest

        changed = health.verdict != self._last_verdict
        self._last_verdict = health.verdict

        if not health.usable:
            # Deliberately no alerts. The loop announces the suspension once;
            # publishing signals off data this old is the failure mode the
            # whole gate exists to prevent.
            self.state.save()
            return PassResult(
                health=health, notes=fetch_notes, polled=len(symbols), verdict_changed=changed
            )

        intraday = IntradayAnalyzer(today=now.date()).analyze_many(bars, self.prev_closes)
        watch_set = set(watch)
        alerts: list[Alert] = []
        for symbol in symbols:
            snapshot = intraday.get(symbol)
            if snapshot is None:
                continue
            advice = self.advice_engine.build(
                symbol=symbol,
                daily=self.snapshots.get(symbol),
                intraday=snapshot,
                wyckoff=self.wyckoff.get(symbol),
                name=self.names.get(symbol),
            )
            armed = self._apply_pinned_levels(advice, snapshot, now)
            holding = self.holdings.get(symbol)
            alerts.extend(
                self.engine.evaluate(
                    advice=advice,
                    intraday=snapshot,
                    state=self.state,
                    now=now,
                    watchlist=symbol in watch_set,
                    risk_note=self.risk_notes.get(bare_code(symbol)),
                    armed=armed,
                    holding=holding,
                    # 结构支撑取日线的止损参考位——它已经带了最小距离约束
                    # （`signals/daily_technical.py`），不会贴着现价。
                    daily_support=(
                        getattr(self.snapshots.get(symbol), "stop_reference", None)
                        if holding is not None
                        else None
                    ),
                )
            )
        self.state.save()
        return PassResult(
            health=health,
            alerts=alerts,
            notes=fetch_notes,
            polled=len(symbols),
            verdict_changed=changed,
        )

    def _apply_pinned_levels(
        self,
        advice: StockAdvice,
        snapshot: Any,
        now: datetime,
    ) -> bool:
        """Freeze stop and targets on first arming; keep the entry band live.

        The two levels are treated differently on purpose:

        - **stop and targets are pinned.** Recomputed each pass, the stop is
          derived from the live session low and therefore always sits below it,
          so price can never break it and the alert can never fire. A stop that
          follows price down is not a stop.
        - **the entry band tracks VWAP.** "Where would I buy now" is a question
          about the current session mean, and pinning it to 09:45 makes it
          meaningless by lunchtime.

        Returns whether stop/target triggers are armed for this symbol.
        """
        pinned = self.state.pinned_of(advice.symbol)
        if pinned is not None:
            advice.stop = pinned.get("stop")
            advice.targets = list(pinned.get("targets") or [])
            # Recompute the risk/reward against the pinned levels so the number
            # published in an alert matches the levels quoted beside it.
            advice.rrr = _rrr(advice)
            return True

        if snapshot.bar_count < ARM_MIN_BARS:
            # Not enough session yet for the levels to describe anything.
            advice.stop = None
            advice.targets = []
            advice.rrr = None
            return False

        self.state.pin(advice.symbol, advice.stop, advice.targets, now)
        return True

    def snapshot_facts(self, symbols: list[str], now: datetime) -> dict[str, Any]:
        """Current levels and intraday state, for `chat` and for the console."""
        codes = [normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)]
        bars, notes = self.intraday_adapter.get_minute_bars(codes, period=self.period)
        health = assess_feed(bars, len(codes), now, None, True)
        intraday = IntradayAnalyzer(today=now.date()).analyze_many(bars, self.prev_closes)
        rows = []
        for symbol in codes:
            snapshot = intraday.get(symbol)
            if snapshot is None:
                continue
            advice: StockAdvice = self.advice_engine.build(
                symbol=symbol,
                daily=self.snapshots.get(symbol),
                intraday=snapshot,
                wyckoff=self.wyckoff.get(symbol),
                name=self.names.get(symbol),
            )
            rows.append(
                {
                    "symbol": advice.symbol,
                    "name": advice.name,
                    "last_price": advice.last_price,
                    "entry_low": advice.entry_low,
                    "entry_high": advice.entry_high,
                    "stop": advice.stop,
                    "targets": advice.targets,
                    "rrr": advice.rrr,
                    "trend_state": advice.trend_state,
                    "intraday": advice.intraday,
                    "warnings": advice.warnings,
                }
            )
        return {
            "as_of": health.latest.isoformat(timespec="seconds") if health.latest else None,
            "feed_verdict": health.verdict,
            "feed_reason": health.reason,
            "symbols": rows,
            "data_notes": notes,
        }


def _rrr(advice: StockAdvice) -> float | None:
    if not (advice.entry_low and advice.stop and advice.targets):
        return None
    downside = advice.entry_low - advice.stop
    upside = advice.targets[-1] - advice.entry_low
    if downside <= 0 or upside <= 0:
        return None
    return round(upside / downside, 2)


def _fetch_daily(adapter: Any, symbols: list[str], start: date, end: date) -> dict[str, list[dict]]:
    batch = getattr(adapter, "get_historical_bars_batch", None)
    if callable(batch):
        return batch(symbols, start, end)
    return adapter.get_historical_bars(symbols, start, end)


def _security_names(adapter: Any, symbols: list[str]) -> dict[str, str | None]:
    getter = getattr(adapter, "get_security_master", None)
    if not callable(getter):
        return {}
    try:
        master = getter(symbols)
    except Exception:
        return {}
    return {
        symbol: (row.get("name") if isinstance(row, dict) else None)
        for symbol, row in master.items()
    }
