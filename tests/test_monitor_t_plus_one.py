"""T+1：今天建的仓今天卖不出。

止损、结构破坏、浮盈回吐、触及目标四条卖出告警都要求一个卖出动作，而建仓当天
这个动作做不成。告警照发、级别不降，但末尾必须说明这是明日开盘的处置提示。
"""

from __future__ import annotations

from datetime import date

from ashare_decision_support.monitor.holdings import HoldingView, t_plus_one_lines
from ashare_decision_support.monitor.triggers import TriggerEngine
from ashare_decision_support.state.positions import Position
from tests.test_monitor_holdings import Intraday
from tests.test_monitor_sell_side import Advice, fire, held, state

TODAY = date(2026, 8, 3)


def test_a_position_opened_today_is_locked_and_older_ones_are_not() -> None:
    opened_today = HoldingView.from_position(
        Position(symbol="601939.SH", cost_price=10.0, opened_on=str(TODAY)), today=TODAY
    )
    opened_earlier = HoldingView.from_position(
        Position(symbol="601939.SH", cost_price=10.0, opened_on="2026-08-01"), today=TODAY
    )
    unknown = HoldingView.from_position(Position(symbol="601939.SH", cost_price=10.0), today=TODAY)

    assert opened_today.t_plus_one_locked is True
    assert opened_earlier.t_plus_one_locked is False
    # 建仓日未知：不知道不等于锁定，告警照常、不加提示。
    assert unknown.t_plus_one_locked is False
    assert t_plus_one_lines(unknown) == []
    assert t_plus_one_lines(None) == []


def test_a_stop_break_on_the_opening_day_is_marked_as_tomorrows_business() -> None:
    engine, st = TriggerEngine(), state()
    holding = held(opened_on=str(TODAY), held_days=0)

    fire(engine, st, Advice(stop=9.00), Intraday(10.00), holding)
    alerts = fire(engine, st, Advice(stop=9.00), Intraday(8.90), holding)

    assert [a.trigger for a in alerts] == ["stop_break"]
    # 级别不降：卖不出不等于不重要，它是明早开盘第一件要处理的事。
    assert alerts[0].level == "ACTION"
    body = " ".join(alerts[0].detail)
    assert "T+1" in body and "今天卖不出" in body and "开盘价" in body


def test_older_positions_do_not_get_the_t_plus_one_note() -> None:
    engine, st = TriggerEngine(), state()
    holding = held(held_days=2)

    fire(engine, st, Advice(stop=9.00), Intraday(10.00), holding)
    alerts = fire(engine, st, Advice(stop=9.00), Intraday(8.90), holding)

    assert [a.trigger for a in alerts] == ["stop_break"]
    assert "T+1" not in " ".join(alerts[0].detail)


def test_the_startup_report_of_an_existing_breach_carries_the_note_too() -> None:
    """午后才开的监控，第一轮就发现已经跌破止损——补报那条同样是今天卖不出的。"""
    engine, st = TriggerEngine(), state()
    holding = held(opened_on=str(TODAY), held_days=0)

    alerts = fire(engine, st, Advice(stop=9.00), Intraday(8.90), holding)

    assert [a.trigger for a in alerts] == ["stop_break"]
    assert "T+1" in " ".join(alerts[0].detail)


def test_target_and_giveback_alerts_carry_the_note_as_well() -> None:
    engine, st = TriggerEngine(), state()
    holding = held(opened_on=str(TODAY), held_days=0, plan_target=11.60)

    fire(engine, st, Advice(), Intraday(11.00), holding)
    alerts = fire(engine, st, Advice(), Intraday(11.65), holding)

    assert [a.trigger for a in alerts] == ["target_hit:0"]
    assert "T+1" in " ".join(alerts[0].detail)

    engine, st = TriggerEngine(), state()
    alerts = fire(engine, st, Advice(), Intraday(10.40), holding, peak_price=11.00, atr=0.30)

    assert [a.trigger for a in alerts] == ["giveback"]
    assert "T+1" in " ".join(alerts[0].detail)


def test_buy_side_alerts_for_names_you_do_not_hold_never_mention_t_plus_one() -> None:
    engine, st = TriggerEngine(), state()

    fire(engine, st, Advice(entry=(9.80, 10.00)), Intraday(10.50), None)
    alerts = fire(engine, st, Advice(entry=(9.80, 10.00)), Intraday(9.95), None)

    assert all("T+1" not in " ".join(alert.detail) for alert in alerts)
