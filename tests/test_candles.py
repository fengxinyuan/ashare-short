"""K 线收盘位置及坏数据的缺失语义。"""

from __future__ import annotations

from ashare_decision_support.signals.candles import close_position


def bar(open_: float, high: float, low: float, close: float) -> dict:
    return {"open": open_, "high": high, "low": low, "close": close}


def test_broken_bar_declines_instead_of_guessing() -> None:
    assert close_position(bar(10.0, 9.0, 11.0, 10.5)) is None
    assert close_position({"open": 10.0, "high": 11.0, "low": 9.0}) is None
    assert close_position({"open": 10.0, "high": 11.0, "low": 9.0, "close": "-"}) is None
    assert close_position({"high": 11.0, "low": 9.0, "close": 10.0}) is None
    assert close_position(bar(float("nan"), 11.0, 9.0, 10.0)) is None
    assert close_position(None) is None


def test_close_position_spans_the_day_range() -> None:
    assert close_position(bar(10.0, 12.0, 10.0, 12.0)) == 1.0
    assert close_position(bar(10.0, 12.0, 10.0, 10.0)) == 0.0
    assert close_position(bar(10.0, 12.0, 10.0, 11.0)) == 0.5


def test_flat_bar_has_no_intraday_position() -> None:
    assert close_position(bar(11.0, 11.0, 11.0, 11.0)) is None
