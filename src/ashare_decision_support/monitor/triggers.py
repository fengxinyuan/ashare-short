"""Intraday triggers: alert on state *transitions*, never on states.

The monitor polls every two minutes. Over one session that is ~145 passes, so
a rule phrased as "price is below VWAP" fires 145 times and the notification
channel gets muted by lunch. Every trigger here is therefore phrased as a
crossing — the symbol *was* above and *is now* below — and each carries a
policy saying how soon the same trigger may fire again for the same symbol.

All numbers in an alert come from the deterministic layer (`advice/` and
`signals/intraday.py`). Nothing here calls an LLM: an alert is time-sensitive
and adding two seconds of network latency plus a failure mode to it buys
nothing that a template cannot say.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ashare_decision_support.advice.engine import StockAdvice
from ashare_decision_support.monitor.holdings import (
    TIME_STOP_DAYS,
    TIME_STOP_MIN_GAIN,
    HoldingView,
    effective_stop,
    structure_broken,
    time_stop_due,
)
from ashare_decision_support.signals.intraday import IntradaySnapshot
from ashare_decision_support.state.alerts import AlertState

LEVEL_ACTION = "ACTION"
LEVEL_WARN = "WARN"
LEVEL_INFO = "INFO"

# Chosen default: ACTION + WARN. INFO triggers are implemented and off, so
# turning them on is a config change rather than new code.
DEFAULT_LEVELS: tuple[str, ...] = (LEVEL_ACTION, LEVEL_WARN)

# Fraction of the session range within which price counts as "at the high".
AT_HIGH_TOLERANCE = 0.02

# Volume ratio above which the session is treated as emotionally extended.
VOLUME_SPIKE_RATIO = 3.0


@dataclass(frozen=True)
class TriggerPolicy:
    key: str
    level: str
    label: str
    cooldown_minutes: int = 15
    once_per_session: bool = False


POLICIES: dict[str, TriggerPolicy] = {
    policy.key: policy
    for policy in (
        # A symbol can legitimately leave and re-enter the buy zone, so this is
        # a cooldown rather than a once-per-day. Half an hour is long enough
        # that oscillation around the band edge does not produce a stream.
        TriggerPolicy("entry_zone", LEVEL_ACTION, "进入买入区间", cooldown_minutes=30),
        # Once per session, deliberately. Price that chops across the stop
        # would otherwise send five identical alerts, and five identical
        # alerts is how a channel gets muted.
        TriggerPolicy("stop_break", LEVEL_ACTION, "跌破止损", once_per_session=True),
        TriggerPolicy("target_hit", LEVEL_ACTION, "触及目标", once_per_session=True),
        TriggerPolicy("vwap_lost", LEVEL_WARN, "跌破分时均价", cooldown_minutes=15),
        TriggerPolicy("scenario_shift", LEVEL_WARN, "情景切换", cooldown_minutes=15),
        TriggerPolicy("volume_spike", LEVEL_WARN, "放量异动", cooldown_minutes=30),
        TriggerPolicy("risk_notice", LEVEL_WARN, "突发风险公告", once_per_session=True),
        # ---- 只对持仓触发 --------------------------------------------------
        # 结构破坏比止损早一步：支撑是当初买入的依据，依据没了就该重新评估，
        # 而不是等价格再跌到止损位。给 ACTION 是因为它要求一个决定。
        TriggerPolicy("structure_break", LEVEL_ACTION, "跌破结构支撑", once_per_session=True),
        # 时间止损每天最多提醒一次。它说的是「当初的判断没兑现」，不是紧急事件，
        # 但放着不管就是低吸最大的隐性亏损来源。
        TriggerPolicy("time_stop", LEVEL_WARN, "持有到期未兑现", once_per_session=True),
        TriggerPolicy("vwap_regain", LEVEL_INFO, "站上分时均价", cooldown_minutes=30),
        TriggerPolicy("new_high", LEVEL_INFO, "创当日新高", cooldown_minutes=30),
    )
}

SCENARIO_STRONG = "强势延续"
SCENARIO_RANGE = "均价震荡"
SCENARIO_WEAK = "转弱回落"


@dataclass(frozen=True)
class Alert:
    symbol: str
    trigger: str
    level: str
    headline: str
    detail: list[str] = field(default_factory=list)
    name: str | None = None
    watchlist: bool = True
    # 持仓告警优先级最高：漏报的代价是真金白银，不是错过机会。
    holding: bool = False
    unrealised_pct: float | None = None
    as_of: datetime | None = None
    lag_minutes: float | None = None
    session_date: str | None = None

    @property
    def label(self) -> str:
        return POLICIES[self.trigger].label if self.trigger in POLICIES else self.trigger


class TriggerEngine:
    """Compares the current reading against the last one and emits crossings."""

    def __init__(self, levels: tuple[str, ...] = DEFAULT_LEVELS) -> None:
        self.levels = tuple(levels)

    def evaluate(
        self,
        advice: StockAdvice,
        intraday: IntradaySnapshot,
        state: AlertState,
        now: datetime,
        watchlist: bool = True,
        risk_note: str | None = None,
        armed: bool = True,
        holding: HoldingView | None = None,
        daily_support: float | None = None,
    ) -> list[Alert]:
        symbol = advice.symbol
        previous = state.state_of(symbol)
        current = _derive_state(advice, intraday, armed, holding, daily_support)
        state.set_state(symbol, current)

        # First sighting of the day establishes the baseline. Emitting on it
        # would alert on wherever the symbol happened to be when the monitor
        # started, which is not a crossing of anything.
        if not previous:
            if risk_note:
                return self._emit(
                    state, now, symbol, advice, intraday, watchlist,
                    "risk_notice", f"{advice.name or symbol} 触发风险公告否决", [risk_note],
                    holding=holding,
                )
            # 时间止损是**状态**不是跃迁：它在监控启动那一刻就已经成立，跳过
            # 首轮会让它整天不发。
            if current.get("time_stop_due"):
                return self._emit(
                    state, now, symbol, advice, intraday, watchlist,
                    "time_stop", f"{advice.name or symbol} 持有到期未兑现",
                    _time_stop_detail(holding, intraday.last_price), holding=holding,
                )
            return []

        alerts: list[Alert] = []

        def emit(trigger: str, headline: str, detail: list[str]) -> None:
            alerts.extend(
                self._emit(
                    state, now, symbol, advice, intraday, watchlist, trigger, headline,
                    detail, holding=holding,
                )
            )

        label = advice.name or symbol
        price = f"{intraday.last_price:.2f}"

        if current.get("vs_entry") == "inside" and previous.get("vs_entry") != "inside":
            detail = [f"价格 {price} 进入买入区间 {advice.entry_low:.2f}-{advice.entry_high:.2f}"]
            if advice.stop is not None:
                gap = (advice.entry_low - advice.stop) / advice.entry_low if advice.entry_low else None
                detail.append(
                    f"止损 {advice.stop:.2f}" + (f"（-{gap:.1%}）" if gap is not None else "")
                )
            if advice.targets:
                detail.append("目标 " + "、".join(f"{level:.2f}" for level in advice.targets))
            if advice.rrr is not None:
                detail.append(f"盈亏比 {advice.rrr}")
            emit("entry_zone", f"{label} 进入买入区间", detail)

        if current.get("stop_broken") and not previous.get("stop_broken"):
            # 报出来的必须是**实际生效的**那个止损。走 plan_stop 时 advice.stop
            # 可能是 None，直接格式化它会崩，而且报错的价位本身就是误导。
            level = effective_stop(holding, advice.stop)
            source = "（你设定的）" if holding is not None and holding.plan_stop else ""
            emit(
                "stop_break",
                f"{label} 跌破止损{source} {level:.2f}",
                [f"现价 {price}，当日结构破坏", "本日不再重复推送该标的止损"],
            )

        # ---- 只对持仓的两条 -------------------------------------------------
        if current.get("structure_broken") and not previous.get("structure_broken"):
            emit(
                "structure_break",
                f"{label} 跌破结构支撑 {daily_support:.2f}",
                [
                    f"现价 {price}，买入时依据的日线支撑已失效",
                    "支撑没了持有理由就没了——不必等价格走到止损位再决定",
                ],
            )

        if current.get("time_stop_due") and not previous.get("time_stop_due"):
            emit(
                "time_stop",
                f"{label} 持有到期未兑现",
                _time_stop_detail(holding, intraday.last_price),
            )

        hit_now = set(current.get("targets_hit") or [])
        hit_before = set(previous.get("targets_hit") or [])
        for index in sorted(hit_now - hit_before):
            if index < len(advice.targets):
                emit(
                    f"target_hit:{index}",
                    f"{label} 触及目标 {advice.targets[index]:.2f}",
                    [f"现价 {price}，为第 {index + 1} 目标位"],
                )

        if current.get("vs_vwap") == "below" and previous.get("vs_vwap") == "above":
            detail = [f"现价 {price}，有效跌破分时均价 {intraday.vwap:.2f}"]
            if intraday.session_low:
                detail.append(f"下看当日低点 {intraday.session_low:.2f}")
            emit("vwap_lost", f"{label} 跌破分时均价", detail)

        if current.get("vs_vwap") == "above" and previous.get("vs_vwap") == "below":
            emit("vwap_regain", f"{label} 站上分时均价", [f"现价 {price}，均价 {intraday.vwap:.2f}"])

        if current.get("scenario") != previous.get("scenario") and current.get("scenario"):
            emit(
                "scenario_shift",
                f"{label} 情景切换：{previous.get('scenario')} → {current['scenario']}",
                _scenario_detail(advice, current["scenario"], intraday),
            )

        if current.get("volume_spike") and not previous.get("volume_spike"):
            emit(
                "volume_spike",
                f"{label} 量比 {intraday.volume_ratio:.1f} 倍",
                [f"成交量为近 5 日同期均量的 {intraday.volume_ratio:.1f} 倍，注意情绪透支"],
            )

        if current.get("at_high") and not previous.get("at_high"):
            emit("new_high", f"{label} 创当日新高 {intraday.session_high:.2f}", [f"现价 {price}"])

        if risk_note:
            emit("risk_notice", f"{label} 触发风险公告否决", [risk_note])

        return alerts

    # ---- policy ----------------------------------------------------------

    def _emit(
        self,
        state: AlertState,
        now: datetime,
        symbol: str,
        advice: StockAdvice,
        intraday: IntradaySnapshot,
        watchlist: bool,
        trigger: str,
        headline: str,
        detail: list[str],
        holding: HoldingView | None = None,
    ) -> list[Alert]:
        policy = POLICIES.get(trigger.split(":", 1)[0])
        if policy is None or policy.level not in self.levels:
            return []
        # Candidates are the machine's picks, not yours. Only the levels that
        # demand a decision are worth interrupting for.
        #
        # 持仓不受这条约束：漏报一条持仓告警的代价是真金白银，不是错过机会。
        if not watchlist and holding is None and policy.level != LEVEL_ACTION:
            return []

        if policy.once_per_session and state.fire_count(symbol, trigger) > 0:
            return []
        last = state.last_fired(symbol, trigger)
        if last is not None and (now - last).total_seconds() < policy.cooldown_minutes * 60:
            return []

        state.mark_fired(symbol, trigger, now)
        lag = None
        if intraday.last_timestamp is not None:
            lag = round((now - intraday.last_timestamp).total_seconds() / 60.0, 1)
        # 持仓告警一律带上成本与浮盈——不带这两个数，等于让你自己去翻账户对一遍。
        if holding is not None:
            detail = list(detail) + holding.describe(intraday.last_price)
        return [
            Alert(
                symbol=symbol,
                trigger=trigger,
                level=policy.level,
                headline=headline,
                detail=detail,
                name=advice.name,
                watchlist=watchlist,
                holding=holding is not None,
                unrealised_pct=None if holding is None else holding.unrealised_pct(intraday.last_price),
                as_of=intraday.last_timestamp,
                lag_minutes=lag,
                session_date=str(intraday.session_date),
            )
        ]


# ---- state derivation ----------------------------------------------------


def _derive_state(
    advice: StockAdvice,
    intraday: IntradaySnapshot,
    armed: bool = True,
    holding: HoldingView | None = None,
    daily_support: float | None = None,
) -> dict:
    last = intraday.last_price
    # 你填的 plan_stop 优先于系统算的：手工值是决定，系统值是建议。
    stop = effective_stop(holding, advice.stop)
    return {
        "vs_entry": _vs_entry(advice, last),
        "vs_vwap": None if intraday.vwap is None else ("above" if last >= intraday.vwap else "below"),
        "scenario": _scenario(intraday),
        # Before the levels are pinned there is no stop to break, and reporting
        # False would look identical to "checked and fine".
        #
        # 持仓例外：plan_stop 是你自己定的绝对价位，不依赖当日结构成型，所以
        # 不受 armed 约束——开盘第一分钟跌破它也该报。
        "stop_broken": (
            stop is not None and last < stop and (armed or (holding is not None and holding.plan_stop))
        ),
        "targets_hit": (
            [index for index, level in enumerate(advice.targets) if last >= level] if armed else []
        ),
        "armed": armed,
        "volume_spike": intraday.volume_ratio is not None and intraday.volume_ratio >= VOLUME_SPIKE_RATIO,
        "at_high": _at_high(intraday),
        # 下面两个只对持仓有意义，未持仓恒为 False。
        "structure_broken": holding is not None and structure_broken(last, daily_support),
        "time_stop_due": time_stop_due(holding, last),
    }


def _time_stop_detail(holding: HoldingView | None, price: float | None) -> list[str]:
    if holding is None:
        return []
    gain = holding.unrealised_pct(price)
    lines = [
        f"持有 {holding.held_days} 天，超过低吸的自然持有期（{TIME_STOP_DAYS} 天）",
    ]
    if gain is not None:
        lines.append(
            f"期间收益 {gain:+.2%}，未达 {TIME_STOP_MIN_GAIN:.0%} 门槛——"
            "当初「回调结束会反弹」的判断已被时间证伪"
        )
    lines.append("不亏不赚地耗着同样是失败：它占着仓位和注意力")
    return lines


def _vs_entry(advice: StockAdvice, last: float) -> str | None:
    if advice.entry_low is None or advice.entry_high is None:
        return None
    if last < advice.entry_low:
        return "below"
    if last > advice.entry_high:
        return "above"
    return "inside"


def _scenario(intraday: IntradaySnapshot) -> str:
    if intraday.vwap is not None and intraday.last_price < intraday.vwap:
        return SCENARIO_WEAK
    if _at_high(intraday):
        return SCENARIO_STRONG
    return SCENARIO_RANGE


def _at_high(intraday: IntradaySnapshot) -> bool:
    span = intraday.session_high - intraday.session_low
    if span <= 0:
        return False
    return (intraday.session_high - intraday.last_price) / span <= AT_HIGH_TOLERANCE


def _scenario_detail(advice: StockAdvice, scenario: str, intraday: IntradaySnapshot) -> list[str]:
    for item in advice.scenarios:
        if item.name == scenario:
            return [item.trigger, item.implication]
    return [f"现价 {intraday.last_price:.2f}"]
