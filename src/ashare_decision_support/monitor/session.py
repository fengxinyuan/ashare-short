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

from ashare_decision_support.adapters.base import fetch_daily_bars, security_names
from ashare_decision_support.advice.engine import AdviceEngine, StockAdvice, risk_reward
from ashare_decision_support.monitor.feed import FeedHealth, assess_feed
from ashare_decision_support.monitor.holdings import HoldingView
from ashare_decision_support.monitor.triggers import DEFAULT_LEVELS, Alert, TriggerEngine
from ashare_decision_support.screens import AnnouncementScreen
from ashare_decision_support.signals import DailyTechnicalAnalyzer, WyckoffAnalyzer
from ashare_decision_support.signals.breakeven import measure_breakeven
from ashare_decision_support.signals.intraday import IntradayAnalyzer
from ashare_decision_support.signals.orderflow import read_many
from ashare_decision_support.signals.stabilise import assess_stabilisation
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
        snapshot_adapter: Any = None,
    ) -> None:
        self.adapter = adapter
        self.intraday_adapter = intraday_adapter
        self.session_date = session_date
        self.period = period
        self.engine = TriggerEngine(levels=levels)
        # **盘中永远不带裁决器。** 它是每标的一次 LLM 调用：20 只票的一轮会变成
        # 20 次网络往返、几十秒延迟、二十个失败机会，而告警要的是秒级时效。
        # 见 `docs/ARCHITECTURE.md` §五。
        self.advice_engine = AdviceEngine()
        # 五档快照相反——它是**整批一次**请求，一轮加不到一秒，却能带来两样
        # 规则层拿不到的东西：真实涨跌停价（封板了就买不进，再好的位置也没用），
        # 和当下真的有人挂着的那个价位。
        self.snapshot_adapter = snapshot_adapter
        self.state = alert_state or AlertState.load(session_date)

        self.snapshots: dict[str, Any] = {}
        self.wyckoff: dict[str, Any] = {}
        # prime() 拉回来的原始日线，`_seed_peak` 用它算建仓以来的最高价。
        self.daily_bars: dict[str, list[dict]] = {}
        self.names: dict[str, str | None] = {}
        self.prev_closes: dict[str, float] = {}
        self.risk_notes: dict[str, str] = {}
        # 持仓视图。空字典意味着「这一轮没有任何持仓」，而不是「还没加载」——
        # 加载失败会写进 note，不会静默退化成「你什么都没持有」。
        self.holdings: dict[str, HoldingView] = {}

        self._previous_latest: datetime | None = None
        self._last_verdict: str | None = None

    # ---- once per session -------------------------------------------------

    def load_positions(self, positions: Any) -> list[str]:
        """读入持仓。**已持仓的票要盯的是卖点，不是买点。**

        持仓由人手工维护（`ashare-ds position open`）——系统不下单、不接管账户，
        不可能自己知道你买了什么。传 None 表示这一轮不带持仓上下文，所有标的都按
        「未持仓、盯买点」处理。
        """
        self.holdings = {}
        if positions is None:
            return []
        notes: list[str] = []
        for entry in getattr(positions, "entries", []) or []:
            view = HoldingView.from_position(entry, today=self.session_date)
            self.holdings[view.symbol] = view
            if not view.cost_price:
                notes.append(f"{view.symbol} 在持仓表里但没有成本价，只能报绝对价位，给不出浮盈判断")
            self._seed_peak(view)
        if self.holdings:
            notes.append(
                f"持仓 {len(self.holdings)} 只，按卖点口径监控"
                "（止损 / 目标 / 结构破坏 / 时间止损 / 浮盈回吐）"
            )
        return notes

    def _seed_peak(self, holding: HoldingView) -> None:
        """把移动止损的峰值播种成**建仓以来的最高价**，而不是今天的最高价。

        只用当日峰值会漏掉最常见的那种坐电梯：昨天冲到 +9%，今天平开一路阴跌
        到 +1%。今天的峰值约等于开盘价，日内回撤很小，于是移动止损整天不响——
        而这正是它该说话的那一天。

        种子取自 `prime()` 已经拉回来的日线，不额外发一次请求。两种情况一律
        **不播种**，让盘中的实时高点自己从零累积：拿不到日线，或者没填建仓日。
        没有建仓日就没有「持仓期」，拿 240 天的历史最高当峰值会算出一个假的
        回撤幅度——**少一个种子只是迟一点，播一个错的种子会直接报错误的告警。**

        已知的一处高估：建仓当日的最高价可能出现在你买入**之前**。日线粒度分不
        出来，代价是那一天的峰值可能偏高。不修是因为修不了——除非记录买入时刻，
        而那是账户数据，系统拿不到。
        """
        opened = str(holding.opened_on or "")[:10]
        if not opened:
            return
        highs = [
            row["high"]
            for row in self.daily_bars.get(holding.symbol) or []
            if row.get("high") and _bar_date(row) >= opened
        ]
        if highs:
            self.state.observe_peak(holding.symbol, max(highs))

    def prime(self, symbols: list[str]) -> list[str]:
        """Fetch and analyse daily structure for every symbol we will watch."""
        codes = [normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)]
        if not codes:
            return ["监控列表为空"]

        notes: list[str] = []
        start = self.session_date - timedelta(days=DAILY_LOOKBACK_DAYS)
        try:
            bars = fetch_daily_bars(self.adapter, codes, start, self.session_date)
        except Exception as exc:
            return [f"日线结构取数失败，本轮监控只能依据分时结构：{type(exc).__name__}: {exc}"]

        usable = {symbol: rows for symbol, rows in bars.items() if rows}
        missing = [symbol for symbol in codes if symbol not in usable]
        if missing:
            notes.append(f"无日线结构的标的：{'、'.join(missing[:8])}")

        self.snapshots = DailyTechnicalAnalyzer().analyze_many(usable)
        self.wyckoff = {signal.symbol: signal for signal in WyckoffAnalyzer().analyze_many(usable)}
        # 留着原始日线：移动止损要拿它算「建仓以来的最高价」，而分析器的产物
        # 只有聚合读数，回不到逐根的高点。
        self.daily_bars = usable
        self.prev_closes = {
            symbol: rows[-2]["close"]
            for symbol, rows in usable.items()
            if len(rows) >= 2 and rows[-2].get("close")
        }
        self.names = security_names(self.adapter, codes)
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
        now: datetime,
        in_session: bool = True,
    ) -> PassResult:
        symbols = list(
            dict.fromkeys(
                normalize_symbol(symbol) for symbol in watch_symbols if normalize_symbol(symbol)
            )
        )
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
            return PassResult(health=health, notes=fetch_notes, verdict_changed=changed)

        intraday = IntradayAnalyzer(today=now.date()).analyze_many(bars, self.prev_closes)
        books = self._fetch_books(symbols, fetch_notes)
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
                book=books.get(symbol),
            )
            armed = self._apply_pinned_levels(advice, snapshot, now)
            holding = self.holdings.get(symbol)
            if holding is not None:
                advice.breakeven_lines = _breakeven_lines(
                    holding.cost_price,
                    snapshot.last_price,
                    self.snapshots.get(symbol),
                )
            # 止跌确认只对**未持仓**的票算：已持仓要回答的是什么时候卖，
            # 「跌势停了没有」对它不是进场问题。顺带省掉一半计算。
            stabilisation = (
                assess_stabilisation(bars.get(symbol) or []) if holding is None else None
            )
            # 峰值只对持仓记。用**当日最高**而不是最新价推进：两分钟一轮，中间
            # 那根冲高的分钟线不会被看见，而移动止损量的正是「最高到哪、现在
            # 回到哪」——漏掉尖顶就等于把回撤算小了。
            peak = (
                self.state.observe_peak(symbol, snapshot.session_high)
                if holding is not None
                else None
            )
            alerts.extend(
                self.engine.evaluate(
                    advice=advice,
                    intraday=snapshot,
                    state=self.state,
                    now=now,
                    risk_note=self.risk_notes.get(bare_code(symbol)),
                    armed=armed,
                    holding=holding,
                    stabilisation=stabilisation,
                    # 盘口进的是**告警本身**，不只是价位计算：封涨停时「进入
                    # 买入区间」要降级成买不进，封跌停时「跌破止损」要说清此刻
                    # 卖不出。价位对不对和动作做不做得成是两回事。
                    book=books.get(symbol),
                    # 移动止损的两个输入。ATR 是回吐幅度的单位——没有它就不判，
                    # 固定百分比在不同波动的票上含义差几倍。
                    peak_price=peak,
                    atr=(
                        getattr(self.snapshots.get(symbol), "atr14", None)
                        if holding is not None
                        else None
                    ),
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
            verdict_changed=changed,
        )

    def _fetch_books(self, symbols: list[str], notes: list[str]) -> dict[str, Any]:
        """一轮一次的批量五档。**取不到就当没有，绝不拖垮这一轮。**

        盘口是加分项不是必需项：缺了只是候选集里少两类价位、封板状态未知，
        而分时结构、价位、触发器全都照常。让它有资格中断一轮告警，等于用一个
        可选数据源给整条实时链路加了一个新的单点故障。
        """
        if self.snapshot_adapter is None:
            return {}
        try:
            snapshots, fetch_notes = self.snapshot_adapter.get_snapshots(list(symbols))
        except Exception as exc:
            notes.append(f"五档取数失败（{type(exc).__name__}），本轮不含盘口读数")
            return {}
        notes.extend(fetch_notes or [])
        return read_many(snapshots)

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
            advice.rrr = risk_reward(advice)
            return True

        if snapshot.bar_count < ARM_MIN_BARS:
            # Not enough session yet for the levels to describe anything.
            advice.stop = None
            advice.targets = []
            advice.rrr = None
            return False

        self.state.pin(advice.symbol, advice.stop, advice.targets, now)
        return True



def _breakeven_lines(cost: float | None, price: float | None, daily: Any) -> list[str]:
    """把成本价与日线压力交叉，仅供已持仓告警补充上下文。"""
    if daily is None:
        return []
    levels = {
        "MA5": getattr(daily, "ma5", None),
        "MA10": getattr(daily, "ma10", None),
        "MA20": getattr(daily, "ma20", None),
        "MA60": getattr(daily, "ma60", None),
        **{
            str(getattr(level, "label", "压力位")): getattr(level, "price", None)
            for level in (getattr(daily, "resistances", None) or [])
        },
    }
    # 第一行重复了 HoldingView 的成本/浮盈信息，告警里只保留结构结论。
    return measure_breakeven(cost, price, levels).describe()[1:]


def _bar_date(row: dict) -> str:
    """日线一根的日期，取成 `YYYY-MM-DD`。

    键名在各适配器之间不统一（`timestamp` / `date` / `日期`），值也可能是
    `datetime` 或字符串。取不出来时返回空串——它排在任何真实日期之前，于是
    这根不会被算进「建仓以来」，宁可少算一根也不拿一根日期不明的去顶峰值。
    """
    for key in ("timestamp", "date", "日期", "time", "时间"):
        value = row.get(key)
        if value in (None, ""):
            continue
        text = value.isoformat() if hasattr(value, "isoformat") else str(value)
        return text[:10]
    return ""
