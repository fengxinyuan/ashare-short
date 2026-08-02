"""持仓监控：已持仓的票要回答的是**什么时候卖**。

四类离场条件缺一不可——止损是底线，目标是兑现，**结构破坏比止损早一步**，
**时间止损管的是那类既不亏也不赚、就那么耗着的仓位**。最后两类此前完全没有。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from ashare_decision_support.monitor.holdings import (
    TIME_STOP_DAYS,
    HoldingView,
    effective_stop,
    structure_broken,
    time_stop_due,
)
from ashare_decision_support.monitor.triggers import TriggerEngine
from ashare_decision_support.state.alerts import AlertState
from ashare_decision_support.state.watchlist import Watchlist

NOW = datetime(2026, 8, 3, 10, 30)


class Advice:
    def __init__(self, stop=None, targets=(), entry=(None, None)):
        self.symbol = "601939.SH"
        self.name = "建设银行"
        self.stop = stop
        self.targets = list(targets)
        self.entry_low, self.entry_high = entry
        self.rrr = None
        self.trend_state = "REBOUND"
        self.scenarios = []
        self.warnings = []


class Intraday:
    def __init__(self, price, vwap=None):
        self.last_price = price
        self.vwap = vwap if vwap is not None else price
        self.session_high = price * 1.01
        self.session_low = price * 0.99
        self.volume_ratio = 1.0
        self.last_timestamp = NOW
        self.session_date = date(2026, 8, 3)
        self.bar_count = 60


def fire(engine, state, advice, intraday, holding=None, support=None):
    return engine.evaluate(
        advice=advice, intraday=intraday, state=state, now=NOW,
        watchlist=True, holding=holding, daily_support=support,
    )


def held(**kw):
    d = dict(symbol="601939.SH", cost_price=10.64, shares=1000,
             opened_on="2026-08-01", held_days=2)
    d.update(kw)
    return HoldingView(**d)


# ---- 你定的止损优先于系统算的 -----------------------------------------------


def test_your_own_stop_wins_over_the_computed_one() -> None:
    """手工填的是**决定**，系统算的是**建议**。冲突时听你的——你可能知道系统
    不知道的事（别处的敞口、税、仓位安排）。"""
    assert effective_stop(held(plan_stop=10.20), 9.80) == 10.20
    assert effective_stop(held(), 9.80) == 9.80
    assert effective_stop(None, 9.80) == 9.80


def test_a_manual_stop_fires_before_the_session_has_taken_shape() -> None:
    """plan_stop 是绝对价位，不依赖当日结构成型——开盘第一分钟跌破它也该报。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path('/dev/null'))
    holding = held(plan_stop=10.50)

    fire(engine, state, Advice(), Intraday(10.80), holding)          # 建立基线
    alerts = fire(engine, state, Advice(), Intraday(10.40), holding)  # 跌破

    assert [a.trigger for a in alerts] == ["stop_break"]
    assert alerts[0].level == "ACTION"


# ---- 结构破坏：比止损早一步 --------------------------------------------------


def test_structure_break_fires_before_price_reaches_the_stop() -> None:
    """支撑是当初买入的**依据**。依据没了就该重新评估，不必等价格走到止损位。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path('/dev/null'))
    holding = held(plan_stop=9.80)

    fire(engine, state, Advice(), Intraday(10.80), holding, support=10.50)
    alerts = fire(engine, state, Advice(), Intraday(10.30), holding, support=10.50)

    triggers = [a.trigger for a in alerts]
    assert "structure_break" in triggers
    # 价格 10.30 还没到止损 9.80，止损不该同时触发。
    assert "stop_break" not in triggers


def test_a_small_dip_through_support_is_not_a_structure_break() -> None:
    """日线支撑不是一条精确的线，盘中插一下就报警只会制造噪声。"""
    assert structure_broken(10.49, 10.50) is False   # 差 0.1%，在缓冲内
    assert structure_broken(10.40, 10.50) is True    # 差 1%，算破了
    assert structure_broken(10.40, None) is False    # 没有支撑就不判


def test_structure_break_does_not_fire_for_a_stock_you_do_not_hold() -> None:
    """没持仓时这条没有意义——你还没有需要保护的东西。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path('/dev/null'))
    fire(engine, state, Advice(), Intraday(10.80), None, support=10.50)
    alerts = fire(engine, state, Advice(), Intraday(10.30), None, support=10.50)
    assert "structure_break" not in [a.trigger for a in alerts]


# ---- 时间止损：低吸最大的隐性亏损来源 ----------------------------------------


def test_a_position_that_goes_nowhere_for_too_long_is_flagged() -> None:
    """不亏不赚地耗着同样是失败：占着仓位和注意力，而当初「回调结束会反弹」
    的判断已经被时间证伪了。"""
    stale = held(held_days=TIME_STOP_DAYS + 1)
    assert time_stop_due(stale, 10.70) is True      # 持有 9 天只涨 0.6%

    assert time_stop_due(stale, 11.20) is False     # 涨了 5.3%，兑现了
    assert time_stop_due(held(held_days=2), 10.70) is False   # 还没到期


def test_time_stop_is_silent_without_a_cost_price() -> None:
    """算不出浮盈就不催人卖——宁可不报，也不拿猜出来的数做判断。"""
    assert time_stop_due(held(held_days=20, cost_price=None), 10.70) is False


def test_time_stop_fires_on_the_first_pass_because_it_is_a_state() -> None:
    """它在监控启动那一刻就已经成立。按跃迁处理会让它整天不发。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path('/dev/null'))
    alerts = fire(engine, state, Advice(), Intraday(10.70), held(held_days=TIME_STOP_DAYS + 1))

    assert [a.trigger for a in alerts] == ["time_stop"]
    assert any("证伪" in line for line in alerts[0].detail)


# ---- 持仓上下文进告警 --------------------------------------------------------


def test_a_holding_alert_carries_cost_and_open_pnl() -> None:
    """不带成本和浮盈，等于让你自己去翻账户对一遍。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path('/dev/null'))
    holding = held(plan_stop=10.50)

    fire(engine, state, Advice(), Intraday(10.80), holding)
    alert = fire(engine, state, Advice(), Intraday(10.40), holding)[0]

    assert alert.holding is True
    assert alert.unrealised_pct is not None
    body = " ".join(alert.detail)
    assert "成本 10.64" in body
    assert "亏损" in body


def test_holdings_are_never_filtered_down_to_action_only() -> None:
    """候选池只推 ACTION 是因为那是机器的选股。持仓不适用同一条——
    漏报一条持仓告警的代价是真金白银，不是错过机会。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path('/dev/null'))
    holding = held(held_days=TIME_STOP_DAYS + 1)

    # watchlist=False 模拟「不在自选列表里但确实持有」。
    alerts = engine.evaluate(
        advice=Advice(), intraday=Intraday(10.70), state=state, now=NOW,
        watchlist=False, holding=holding,
    )
    # time_stop 是 WARN 级，若按候选池规则会被滤掉。
    assert [a.trigger for a in alerts] == ["time_stop"]


# ---- 会话装载 ---------------------------------------------------------------


def test_the_session_separates_held_names_from_the_rest(tmp_path) -> None:
    from ashare_decision_support.monitor.session import MonitorSession

    book = Watchlist(path=tmp_path / "watchlist.json")
    book.add("601939.SH")
    book.add("600900.SH")
    book.set_position("601939.SH", cost_price=10.64, today=date(2026, 8, 1))

    session = MonitorSession(
        adapter=None, intraday_adapter=None, session_date=date(2026, 8, 3),
        alert_state=AlertState(date(2026, 8, 3), Path('/dev/null')),
    )
    notes = session.load_positions(book)

    assert set(session.holdings) == {"601939.SH"}
    assert session.holdings["601939.SH"].held_days == 2
    assert any("持仓 1 只" in note for note in notes)


def test_no_watchlist_means_everything_is_treated_as_unheld(tmp_path) -> None:
    """传 None 不该退化成「你什么都没持有」的静默假设——它是显式的无上下文。"""
    from ashare_decision_support.monitor.session import MonitorSession

    session = MonitorSession(
        adapter=None, intraday_adapter=None, session_date=date(2026, 8, 3),
        alert_state=AlertState(date(2026, 8, 3), Path('/dev/null')),
    )
    assert session.load_positions(None) == []
    assert session.holdings == {}


def test_a_position_without_a_cost_price_is_called_out(tmp_path) -> None:
    book = Watchlist(path=tmp_path / "watchlist.json")
    entry, _ = book.add("601939.SH")
    entry.holding = True
    book.save()

    from ashare_decision_support.monitor.session import MonitorSession

    session = MonitorSession(
        adapter=None, intraday_adapter=None, session_date=date(2026, 8, 3),
        alert_state=AlertState(date(2026, 8, 3), Path('/dev/null')),
    )
    notes = session.load_positions(book)
    assert any("没有成本价" in note for note in notes)


def test_holding_alerts_sort_ahead_of_candidate_alerts() -> None:
    """同一批推送里，「你手上那只跌破止损」不该排在「候选池某只进买区」后面。"""
    from ashare_decision_support.notify.render import render_alerts

    class A:
        def __init__(self, headline, holding, watchlist=True):
            self.level = "ACTION"
            self.headline = headline
            self.detail = []
            self.as_of = NOW
            self.holding = holding
            self.watchlist = watchlist

    text = render_alerts(
        [A("候选票进入买入区间", False, watchlist=False), A("建设银行跌破止损", True)], None
    )
    assert text.index("建设银行") < text.index("候选票")
    assert "【持仓】" in text


# ---- L3 止跌确认：进入区间不等于可以买 --------------------------------------


def minutes(prices, volumes=None, span=0.004):
    vols = volumes or [1000.0] * len(prices)
    return [
        {"high": p * (1 + span), "low": p * (1 - span), "close": p, "volume": v}
        for p, v in zip(prices, vols)
    ]


def test_a_falling_tape_is_not_confirmed_however_deep_it_has_gone() -> None:
    """下跌途中的任何一点都「回调到位过」。接刀和低吸的区别只在于跌势有没有停。"""
    from ashare_decision_support.signals.stabilise import assess_stabilisation

    falling = [100.0 - i * 0.3 for i in range(40)]
    reading = assess_stabilisation(minutes(falling))

    assert reading.confirmed is False
    assert reading.insufficient is False
    assert "创新低" in reading.reason


def test_volume_drying_up_without_new_lows_counts_as_stabilising() -> None:
    """卖压自然衰竭：没人急着出了。"""
    from ashare_decision_support.signals.stabilise import (
        PATTERN_CONTRACTION, assess_stabilisation,
    )

    prices = [100.0 - i * 0.4 for i in range(20)] + [92.2 + i * 0.01 for i in range(15)]
    vols = [3000.0] * 20 + [600.0] * 15
    reading = assess_stabilisation(minutes(prices, vols))

    assert reading.confirmed is True
    assert reading.pattern == PATTERN_CONTRACTION
    assert reading.volume_contraction < 0.7


def test_a_volume_reversal_bar_confirms_immediately() -> None:
    """有资金主动买回来——比缩量止跌更硬的信号。"""
    from ashare_decision_support.signals.stabilise import (
        PATTERN_REVERSAL, assess_stabilisation,
    )

    prices = [100.0 - i * 0.2 for i in range(34)] + [98.0]
    vols = [1000.0] * 34 + [5000.0]
    reading = assess_stabilisation(minutes(prices, vols))

    assert reading.confirmed is True
    assert reading.pattern == PATTERN_REVERSAL


def test_insufficient_data_is_not_reported_as_no_signal() -> None:
    """把「不知道」当成「没有」，会在数据源出问题时静默地不再产生任何买点。"""
    from ashare_decision_support.signals.stabilise import assess_stabilisation

    reading = assess_stabilisation(minutes([100.0] * 5))
    assert reading.insufficient is True
    assert reading.confirmed is False
    assert "不足" in reading.describe()

    assert assess_stabilisation([]).insufficient is True


def test_entering_the_band_without_stabilisation_is_downgraded() -> None:
    """未止跌时按 ACTION 发，会在一路向下的过程中连着响，每次都像机会。"""
    from ashare_decision_support.signals.stabilise import Stabilisation

    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    advice = Advice(stop=9.5, targets=[12.0], entry=(10.0, 10.6))
    unconfirmed = Stabilisation(confirmed=False, reason="刚创新低")

    fire(engine, state, advice, Intraday(11.0))  # 区间上方，建立基线
    alerts = engine.evaluate(
        advice=advice, intraday=Intraday(10.3), state=state, now=NOW,
        stabilisation=unconfirmed,
    )

    assert [a.trigger for a in alerts] == ["entry_watch"]
    assert alerts[0].level == "WARN"


def test_stabilising_inside_the_band_is_the_actual_buy_signal() -> None:
    """低吸最典型的顺序是先跌进来、再等企稳。只看「进入区间」那一刻会永远错过它。"""
    from ashare_decision_support.signals.stabilise import Stabilisation

    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    advice = Advice(stop=9.5, targets=[12.0], entry=(10.0, 10.6))

    # 进区间时还没止跌
    fire(engine, state, advice, Intraday(11.0))
    engine.evaluate(advice=advice, intraday=Intraday(10.3), state=state, now=NOW,
                    stabilisation=Stabilisation(confirmed=False, reason="刚创新低"))
    # 仍在区间内，随后出现止跌
    alerts = engine.evaluate(
        advice=advice, intraday=Intraday(10.35), state=state, now=NOW,
        stabilisation=Stabilisation(confirmed=True, pattern="放量反包", reason="量能 3.2 倍"),
    )

    triggers = [a.trigger for a in alerts]
    assert "entry_confirmed" in triggers
    assert next(a for a in alerts if a.trigger == "entry_confirmed").level == "ACTION"
