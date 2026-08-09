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
from typing import Any

from ashare_decision_support.advice.engine import StockAdvice
from ashare_decision_support.advice.view import trade_context_parts
from ashare_decision_support.monitor.holdings import (
    TIME_STOP_DAYS,
    TIME_STOP_MIN_GAIN,
    Giveback,
    HoldingView,
    assess_giveback,
    effective_stop,
    effective_targets,
    structure_broken,
    time_stop_due,
)
from ashare_decision_support.signals.intraday import IntradaySnapshot
from ashare_decision_support.signals.stabilise import Stabilisation
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

# 人工盯盘时反复使用的两条执行口径：
# 1) 弱票先收回观察线，再谈修复；这不是买点，只是从“别碰”变成“可继续看”。
# 2) 优质趋势票放量贴近/刷新日内高点，才允许从“等回踩”升级成“突破跟随”。
BREAKOUT_MIN_VOLUME_RATIO = 1.0
RECLAIM_ACTIONS = frozenset({"wait_reclaim"})
BREAKOUT_ACTIONS = frozenset({"buy_pullback", "breakout_follow"})
# 命中任意一条就不给 ACTION 级买点——低吸和突破共用这一条标准。名字原先叫
# BREAKOUT_*，因为它当初只接在突破路径上；低吸那一路漏掉了，于是被业绩否决、
# 盈亏比 1.09 的票照样能推出 ACTION 买点。改名是为了让「它管两条路」这件事
# 写在名字里，而不是只写在注释里。
#
# 「业绩否决」是后补的：它由 `jobs/advise.py` 写进 warnings，语义比盈亏比更硬
# ——盈亏比是算出来的估计，业绩否决是公司自己签字披露的事实。
ENTRY_BLOCKING_WARNINGS = (
    "风险收益不划算",
    "不满足超短线最低要求",
    "低吸转为接刀",
    "缺少有效支撑",
    "业绩否决",
)


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
        # 进了区间但跌势未止。降成 WARN 是刻意的：它是「盯着」不是「买」，
        # 而下跌途中的每一点都会「进入区间」，按 ACTION 发会一路向下连着响。
        TriggerPolicy("entry_watch", LEVEL_WARN, "进入买入区间（未止跌）", cooldown_minutes=30),
        # 进了区间但**买不进**：封涨停。和上面那条同为 WARN，成因完全不同——
        # 那条是「该等」，这条是「想动也动不了」。合成一条会让人以为还能挂单。
        TriggerPolicy("entry_sealed", LEVEL_WARN, "进入买入区间（封涨停买不进）", cooldown_minutes=30),
        # 进了区间但**不该买**：盈亏比不达标，或命中业绩否决这类警告。和上面两条
        # 同为 WARN，成因又不同——那两条是「等」和「动不了」，这条是「这只票本身
        # 就被系统否了」。质量闸门 `_entry_quality_blocked` 原先只接在突破路径上，
        # 低吸路径漏掉了，于是一只盈亏比 1.09、命中业绩否决的票照样能推 ACTION 买点，
        # 这和简报自己写的「盈亏比不足 1.5 的机会直接放弃」是矛盾的。区间确实进了，
        # 所以照发，但只发 WARN。
        TriggerPolicy("entry_blocked", LEVEL_WARN, "进入买入区间（质量不达标）", cooldown_minutes=30),
        # 跌势停了。这才是低吸真正的进场信号。
        TriggerPolicy("entry_confirmed", LEVEL_ACTION, "止跌确认", cooldown_minutes=30),
        # 弱票先收回观察线再看修复。给 WARN，不给 ACTION：收回不是买点，
        # 只是从“禁止追”升级为“可以重新盯”。
        TriggerPolicy("reclaim_confirmed", LEVEL_WARN, "收回观察线", cooldown_minutes=30),
        # 趋势票等不到回踩时的备选动作：只有质量过关且量能确认，才把“创新高”
        # 从 INFO 升成 ACTION。
        TriggerPolicy("breakout_confirmed", LEVEL_ACTION, "放量突破确认", cooldown_minutes=30),
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
        # 浮盈回吐 = 移动止损。上面四条**没有一条会响**：止损远在下方、目标没到、
        # 支撑没破、期限没到，而浮盈已经从 +9% 掉回 +1%。给 ACTION 是因为它要求
        # 一个决定（走还是接着拿）；每日一次是因为它说的是「这一天的高点已经
        # 吐掉一半」，同一天重复说第二遍不增加任何信息。
        TriggerPolicy("giveback", LEVEL_ACTION, "浮盈回吐", once_per_session=True),
        TriggerPolicy("vwap_regain", LEVEL_INFO, "站上分时均价", cooldown_minutes=30),
        TriggerPolicy("new_high", LEVEL_INFO, "创当日新高", cooldown_minutes=30),
    )
}

SCENARIO_STRONG = "强势延续"
SCENARIO_RANGE = "均价震荡"
SCENARIO_WEAK = "转弱回落"

# 哪些告警值得带上盘口。标准是**盘口会不会改变你接下来做的事**：这几条都要求
# 一个下单动作，而五档回答的正是「这个动作此刻做不做得成、那个价上有没有人挂
# 着」。放量异动、创新高、站上均价这类是状态通报，贴上两行挂单量只是噪音。
BOOK_RELEVANT = frozenset(
    {
        "entry_zone",
        "entry_watch",
        "entry_sealed",
        "entry_confirmed",
        "reclaim_confirmed",
        "breakout_confirmed",
        "stop_break",
        "structure_break",
        "time_stop",
        "giveback",
        "target_hit",
    }
)


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
        stabilisation: Stabilisation | None = None,
        book: Any = None,
        peak_price: float | None = None,
        atr: float | None = None,
    ) -> list[Alert]:
        symbol = advice.symbol
        previous = state.state_of(symbol)
        # 你设的目标价优先于系统算的。算一次传下去，别在状态推导和告警正文里
        # 各算一遍——那两处一旦漂移，报出来的目标价和「触及目标」的判据就会
        # 指向不同的数。
        targets = effective_targets(holding, advice.targets)
        giveback = assess_giveback(holding, intraday.last_price, peak_price, atr)
        current = _derive_state(
            advice, intraday, armed, holding, daily_support, stabilisation, targets, giveback
        )
        state.set_state(symbol, current)

        # 封涨停 = 买不进。**未知不算封板**：拿不到涨跌停价时 `at_limit_up` 是
        # None，`bool(None)` 为 False，于是缺数据永远只会让这一层什么都不做，
        # 不会凭空压掉一条本该发的买入告警。
        sealed_up = bool(book is not None and getattr(book, "at_limit_up", None))

        # First sighting of the day still establishes the baseline for noisy
        # state changes. But *actionable* facts that already exist at startup
        # must not be silent: a monitor launched after lunch should still tell
        # you a watched name is already in a confirmed buy zone, or a holding is
        # already below its stop.
        if not previous:
            return self._startup_alerts(
                state=state,
                now=now,
                symbol=symbol,
                advice=advice,
                intraday=intraday,
                watchlist=watchlist,
                current=current,
                risk_note=risk_note,
                holding=holding,
                daily_support=daily_support,
                stabilisation=stabilisation,
                book=book,
                targets=targets,
                giveback=giveback,
                sealed_up=sealed_up,
            )

        alerts: list[Alert] = []

        def emit(trigger: str, headline: str, detail: list[str]) -> None:
            alerts.extend(
                self._emit(
                    state, now, symbol, advice, intraday, watchlist, trigger, headline,
                    detail, holding=holding, book=book,
                )
            )

        label = advice.name or symbol
        price = f"{intraday.last_price:.2f}"
        # 质量闸门对低吸和突破是同一条标准，只是原先只接在突破那一路上。
        quality_blocked = _entry_quality_blocked(advice)

        if current.get("vs_entry") == "inside" and previous.get("vs_entry") != "inside":
            detail = [f"价格 {price} 进入买入区间 {advice.entry_low:.2f}-{advice.entry_high:.2f}"]
            detail.extend(_entry_plan_detail(advice, holding, targets))
            detail.extend(_execution_guard_detail(advice))

            # 低吸的第三层：**进入区间不等于可以买。** 下跌途中的任何一点都
            # 「到位过」，接刀和低吸的区别只在于跌势有没有停。没有确认时降级
            # 成观察，而不是照发一条看起来像机会的 ACTION。
            if stabilisation is not None:
                detail.append(stabilisation.describe())
            if sealed_up:
                # 买入区间和封板价重叠只有一种成因：当日振幅够大，回踩带的上沿
                # 盖住了涨停价。区间是真的「进」了，所以不能不发；但这一刻买不
                # 进，按 ACTION 发等于催人去做一件此刻做不成的事。
                emit("entry_sealed", f"{label} 进入买入区间，但已封涨停买不进", detail)
            elif quality_blocked:
                emit("entry_blocked", f"{label} 进入买入区间，但质量不达标", detail)
            elif stabilisation is not None and not stabilisation.confirmed:
                emit("entry_watch", f"{label} 进入买入区间（未见止跌）", detail)
            else:
                emit("entry_zone", f"{label} 进入买入区间", detail)

        # 已经在区间里，随后才出现止跌——这是低吸最典型的进场顺序：先跌进来，
        # 再等企稳。只看「进入区间」那一刻会永远错过它。
        #
        # 封涨停时不发：封板价上的「止跌确认」恒成立（价格贴着上沿不动），
        # 而它指向的动作——现在买——恰恰是此刻做不到的那个。
        #
        # 质量不达标时同样不发：止跌确认是这条链路上唯一的 ACTION 买点，而
        # 「跌势停了」并不能把一只盈亏比不足或被业绩否决的票变成可买。
        if (
            current.get("stabilised")
            and not previous.get("stabilised")
            and current.get("vs_entry") == "inside"
            and not sealed_up
            and not quality_blocked
        ):
            emit(
                "entry_confirmed",
                f"{label} 止跌确认，位于买入区间内",
                [f"现价 {price}，区间 {advice.entry_low:.2f}-{advice.entry_high:.2f}", stabilisation.describe() if stabilisation else "", "跌势停了才是低吸的进场点——进区间本身不是", *_entry_plan_detail(advice, holding, targets), *_execution_guard_detail(advice)],
            )

        reclaim = _reclaim_level(advice)
        if (
            holding is None
            and _shortline_action(advice) in RECLAIM_ACTIONS
            and reclaim is not None
            and current.get("vs_reclaim") == "above"
            and previous.get("vs_reclaim") != "above"
            and not (current.get("vs_entry") == "inside" and current.get("stabilised"))
        ):
            emit(
                "reclaim_confirmed",
                f"{label} 收回观察线 {reclaim:.2f}",
                [f"现价 {price}，已收回弱转修复观察线 {reclaim:.2f}", "这不是追高买点，只是从“先别碰”升级为“继续盯站稳”。", *_entry_plan_detail(advice, holding, targets), *_execution_guard_detail(advice)],
            )

        if holding is None and current.get("breakout_confirmed") and not previous.get("breakout_confirmed"):
            volume = intraday.volume_ratio
            detail = [
                f"现价 {price}，贴近或刷新当日高点 {intraday.session_high:.2f}",
                (
                    f"分钟量能为近 5 日同期 {volume:.1f} 倍"
                    if isinstance(volume, (int, float))
                    else "量能确认缺失"
                ),
                "突破买法只适合质量过关的趋势票；跌回分时均价下方则突破失败。",
            ]
            emit(
                "breakout_confirmed",
                f"{label} 放量突破确认",
                detail + _entry_plan_detail(advice, holding, targets) + _execution_guard_detail(advice),
            )

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

        # 冲高又跌回来：止损没破、目标没到、支撑还在、期限没满——四条卖出触发
        # 一条都不响，而浮盈已经吐掉一半。这就是「坐电梯」的全过程。
        if current.get("giveback_due") and not previous.get("giveback_due"):
            emit("giveback", f"{label} 浮盈回吐过半", _giveback_detail(giveback))

        hit_now = set(current.get("targets_hit") or [])
        hit_before = set(previous.get("targets_hit") or [])
        source = "（你设定的）" if _manual_targets(holding) else ""
        for index in sorted(hit_now - hit_before):
            if holding is None and current.get("breakout_confirmed"):
                continue
            if index < len(targets):
                emit(
                    f"target_hit:{index}",
                    f"{label} 触及目标{source} {targets[index]:.2f}",
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

    def _startup_alerts(
        self,
        state: AlertState,
        now: datetime,
        symbol: str,
        advice: StockAdvice,
        intraday: IntradaySnapshot,
        watchlist: bool,
        current: dict,
        risk_note: str | None,
        holding: HoldingView | None,
        daily_support: float | None,
        stabilisation: Stabilisation | None,
        book: Any,
        targets: list[float],
        giveback: Giveback | None,
        sealed_up: bool,
    ) -> list[Alert]:
        alerts: list[Alert] = []
        label = advice.name or symbol
        price = f"{intraday.last_price:.2f}"

        def emit(trigger: str, headline: str, detail: list[str]) -> None:
            alerts.extend(
                self._emit(
                    state, now, symbol, advice, intraday, watchlist, trigger, headline,
                    detail, holding=holding, book=book,
                )
            )

        if risk_note:
            emit("risk_notice", f"{label} 触发风险公告否决", [risk_note])

        if holding is not None:
            if current.get("stop_broken"):
                level = effective_stop(holding, advice.stop)
                source = "（你设定的）" if holding.plan_stop else ""
                emit(
                    "stop_break",
                    f"{label} 跌破止损{source} {level:.2f}",
                    [f"现价 {price}，启动监控时已低于止损", "本日不再重复推送该标的止损"],
                )
            if current.get("structure_broken"):
                emit(
                    "structure_break",
                    f"{label} 跌破结构支撑 {daily_support:.2f}",
                    [
                        f"现价 {price}，启动监控时买入依据已失效",
                        "支撑没了持有理由就没了——不必等价格走到止损位再决定",
                    ],
                )
            if current.get("time_stop_due"):
                emit(
                    "time_stop",
                    f"{label} 持有到期未兑现",
                    _time_stop_detail(holding, intraday.last_price),
                )
            if current.get("giveback_due"):
                emit("giveback", f"{label} 浮盈回吐过半", _giveback_detail(giveback))
            source = "（你设定的）" if _manual_targets(holding) else ""
            for index in sorted(current.get("targets_hit") or []):
                if index < len(targets):
                    emit(
                        f"target_hit:{index}",
                        f"{label} 触及目标{source} {targets[index]:.2f}",
                        [f"现价 {price}，启动监控时已在第 {index + 1} 目标位上方"],
                    )
            return alerts

        reclaim = _reclaim_level(advice)
        if (
            _shortline_action(advice) in RECLAIM_ACTIONS
            and reclaim is not None
            and current.get("vs_reclaim") == "above"
            and not (current.get("vs_entry") == "inside" and current.get("stabilised"))
        ):
            emit(
                "reclaim_confirmed",
                f"{label} 已收回观察线 {reclaim:.2f}",
                [f"现价 {price}，启动监控时已收回弱转修复观察线 {reclaim:.2f}", "这不是追高买点，只是从“先别碰”升级为“继续盯站稳”。", *_entry_plan_detail(advice, holding, targets), *_execution_guard_detail(advice)],
            )

        if current.get("breakout_confirmed"):
            volume = intraday.volume_ratio
            emit(
                "breakout_confirmed",
                f"{label} 已放量突破确认",
                [f"现价 {price}，启动监控时已贴近或刷新当日高点 {intraday.session_high:.2f}", f"分钟量能为近 5 日同期 {volume:.1f} 倍" if isinstance(volume, (int, float)) else "量能确认缺失", "突破买法只适合质量过关的趋势票；跌回分时均价下方则突破失败。", *_entry_plan_detail(advice, holding, targets), *_execution_guard_detail(advice)],
            )

        # 未持仓首轮只补「买点相关」状态，不补均价/情景这类噪音状态。
        if current.get("vs_entry") != "inside":
            return alerts

        detail = [f"价格 {price} 已在买入区间 {advice.entry_low:.2f}-{advice.entry_high:.2f}", *_entry_plan_detail(advice, holding, targets), *_execution_guard_detail(advice)]
        if stabilisation is not None:
            detail.append(stabilisation.describe())

        if sealed_up:
            emit("entry_sealed", f"{label} 已在买入区间，但封涨停买不进", detail)
        elif _entry_quality_blocked(advice):
            emit("entry_blocked", f"{label} 已在买入区间，但质量不达标", detail)
        elif stabilisation is not None and stabilisation.confirmed:
            emit(
                "entry_confirmed",
                f"{label} 止跌确认，位于买入区间内",
                [f"现价 {price}，区间 {advice.entry_low:.2f}-{advice.entry_high:.2f}", stabilisation.describe(), "启动监控时买点已经成立，本条用于补报当前可执行状态", *_entry_plan_detail(advice, holding, targets), *_execution_guard_detail(advice)],
            )
        elif stabilisation is not None:
            emit("entry_watch", f"{label} 已在买入区间（未见止跌）", detail)
        else:
            emit("entry_zone", f"{label} 已在买入区间", detail)
        return alerts

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
        book: Any = None,
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
        context_lines = _advice_context_lines(advice)
        if context_lines:
            detail = list(detail) + context_lines
        # 盘口贴在 detail 末尾、成本浮盈之前：它是旁证，不该盖过这条告警本身
        # 在说的事。只贴给要求下单动作的那几类（`BOOK_RELEVANT`）。
        if book is not None and trigger.split(":", 1)[0] in BOOK_RELEVANT:
            detail = list(detail) + list(book.describe())
        # 持仓告警一律带上成本与浮盈——不带这两个数，等于让你自己去翻账户对一遍。
        if holding is not None:
            detail = list(detail) + holding.describe(intraday.last_price)
            # 仅在浮亏时补回本结构；浮盈状态下这层没有新增决策信息。
            breakeven = list(getattr(advice, "breakeven_lines", []) or [])
            if any("回本还需" in line for line in breakeven):
                detail += breakeven
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
    stabilisation: Stabilisation | None = None,
    targets: list[float] | None = None,
    giveback: Giveback | None = None,
) -> dict:
    last = intraday.last_price
    # 你填的 plan_stop 优先于系统算的：手工值是决定，系统值是建议。
    stop = effective_stop(holding, advice.stop)
    levels = effective_targets(holding, advice.targets) if targets is None else targets
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
            stop is not None and last < stop and (armed or _manual_stop(holding))
        ),
        # 同理：`_apply_pinned_levels` 在开盘 15 分钟内会把系统算的目标清空，
        # 但你手填的目标价跟当日结构无关，第一分钟摸到就该报。
        #
        # 这里只认 `plan_target`，不认 `plan_stop`：**只设了止损不等于给目标也
        # 开了口子**，那会让系统算的目标在开盘头 15 分钟提前触发，而它正是那道
        # 约束要拦的东西。
        "targets_hit": (
            [index for index, level in enumerate(levels) if last >= level]
            if (armed or _manual_targets(holding))
            else []
        ),
        "armed": armed,
        "volume_spike": intraday.volume_ratio is not None and intraday.volume_ratio >= VOLUME_SPIKE_RATIO,
        "at_high": _at_high(intraday),
        "vs_reclaim": _vs_reclaim(advice, last),
        "breakout_confirmed": holding is None and _breakout_confirmed(advice, intraday),
        # 下面几个只对持仓有意义，未持仓恒为 False。
        "stabilised": bool(stabilisation is not None and stabilisation.confirmed),
        "structure_broken": holding is not None and structure_broken(last, daily_support),
        "time_stop_due": time_stop_due(holding, last),
        "giveback_due": bool(giveback is not None and giveback.due),
    }


def _manual_stop(holding: HoldingView | None) -> bool:
    return bool(holding is not None and holding.plan_stop)


def _manual_targets(holding: HoldingView | None) -> bool:
    return bool(holding is not None and holding.plan_target)


def _targets_line(holding: HoldingView | None, targets: list[float]) -> str:
    """目标价那一行。你自己设的要标出来——否则你会以为那是系统算的。"""
    prefix = "目标（你设定的）" if _manual_targets(holding) else "目标"
    return prefix + " " + "、".join(f"{level:.2f}" for level in targets)


def _entry_plan_detail(
    advice: StockAdvice,
    holding: HoldingView | None,
    targets: list[float],
) -> list[str]:
    """Stop, target and R/R lines shared by entry-zone and confirmation alerts."""
    lines: list[str] = []
    if advice.stop is not None:
        gap = (advice.entry_low - advice.stop) / advice.entry_low if advice.entry_low else None
        lines.append(
            f"止损 {advice.stop:.2f}" + (f"（-{gap:.1%}）" if gap is not None else "")
        )
    if targets:
        lines.append(_targets_line(holding, targets))
    # 盈亏比是拿系统那串目标算的。你自己设了目标价时它对不上号，
    # 把两者并排印出来只会让人不知道该信哪个。
    if advice.rrr is not None and not _manual_targets(holding):
        lines.append(f"盈亏比 {advice.rrr}")
    return lines


def _execution_guard_detail(advice: StockAdvice) -> list[str]:
    """Turn the brief's risk language into execution guardrails inside alerts."""
    lines: list[str] = []
    warnings = [str(item) for item in getattr(advice, "warnings", []) or []]
    action = _shortline_action(advice)

    if any("低吸转为接刀" in item for item in warnings):
        lines.append("执行纪律：这是接刀型试错，只能小仓；跌回买入带下沿下方不补仓。")
    if any(("风险收益不划算" in item or "不满足超短线最低要求" in item) for item in warnings):
        lines.append("执行纪律：盈亏比不足，自动降级为观察提醒，不把到位当成必须买。")
    if action == "wait_reclaim":
        level = _reclaim_level(advice)
        if level is not None:
            lines.append(f"执行纪律：弱票先确认站稳 {level:.2f}，否则不追。")
    elif action == "buy_pullback":
        lines.append("执行纪律：只做回踩承接，不追分时拉升。")
    elif action == "breakout_follow":
        lines.append("执行纪律：只在放量突破时跟随，缩量冲高不追。")
    return lines


def _giveback_detail(giveback: Giveback | None) -> list[str]:
    if giveback is None:
        return []
    return [*giveback.describe(), ("止损没破、目标没到、支撑还在、期限没满——四条卖出触发一条都不会响，而浮盈已经吐掉一半"), "系统不替你决定走留：它只负责让这件事不再无声发生"]


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


def _vs_reclaim(advice: StockAdvice, last: float) -> str | None:
    level = _reclaim_level(advice)
    if level is None:
        return None
    return "above" if last >= level else "below"


def _shortline_action(advice: StockAdvice) -> str | None:
    shortline = getattr(advice, "shortline", None)
    value = getattr(shortline, "action", None) if shortline is not None else None
    return str(value) if value else None


def _reclaim_level(advice: StockAdvice) -> float | None:
    shortline = getattr(advice, "shortline", None)
    value = getattr(shortline, "reclaim_level", None) if shortline is not None else None
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _breakout_confirmed(advice: StockAdvice, intraday: IntradaySnapshot) -> bool:
    if _shortline_action(advice) not in BREAKOUT_ACTIONS:
        return False
    if _entry_quality_blocked(advice):
        return False
    if not _at_high(intraday):
        return False
    return not (intraday.volume_ratio is None or intraday.volume_ratio < BREAKOUT_MIN_VOLUME_RATIO)


def _entry_quality_blocked(advice: StockAdvice) -> bool:
    warnings = [str(item) for item in getattr(advice, "warnings", []) or []]
    if any(any(token in warning for token in ENTRY_BLOCKING_WARNINGS) for warning in warnings):
        return True
    rrr = getattr(advice, "rrr", None)
    return isinstance(rrr, (int, float)) and rrr < 1.5


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


def _advice_context_lines(advice: StockAdvice) -> list[str]:
    lines: list[str] = []
    shortline = getattr(advice, "shortline", None)
    if shortline is not None and getattr(shortline, "summary", None):
        lines.append(f"短线判断：{shortline.summary}")
    trade = getattr(advice, "trade_context", None)
    if trade is not None:
        parts = trade_context_parts(
            getattr(trade, "turnover_ratio", None),
            getattr(trade, "session_amount", None),
        )
        if parts:
            lines.append("交易数据：" + "，".join(parts))
    return lines
