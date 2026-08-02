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
