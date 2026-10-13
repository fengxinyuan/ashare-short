"""`advise` 对已登记持仓：你定的止损/目标优先，成本与浮盈进简报。"""

from __future__ import annotations

from datetime import date

from ashare_decision_support.jobs.advise import run_advise
from ashare_decision_support.notify.render import render_advice_brief
from ashare_decision_support.state.positions import Positions
from tests.test_automation import RISE, SESSION, StubDaily, minute_bars


class _StubIntraday:
    def get_minute_bars(self, symbols, period="5", **kwargs):
        return {symbol: minute_bars(RISE) for symbol in symbols}, []


def _advise(positions=None):
    return run_advise(
        adapter=StubDaily(),
        symbols=["600519.SH"],
        intraday_adapter=_StubIntraday(),
        use_news=False,
        use_order_book=False,
        notifiers=[],
        trade_date=SESSION,
        positions=positions,
    )


def test_registered_position_overrides_levels_and_shows_cost(tmp_path) -> None:
    baseline = _advise()["facts"]["symbols"][0]
    assert baseline["holding"] is None
    assert baseline["stop"] is not None

    positions = Positions(tmp_path / "positions.json")
    positions.open(
        "600519.SH",
        cost_price=98.0,
        shares=200,
        opened_on=date(2026, 7, 30),
        plan_stop=96.5,
        plan_target=110.0,
        today=SESSION,
    )
    summary = _advise(positions)
    item = summary["facts"]["symbols"][0]

    assert item["stop"] == 96.5
    assert item["targets"] == [110.0]
    assert item["holding"]["cost_price"] == 98.0
    assert item["holding"]["held_days"] == 4
    assert item["warnings"][0].startswith("你设定的 止损 96.50、目标 110.00 已替换系统价位")
    # 盈亏比按替换后的价位重算，不再是系统那份。
    assert item["rrr"] != baseline["rrr"]
    assert "持仓：成本 98.00" in summary["content"]


def test_position_without_manual_levels_keeps_system_levels(tmp_path) -> None:
    positions = Positions(tmp_path / "positions.json")
    positions.open("600519.SH", cost_price=98.0, today=SESSION)
    baseline = _advise()["facts"]["symbols"][0]
    item = _advise(positions)["facts"]["symbols"][0]

    assert item["stop"] == baseline["stop"]
    assert item["targets"] == baseline["targets"]
    assert item["holding"]["cost_price"] == 98.0
    assert not any("已替换系统价位" in text for text in item["warnings"])


def test_render_puts_holding_and_breakeven_on_their_own_lines() -> None:
    from ashare_decision_support.advice.engine import StockAdvice

    item = StockAdvice(
        symbol="600519.SH",
        name="测试",
        trend_state="UPTREND",
        holding={"lines": ["成本 98.00，现价 95.00，浮动亏损 -3.06%", "已持有 4 天"]},
        breakeven_lines=["回本还需 +3.16%", "MA20 39.01 压在回本价附近"],
    )
    text = render_advice_brief(date(2026, 8, 3), [item])
    assert "- 持仓：成本 98.00，现价 95.00，浮动亏损 -3.06%；已持有 4 天" in text
    assert "- 回本：回本还需 +3.16%" in text


def test_breached_manual_stop_is_said_outright_and_disqualifies_the_plan(tmp_path) -> None:
    from ashare_decision_support.jobs.advise import _risk_qualified_plan

    baseline = _advise()["facts"]["symbols"][0]
    price = baseline["last_price"]
    positions = Positions(tmp_path / "positions.json")
    positions.open("600519.SH", cost_price=price * 1.05, plan_stop=price * 1.01, today=SESSION)

    summary = _advise(positions)
    item = summary["facts"]["symbols"][0]

    assert item["warnings"][0].startswith("⚠️ 现价")
    assert "已跌破你设定的止损" in item["warnings"][0]
    assert not any(text.startswith("止损距离仅") for text in item["warnings"])
    assert summary["risk_qualified_plans"] == 0
    # 引擎对象层面也要被判为不合格，不只是计数。
    from ashare_decision_support.advice.engine import StockAdvice

    advice = StockAdvice(symbol="600519.SH", warnings=list(item["warnings"]))
    assert not _risk_qualified_plan(advice)


def test_reached_manual_target_is_flagged(tmp_path) -> None:
    baseline = _advise()["facts"]["symbols"][0]
    price = baseline["last_price"]
    positions = Positions(tmp_path / "positions.json")
    positions.open("600519.SH", cost_price=price * 0.9, plan_target=price * 0.99, today=SESSION)

    item = _advise(positions)["facts"]["symbols"][0]
    assert any("已到你设定的目标" in text for text in item["warnings"])
