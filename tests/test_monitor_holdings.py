"""持仓监控：已持仓的票要回答的是**什么时候卖**。

四类离场条件缺一不可——止损是底线，目标是兑现，**结构破坏比止损早一步**，
**时间止损管的是那类既不亏也不赚、就那么耗着的仓位**。最后两类此前完全没有。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from ashare_decision_support.monitor.holdings import (
    TIME_STOP_DAYS,
    HoldingView,
    breakeven_lines,
    effective_stop,
    structure_broken,
    time_stop_due,
)
from ashare_decision_support.monitor.triggers import TriggerEngine
from ashare_decision_support.state.alerts import AlertState
from ashare_decision_support.state.positions import Positions

NOW = datetime(2026, 8, 3, 10, 30)


def test_breakeven_context_only_describes_the_path_out_of_a_loss() -> None:
    daily = SimpleNamespace(
        ma5=39.0,
        ma10=39.15,
        ma20=39.01,
        ma60=None,
        resistances=[],
    )

    underwater = breakeven_lines(39.12, 38.02, daily)
    profitable = breakeven_lines(39.12, 39.50, daily)

    assert any("回本还需" in line for line in underwater)
    assert any("重合" in line for line in underwater)
    assert profitable == ["当前浮盈，回本价已在下方"]


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
        advice=advice,
        intraday=intraday,
        state=state,
        now=NOW,
        holding=holding,
        daily_support=support,
    )


def held(**kw):
    d = {
        "symbol": "601939.SH",
        "cost_price": 10.64,
        "shares": 1000,
        "opened_on": "2026-08-01",
        "held_days": 2,
    }
    d.update(kw)
    return HoldingView(**d)


# 你定的止损优先于系统算的


def test_your_own_stop_wins_over_the_computed_one() -> None:
    """手工填的是**决定**，系统算的是**建议**。冲突时听你的——你可能知道系统
    不知道的事（别处的敞口、税、仓位安排）。"""
    assert effective_stop(held(plan_stop=10.20), 9.80) == 10.20
    assert effective_stop(held(), 9.80) == 9.80
    assert effective_stop(None, 9.80) == 9.80


def test_a_manual_stop_fires_before_the_session_has_taken_shape() -> None:
    """plan_stop 是绝对价位，不依赖当日结构成型——开盘第一分钟跌破它也该报。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    holding = held(plan_stop=10.50)

    fire(engine, state, Advice(), Intraday(10.80), holding)  # 建立基线
    alerts = fire(engine, state, Advice(), Intraday(10.40), holding)  # 跌破

    assert [a.trigger for a in alerts] == ["stop_break"]
    assert alerts[0].level == "ACTION"


# 结构破坏：比止损早一步


def test_structure_break_fires_before_price_reaches_the_stop() -> None:
    """支撑是当初买入的**依据**。依据没了就该重新评估，不必等价格走到止损位。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    holding = held(plan_stop=9.80)

    fire(engine, state, Advice(), Intraday(10.80), holding, support=10.50)
    alerts = fire(engine, state, Advice(), Intraday(10.30), holding, support=10.50)

    triggers = [a.trigger for a in alerts]
    assert "structure_break" in triggers
    # 价格 10.30 还没到止损 9.80，止损不该同时触发。
    assert "stop_break" not in triggers


def test_a_small_dip_through_support_is_not_a_structure_break() -> None:
    """日线支撑不是一条精确的线，盘中插一下就报警只会制造噪声。"""
    assert structure_broken(10.49, 10.50) is False  # 差 0.1%，在缓冲内
    assert structure_broken(10.40, 10.50) is True  # 差 1%，算破了
    assert structure_broken(10.40, None) is False  # 没有支撑就不判


def test_structure_break_does_not_fire_for_a_stock_you_do_not_hold() -> None:
    """没持仓时这条没有意义——你还没有需要保护的东西。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    fire(engine, state, Advice(), Intraday(10.80), None, support=10.50)
    alerts = fire(engine, state, Advice(), Intraday(10.30), None, support=10.50)
    assert "structure_break" not in [a.trigger for a in alerts]


# 时间止损：低吸最大的隐性亏损来源


def test_a_position_that_goes_nowhere_for_too_long_is_flagged() -> None:
    """不亏不赚地耗着同样是失败：占着仓位和注意力，而当初「回调结束会反弹」
    的判断已经被时间证伪了。"""
    stale = held(held_days=TIME_STOP_DAYS + 1)
    assert time_stop_due(stale, 10.70) is True  # 持有 9 天只涨 0.6%

    assert time_stop_due(stale, 11.20) is False  # 涨了 5.3%，兑现了
    assert time_stop_due(held(held_days=2), 10.70) is False  # 还没到期


def test_time_stop_is_silent_without_a_cost_price() -> None:
    """算不出浮盈就不催人卖——宁可不报，也不拿猜出来的数做判断。"""
    assert time_stop_due(held(held_days=20, cost_price=None), 10.70) is False


def test_time_stop_fires_on_the_first_pass_because_it_is_a_state() -> None:
    """它在监控启动那一刻就已经成立。按跃迁处理会让它整天不发。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    alerts = fire(engine, state, Advice(), Intraday(10.70), held(held_days=TIME_STOP_DAYS + 1))

    assert [a.trigger for a in alerts] == ["time_stop"]
    assert any("证伪" in line for line in alerts[0].detail)


def test_stop_break_fires_on_the_first_pass_when_already_broken() -> None:
    """监控中午才启动时，已经跌破止损的持仓也必须立刻提醒。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    alerts = fire(engine, state, Advice(stop=10.50), Intraday(10.40), held())

    assert [a.trigger for a in alerts] == ["stop_break"]
    assert "启动监控时已低于止损" in " ".join(alerts[0].detail)


# 持仓上下文进告警


def test_a_holding_alert_carries_cost_and_open_pnl() -> None:
    """不带成本和浮盈，等于让你自己去翻账户对一遍。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    holding = held(plan_stop=10.50)

    fire(engine, state, Advice(), Intraday(10.80), holding)
    alert = fire(engine, state, Advice(), Intraday(10.40), holding)[0]

    assert alert.holding is True
    assert alert.unrealised_pct is not None
    body = " ".join(alert.detail)
    assert "成本 10.64" in body
    assert "亏损" in body


def test_a_warn_level_holding_alert_is_not_filtered_out() -> None:
    """持仓告警不分级别一律要报——漏一条的代价是真金白银，不是错过机会。"""
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    holding = held(held_days=TIME_STOP_DAYS + 1)

    alerts = engine.evaluate(
        advice=Advice(),
        intraday=Intraday(10.70),
        state=state,
        now=NOW,
        holding=holding,
    )
    # time_stop 是 WARN 级，不是 ACTION。
    assert [a.trigger for a in alerts] == ["time_stop"]


# 会话装载


def test_the_session_loads_the_position_table(tmp_path) -> None:
    from ashare_decision_support.monitor.session import MonitorSession

    book = Positions(path=tmp_path / "positions.json")
    book.open("601939.SH", cost_price=10.64, today=date(2026, 8, 1))

    session = MonitorSession(
        adapter=None,
        intraday_adapter=None,
        session_date=date(2026, 8, 3),
        alert_state=AlertState(date(2026, 8, 3), Path("/dev/null")),
    )
    notes = session.load_positions(book)

    assert set(session.holdings) == {"601939.SH"}
    assert session.holdings["601939.SH"].held_days == 2
    assert any("持仓 1 只" in note for note in notes)


def test_no_position_table_means_everything_is_treated_as_unheld(tmp_path) -> None:
    """传 None 不该退化成「你什么都没持有」的静默假设——它是显式的无上下文。"""
    from ashare_decision_support.monitor.session import MonitorSession

    session = MonitorSession(
        adapter=None,
        intraday_adapter=None,
        session_date=date(2026, 8, 3),
        alert_state=AlertState(date(2026, 8, 3), Path("/dev/null")),
    )
    assert session.load_positions(None) == []
    assert session.holdings == {}


def test_a_position_without_a_cost_price_is_called_out(tmp_path) -> None:
    path = tmp_path / "positions.json"
    path.write_text('[{"symbol": "601939.SH"}]', encoding="utf-8")
    book = Positions(path=path)

    from ashare_decision_support.monitor.session import MonitorSession

    session = MonitorSession(
        adapter=None,
        intraday_adapter=None,
        session_date=date(2026, 8, 3),
        alert_state=AlertState(date(2026, 8, 3), Path("/dev/null")),
    )
    notes = session.load_positions(book)
    assert any("没有成本价" in note for note in notes)


def test_holding_alerts_sort_ahead_of_the_rest() -> None:
    """同一批推送里，「你手上那只跌破止损」不该排在「另一只进买区」后面。"""
    from ashare_decision_support.notify.render import render_alerts

    class A:
        def __init__(self, headline, holding):
            self.level = "ACTION"
            self.headline = headline
            self.detail = []
            self.as_of = NOW
            self.holding = holding

    text = render_alerts([A("丸美生物进入买入区间", False), A("建设银行跌破止损", True)], None)
    assert text.index("建设银行") < text.index("丸美生物")
    assert "【持仓】" in text
    assert text.startswith("## 先处理")
    assert "## 触发依据" in text


def test_alert_render_starts_with_action_summary() -> None:
    from ashare_decision_support.notify.render import render_alerts

    class A:
        level = "ACTION"
        headline = "丸美生物 止跌确认，位于买入区间内"
        detail = ["现价 28.28"]
        as_of = NOW
        holding = False
        trigger = "entry_confirmed"
        symbol = "603983.SH"

    text = render_alerts([A()], None)

    summary, _, details = text.partition("## 触发依据")
    assert "买点确认" in summary
    assert "现价 28.28" in details


def test_monitor_alerts_carry_shortline_context_and_trade_data() -> None:
    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    advice = Advice(stop=9.5, targets=[12.0], entry=(10.0, 10.6))
    advice.shortline = type(
        "Shortline", (), {"summary": "短线偏弱，不宜追买；先等收回 10.50 再看修复"}
    )()
    advice.trade_context = type(
        "TradeContext", (), {"turnover_ratio": 3.2, "session_amount": 560000000.0}
    )()

    fire(engine, state, advice, Intraday(11.0))
    alert = fire(engine, state, advice, Intraday(10.3))[0]

    body = " ".join(alert.detail)
    assert "短线判断：短线偏弱，不宜追买；先等收回 10.50 再看修复" in body
    assert "交易数据：换手率 3.20%，成交额 5.60 亿" in body


# L3 止跌确认：进入区间不等于可以买


def minutes(prices, volumes=None, span=0.004):
    vols = volumes or [1000.0] * len(prices)
    return [
        {"high": p * (1 + span), "low": p * (1 - span), "close": p, "volume": v}
        for p, v in zip(prices, vols, strict=True)
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
        PATTERN_CONTRACTION,
        assess_stabilisation,
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
        PATTERN_REVERSAL,
        assess_stabilisation,
    )

    prices = [100.0 - i * 0.2 for i in range(34)] + [98.0]
    vols = [1000.0] * 34 + [5000.0]
    reading = assess_stabilisation(minutes(prices, vols))

    assert reading.confirmed is True
    assert reading.pattern == PATTERN_REVERSAL


def test_a_confirmed_signal_says_how_long_since_the_low() -> None:
    """止跌信号的成色随时间变，而三种形态里只有最弱的那个说过这件事。

    低点后第 2 根就确认，和第 15 根才确认，是两种强度——前者很可能只是下跌途中
    的一次反抽。`bars_since_low` 三种形态本来都算了，此前只有「缩量止跌」把它写
    进自己的 reason，**放量反包与二次探底不破一个字都没有**，而恰恰是这两个会被
    拿去当买点。
    """
    from ashare_decision_support.signals.stabilise import (
        PATTERN_REVERSAL,
        assess_stabilisation,
    )

    prices = [100.0 - i * 0.2 for i in range(34)] + [98.0]
    vols = [1000.0] * 34 + [5000.0]
    reading = assess_stabilisation(minutes(prices, vols))

    assert reading.pattern == PATTERN_REVERSAL
    assert reading.bars_since_low is not None
    assert f"距当日最低 {reading.bars_since_low} 根分时" in reading.describe()


def test_the_age_is_not_printed_twice_for_the_weakest_pattern() -> None:
    """缩量止跌的 reason 原来自带「N 根未创新低」，收口后不能变成印两遍。"""
    from ashare_decision_support.signals.stabilise import (
        PATTERN_CONTRACTION,
        assess_stabilisation,
    )

    prices = [100.0 - i * 0.4 for i in range(20)] + [92.2 + i * 0.01 for i in range(15)]
    vols = [3000.0] * 20 + [600.0] * 15
    reading = assess_stabilisation(minutes(prices, vols))

    assert reading.pattern == PATTERN_CONTRACTION
    assert reading.describe().count("根分时") == 1
    assert "未创新低" not in reading.reason


def test_an_unconfirmed_reading_does_not_claim_an_age() -> None:
    """还没止跌时不追加「距最低 N 根」——那会读成一个已经站住的信号。"""
    from ashare_decision_support.signals.stabilise import assess_stabilisation

    falling = [100.0 - i * 0.3 for i in range(40)]
    reading = assess_stabilisation(minutes(falling))

    assert reading.confirmed is False
    assert "距当日最低" not in reading.describe()


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
    advice = Advice(stop=9.5, targets=[12.5], entry=(10.0, 10.6))
    unconfirmed = Stabilisation(confirmed=False, reason="刚创新低")

    fire(engine, state, advice, Intraday(11.0))  # 区间上方，建立基线
    alerts = engine.evaluate(
        advice=advice,
        intraday=Intraday(10.3),
        state=state,
        now=NOW,
        stabilisation=unconfirmed,
    )

    assert [a.trigger for a in alerts] == ["entry_watch"]
    assert alerts[0].level == "WARN"


def test_stabilising_inside_the_band_is_the_actual_buy_signal() -> None:
    """低吸最典型的顺序是先跌进来、再等企稳。只看「进入区间」那一刻会永远错过它。"""
    from ashare_decision_support.signals.stabilise import Stabilisation

    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    advice = Advice(stop=9.5, targets=[12.5], entry=(10.0, 10.6))

    # 进区间时还没止跌
    fire(engine, state, advice, Intraday(11.0))
    engine.evaluate(
        advice=advice,
        intraday=Intraday(10.3),
        state=state,
        now=NOW,
        stabilisation=Stabilisation(confirmed=False, reason="刚创新低"),
    )
    # 仍在区间内，随后出现止跌
    alerts = engine.evaluate(
        advice=advice,
        intraday=Intraday(10.35),
        state=state,
        now=NOW,
        stabilisation=Stabilisation(confirmed=True, pattern="放量反包", reason="量能 3.2 倍"),
    )

    triggers = [a.trigger for a in alerts]
    assert "entry_confirmed" in triggers
    assert next(a for a in alerts if a.trigger == "entry_confirmed").level == "ACTION"


def test_reclaiming_the_watch_line_is_a_warn_not_a_buy_signal() -> None:
    """弱票先收回观察线只是从「别碰」变成「继续盯」，不能自动升级成买点。"""
    from ashare_decision_support.notify.render import render_alerts

    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    advice = Advice(stop=9.5, targets=[], entry=(11.0, 11.5))
    advice.shortline = type(
        "Shortline",
        (),
        {
            "action": "wait_reclaim",
            "reclaim_level": 10.50,
            "summary": "短线偏弱，不宜追买；先等收回 10.50 再看修复",
        },
    )()

    fire(engine, state, advice, Intraday(10.20, vwap=10.0))
    alerts = fire(engine, state, advice, Intraday(10.55, vwap=10.0))

    assert [a.trigger for a in alerts] == ["reclaim_confirmed"]
    assert alerts[0].level == "WARN"
    body = " ".join(alerts[0].detail)
    assert "不是追高买点" in body
    assert "弱票先确认站稳 10.50" in body
    assert "修复观察" in render_alerts(alerts, None)


def test_reaching_target_far_above_entry_band_does_not_become_a_new_breakout_buy() -> None:
    """到目标价时优先报目标，不能在计划买入带上方追发突破买点。"""
    from ashare_decision_support.notify.render import render_alerts

    engine, state = (
        TriggerEngine(levels=("ACTION",)),
        AlertState(date(2026, 8, 3), Path("/dev/null")),
    )
    advice = Advice(stop=9.5, targets=[10.8], entry=(10.0, 10.2))
    advice.rrr = 2.2
    advice.shortline = type(
        "Shortline",
        (),
        {"action": "buy_pullback", "reclaim_level": 10.2, "summary": "趋势未坏，回踩可观察承接"},
    )()

    fire(engine, state, advice, Intraday(10.30, vwap=10.0))
    second = Intraday(10.80, vwap=10.4)
    second.session_high = 10.80
    second.session_low = 10.20
    second.volume_ratio = 1.35
    alerts = fire(engine, state, advice, second)

    assert [a.trigger for a in alerts] == ["target_hit:0"]
    assert "突破确认" not in render_alerts(alerts, None)


def test_low_rrr_names_do_not_get_breakout_action_alerts() -> None:
    """盈亏比太差的票就算拉到日内高点，也只应按原有观察/目标逻辑处理。"""
    engine, state = (
        TriggerEngine(levels=("ACTION",)),
        AlertState(date(2026, 8, 3), Path("/dev/null")),
    )
    advice = Advice(stop=9.5, targets=[], entry=(10.0, 10.2))
    advice.rrr = 0.6
    advice.shortline = type(
        "Shortline",
        (),
        {"action": "buy_pullback", "reclaim_level": 10.2, "summary": "趋势未坏，但性价比不足"},
    )()

    fire(engine, state, advice, Intraday(10.30, vwap=10.0))
    second = Intraday(10.80, vwap=10.4)
    second.session_high = 10.80
    second.session_low = 10.20
    second.volume_ratio = 1.35

    assert fire(engine, state, advice, second) == []


def test_low_rrr_names_do_not_get_entry_action_alerts() -> None:
    """质量闸门此前只接在突破路径上，低吸这一路是漏的：一只盈亏比 1.09、命中
    业绩否决的票跌进买入区间，照样会推 ACTION 级「进入买入区间」和「止跌确认」，
    而同一份数据下简报写的是「今日无可用打法」。区间确实进了，所以照发，
    但降级成 WARN 的 entry_blocked。"""
    from ashare_decision_support.signals.stabilise import Stabilisation

    engine = TriggerEngine(levels=("ACTION", "WARN"))
    state = AlertState(date(2026, 8, 3), Path("/dev/null"))
    advice = Advice(stop=9.5, targets=[10.8], entry=(10.0, 10.6))
    advice.rrr = 1.09

    fire(engine, state, advice, Intraday(10.90, vwap=10.9))
    alerts = engine.evaluate(
        advice=advice,
        intraday=Intraday(10.30, vwap=10.4),
        state=state,
        now=NOW,
        stabilisation=Stabilisation(confirmed=True, pattern="放量反包", reason="量能 3.2 倍"),
    )

    triggers = [a.trigger for a in alerts]
    assert "entry_blocked" in triggers
    assert "entry_zone" not in triggers
    assert "entry_confirmed" not in triggers
    assert next(a for a in alerts if a.trigger == "entry_blocked").level == "WARN"


def test_low_rrr_names_are_blocked_on_the_first_pass_too() -> None:
    """启动补报那一路是另一处 emit，要一起堵——否则重启一次监控就绕过了闸门。"""
    from ashare_decision_support.signals.stabilise import Stabilisation

    engine = TriggerEngine(levels=("ACTION", "WARN"))
    state = AlertState(date(2026, 8, 3), Path("/dev/null"))
    advice = Advice(stop=9.5, targets=[12.0], entry=(10.0, 10.6))
    advice.rrr = 1.09

    alerts = engine.evaluate(
        advice=advice,
        intraday=Intraday(10.35),
        state=state,
        now=NOW,
        stabilisation=Stabilisation(confirmed=True, pattern="放量反包", reason="量能 3.2 倍"),
    )

    assert [a.trigger for a in alerts] == ["entry_blocked"]
    assert alerts[0].level == "WARN"


def test_earnings_veto_also_blocks_the_entry_action() -> None:
    """闸门的另一半是警告文案，不只是盈亏比。业绩否决原先不在拦截清单里，
    所以一只盈亏比漂亮、但中报预告首亏的票照样能推 ACTION 买点——而这条否决
    比盈亏比更硬：盈亏比是算出来的估计，首亏是公司自己签字披露的事实。"""
    from ashare_decision_support.signals.stabilise import Stabilisation

    engine = TriggerEngine(levels=("ACTION", "WARN"))
    state = AlertState(date(2026, 8, 3), Path("/dev/null"))
    advice = Advice(stop=9.5, targets=[12.0], entry=(10.0, 10.6))
    advice.rrr = 3.0
    advice.warnings = ["⚠️ 业绩否决：预告首亏 -158.9%，2026-07-11 披露（盘前链路不会选它）"]

    alerts = engine.evaluate(
        advice=advice,
        intraday=Intraday(10.35),
        state=state,
        now=NOW,
        stabilisation=Stabilisation(confirmed=True, pattern="放量反包", reason="量能 3.2 倍"),
    )

    assert [a.trigger for a in alerts] == ["entry_blocked"]


def test_confirmed_entry_fires_on_the_first_pass_when_already_valid() -> None:
    """监控不是 09:35 准点启动时，已经成立的买点不能等下一次穿越才提示。"""
    from ashare_decision_support.signals.stabilise import Stabilisation

    engine, state = TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))
    advice = Advice(stop=9.5, targets=[12.5], entry=(10.0, 10.6))
    advice.rrr = 2.5

    alerts = engine.evaluate(
        advice=advice,
        intraday=Intraday(10.35),
        state=state,
        now=NOW,
        stabilisation=Stabilisation(confirmed=True, pattern="放量反包", reason="量能 3.2 倍"),
    )

    assert [a.trigger for a in alerts] == ["entry_confirmed"]
    body = " ".join(alerts[0].detail)
    assert "启动监控时买点已经成立" in body
    assert "止损 9.50" in body
    assert "目标 12.50" in body
