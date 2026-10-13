"""执行复盘不能把未下单、未触发或尚未卖出的情景变成已实现收益。"""

from __future__ import annotations

from datetime import date

import pytest

from ashare_decision_support.advice.engine import StockAdvice
from ashare_decision_support.jobs.review import render_review
from ashare_decision_support.journal import Journal, verify_pending
from ashare_decision_support.journal.records import Recommendation
from ashare_decision_support.journal.stats import summarize
from ashare_decision_support.journal.verify import (
    EXEC_ABOVE_BREAKOUT,
    EXEC_ABSTAINED,
    EXEC_BELOW_BAND,
    EXEC_BREAKOUT_NOT_TRIGGERED,
    EXEC_FILL_BREAKOUT,
    EXEC_INVALID_PLAN,
    EXEC_NO_PLAN,
    EXEC_NO_SETUP,
    EXEC_VETOED,
    EXIT_LIMIT_LOCKED,
    EXIT_STOP,
    EXIT_T1_LOCKED,
    EXIT_TARGET,
    EXIT_TIME,
    EXIT_UNCERTAIN,
    _resolve,
)


def bar(day: int, opening=100.0, high=103.0, low=99.0, close=101.0) -> dict:
    return {
        "date": f"2026-08-{day:02}",
        "open": opening,
        "high": high,
        "low": low,
        "close": close,
    }


def outcome(rows: list[dict] | None = None, horizon: int = 5, **fields) -> dict:
    plan = {
        "symbol": "600519.SH",
        "source": "morning",
        "trade_date": "2026-08-17",
        "reference_date": "2026-08-14",
        "reference_price": 100.0,
        "stop_pct": -0.04,
        "target_pcts": [0.10],
    }
    rows = rows or [bar(14, close=100.0), *(bar(day) for day in (17, 18, 19, 20, 21, 24))]
    return _resolve(plan | fields, rows, date(2026, 8, 31), horizon).to_dict()


def test_t_plus_one_floating_profit_is_not_a_realized_win_or_curve_input() -> None:
    row = outcome(horizon=1)
    execution = row["execution"]
    assert execution["exit"] == EXIT_T1_LOCKED
    assert execution["realized"] is False
    assert execution["exit_session"] is None
    assert execution["unrealized_return_pct"] == pytest.approx(0.01)
    assert execution["realized_return_pct"] is None

    stats = summarize([row])
    result = stats["overall"]["execution"]
    assert result["filled"] == result["open_positions"] == 1
    assert result["closed"] == result["return_samples"] == 0
    assert result["median_return_pct"] is None
    assert result["median_net_return_pct"] is None
    assert result["positive_rate"] is None
    assert result["median_unrealized_return_pct"] == pytest.approx(0.01)
    assert stats["equity"]["picked"]["trades"] == 0


def test_unclosed_plans_cannot_supply_paired_realized_returns_or_cached_values() -> None:
    picked = outcome(horizon=1)
    picked["exec_return_pct"] = 0.99
    control = outcome(horizon=1, kind="control", symbol="000001.SZ")
    stats = summarize([picked, control])
    assert stats["selection_edge"]["exec_return_gap"] is None
    assert stats["selection_edge"]["metric_coverage"]["exec_return_gap"]["left_samples"] == 0
    assert picked["exec_return_pct"] == 0.99  # 统计不修改输入台账。


def test_many_open_positions_do_not_make_realized_statistics_trusted() -> None:
    rows = [outcome(horizon=1, symbol=f"{600000 + index}.SH") for index in range(25)]
    result = summarize(rows)["overall"]["execution"]
    assert result["filled"] == 25
    assert result["trusted"] is False


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"adjudication": {"abstained": True}}, EXEC_ABSTAINED),
        ({"kind": "vetoed"}, EXEC_VETOED),
        ({"setup": {"primary": None, "matched": [], "rejected": [{}]}}, EXEC_NO_SETUP),
        ({"source": "advise"}, EXEC_NO_PLAN),
        ({"stop_pct": None}, EXEC_NO_PLAN),
        ({"target_pcts": []}, EXEC_NO_PLAN),
        ({"entry_low_pct": -0.02, "entry_high_pct": -0.03}, EXEC_INVALID_PLAN),
        ({"entry_low_pct": -0.05, "entry_high_pct": -0.02}, EXEC_INVALID_PLAN),
        ({"entry_high_pct": 0.10}, EXEC_INVALID_PLAN),
    ],
)
def test_ineligible_plans_remain_signal_observations_without_trades(fields, reason) -> None:
    row = outcome(**fields)
    execution = row["execution"]
    assert row["close_pct"] is not None
    assert execution["filled"] is False
    assert execution["eligible"] is False
    assert execution["basis"] == reason
    assert execution["fill_session"] is None
    stats = summarize([row])["overall"]["execution"]
    assert stats["replayable"] == 0
    assert stats["fill_rate"] is None


def test_a_stale_reference_never_creates_a_trade_before_the_plan_date() -> None:
    row = outcome(trade_date="2026-08-20", horizon=2)
    assert row["anchor_date"] == "2026-08-14"
    assert row["execution"]["fill_session"] == "2026-08-20"
    assert row["execution"]["exit_session"] == "2026-08-21"


def test_intraday_advice_cannot_be_replayed_at_an_earlier_open_on_the_same_day() -> None:
    row = outcome(source="advise", entry_high_pct=0.01, horizon=2)
    assert row["execution"]["fill_session"] == "2026-08-18"
    assert row["execution"]["exit_session"] == "2026-08-19"


def test_post_close_daily_plan_starts_its_open_proxy_on_the_next_session() -> None:
    rows = [
        bar(17, close=100.0),
        bar(18, 101.0, 104.0, 100.0, 103.0),
        bar(19, 103.0, 105.0, 102.0, 104.0),
    ]
    execution = outcome(rows, horizon=2, source="post_close", reference_date="2026-08-17")[
        "execution"
    ]
    assert execution["filled"] is True
    assert execution["fill_session"] == "2026-08-18"
    assert execution["entry_model"] == "open_proxy"
    assert execution["price_alignment"] == "reference_close"
    assert execution["fill_pct"] == pytest.approx(0.01)
    assert execution["exit_session"] == "2026-08-19"


def test_pullback_open_below_the_entry_band_does_not_fill_at_a_cheaper_price() -> None:
    row = outcome(entry_low_pct=0.01, entry_high_pct=0.02, playbook="回踩")
    assert row["execution"]["basis"] == EXEC_BELOW_BAND
    assert row["execution"]["filled"] is False


def test_breakout_waits_for_the_trigger_and_keeps_t_plus_one_from_that_day() -> None:
    row = outcome(entry_low_pct=0.02, entry_high_pct=0.03, playbook="突破")
    execution = row["execution"]
    assert execution["basis"] == EXEC_FILL_BREAKOUT
    assert execution["fill_at_open"] is False
    assert execution["fill_pct"] == pytest.approx(0.02)
    assert execution["exit_session"] == "2026-08-21"
    assert execution["return_pct"] == pytest.approx(101.0 / 102.0 - 1, abs=1e-6)


def test_a_breakout_that_never_reaches_its_trigger_has_no_fill() -> None:
    row = outcome(entry_low_pct=0.04, entry_high_pct=0.04, playbook="突破")
    assert row["execution"]["basis"] == EXEC_BREAKOUT_NOT_TRIGGERED
    assert row["execution"]["filled"] is False
    assert row["entered"] is False


def test_breakout_gapping_past_the_maximum_entry_price_is_not_chased() -> None:
    rows = [bar(14, close=100.0), bar(17, 104.0, 105.0, 101.0, 103.0), bar(18)]
    row = outcome(rows, horizon=2, entry_low_pct=0.02, entry_high_pct=0.03, playbook="突破")
    assert row["execution"]["basis"] == EXEC_ABOVE_BREAKOUT
    assert row["execution"]["filled"] is False


def test_limit_order_filled_at_the_open_includes_the_entry_days_high() -> None:
    rows = [bar(14, close=100.0), bar(17, 100.0, 108.0, 99.0, 101.0), bar(18)]
    execution = outcome(rows, horizon=2, entry_low_pct=-0.01, entry_high_pct=0.01)["execution"]
    assert execution["fill_at_open"] is True
    assert execution["mfe_pct"] == pytest.approx(0.08)


def test_a_gap_exit_excludes_price_moves_after_the_sale() -> None:
    rows = [bar(14, close=100.0), bar(17), bar(18, 94.0, 120.0, 80.0, 100.0)]
    execution = outcome(rows, horizon=2)["execution"]
    assert execution["exit"] == EXIT_STOP
    assert execution["return_pct"] == pytest.approx(-0.06)
    assert execution["mfe_pct"] == pytest.approx(0.03)
    assert execution["mae_pct"] == pytest.approx(-0.06)


@pytest.mark.parametrize("stop_pct", [-0.04, -0.30])
def test_last_day_locked_at_a_single_down_price_is_valued_but_not_sold(stop_pct) -> None:
    rows = [bar(14, close=100.0), bar(17, close=100.0), bar(18, 90.0, 90.0, 90.0, 90.0)]
    row = outcome(rows, horizon=2, stop_pct=stop_pct)
    execution = row["execution"]
    assert execution["exit"] == EXIT_LIMIT_LOCKED
    assert execution["realized"] is False
    assert execution["unrealized_return_pct"] == pytest.approx(-0.10)
    assert execution["locked_sessions"] == 1
    stats = summarize([row])["overall"]["execution"]
    assert stats["closed"] == 0
    assert stats["time_exit_rate"] is None


def test_a_blocked_stop_stays_pending_even_if_the_next_open_recovers_above_it() -> None:
    rows = [
        bar(14, close=100.0),
        bar(17, close=100.0),
        bar(18, 90.0, 90.0, 90.0, 90.0),
        bar(19, 98.0, 100.0, 97.0, 99.0),
    ]
    execution = outcome(rows, horizon=3)["execution"]
    assert execution["exit"] == EXIT_STOP
    assert execution["realized"] is True
    assert execution["return_pct"] == pytest.approx(-0.02)
    assert execution["locked_sessions"] == 1
    assert execution["exit_session"] == "2026-08-19"


def test_missing_exit_open_does_not_invent_a_fill_at_the_stop() -> None:
    rows = [bar(14, close=100.0), bar(17), bar(18, None, 95.0, 90.0, 94.0)]
    row = outcome(rows, horizon=2)
    execution = row["execution"]
    assert execution["exit"] == EXIT_UNCERTAIN
    assert execution["return_pct"] is None
    assert execution["realized"] is False
    stats = summarize([row])["overall"]["execution"]
    assert stats["unresolved_positions"] == 1
    assert stats["median_return_pct"] is None


def test_legacy_time_exits_after_locking_are_not_silently_assumed_to_be_closed() -> None:
    row = outcome(horizon=2)
    row["execution"].pop("realized")
    row["execution"].update(exit=EXIT_TIME, locked_sessions=1)
    assert summarize([row])["overall"]["execution"]["closed"] == 0
    row["execution"]["exit"] = EXIT_STOP
    assert summarize([row])["overall"]["execution"]["closed"] == 1


def test_advice_records_keep_the_playbook_and_intraday_price_anchor() -> None:
    advice = StockAdvice(
        symbol="600519.SH",
        last_price=102.0,
        session_date="2026-08-17",
        entry_low=101.0,
        entry_high=104.0,
        stop=96.0,
        targets=[110.0],
        playbook="突破",
        setup={"primary": "突破", "matched": [{"playbook": "突破", "ok": True}]},
        intraday={"open": 100.0},
    )
    record = Recommendation.from_advice(advice, date(2026, 8, 17), source="advise")
    assert record is not None
    assert record.playbook == advice.playbook
    assert record.setup == advice.setup
    assert record.reference_open_price == 100.0


@pytest.mark.parametrize("adjustment", [1.0, 0.8])
def test_intraday_reference_uses_the_same_open_for_adjustment_not_the_later_close(
    adjustment,
) -> None:
    raw = [
        bar(17, 100.0, 106.0, 98.0, 105.0),
        bar(18, 103.0, 104.0, 101.0, 103.0),
        bar(19, 108.0, 111.0, 107.0, 110.0),
    ]
    rows = [
        {key: value if key == "date" else value * adjustment for key, value in item.items()}
        for item in raw
    ]
    row = outcome(
        rows,
        horizon=2,
        source="advise",
        reference_date="2026-08-17",
        reference_price=102.0,
        reference_open_price=100.0,
        entry_low_pct=101.0 / 102.0 - 1,
        entry_high_pct=104.0 / 102.0 - 1,
        stop_pct=96.0 / 102.0 - 1,
        target_pcts=[110.0 / 102.0 - 1],
    )
    assert row["next_open_pct"] == pytest.approx(103.0 / 102.0 - 1, abs=1e-6)
    execution = row["execution"]
    assert execution["price_alignment"] == "session_open"
    assert execution["exit"] == EXIT_TARGET
    assert execution["return_pct"] == pytest.approx(110.0 / 103.0 - 1, abs=1e-6)


@pytest.mark.parametrize("missing_index", [1, 2])
def test_missing_execution_data_stays_pending_and_resolves_after_repair(
    tmp_path, missing_index
) -> None:
    rows = [bar(14, close=100.0), bar(17), bar(18)]
    rows[missing_index]["open"] = None

    class Adapter:
        def get_historical_bars(self, symbols, start, end):
            return dict.fromkeys(symbols, rows)

    book = Journal(root=tmp_path / "journal")
    book.record(
        Recommendation(
            symbol="600519.SH",
            source="morning",
            trade_date="2026-08-17",
            reference_date="2026-08-14",
            reference_price=100.0,
            stop_pct=-0.04,
            target_pcts=[0.10],
        )
    )
    result = verify_pending(book, Adapter(), today=date(2026, 8, 31), horizon=2)
    assert result["resolved"] == 0
    assert "缺少开盘价" in result["deferred"][0]["reason"]
    assert len(book.pending(date(2026, 8, 31))) == 1
    rows[missing_index]["open"] = 100.0
    repaired = verify_pending(book, Adapter(), today=date(2026, 8, 31), horizon=2)
    assert repaired["resolved"] == 1
    assert len(book.outcomes()) == 1
    assert repaired["outcomes"][0]["execution"]["realized"] is True


def test_execution_comparisons_match_the_simulation_version() -> None:
    picked = outcome(horizon=2)
    control = outcome(horizon=2, kind="control", symbol="000001.SZ")
    control["execution"]["model_version"] = "daily-ohlc-v1"
    stats = summarize([picked, control])
    assert stats["selection_edge"]["next_close_gap"] == 0.0
    assert stats["selection_edge"]["exec_return_gap"] is None
    assert {row["bucket"] for row in stats["by_execution_model"]} == {
        "daily-ohlc-v1",
        "daily-ohlc-v2",
    }
    control["execution"]["model_version"] = picked["execution"]["model_version"]
    assert summarize([picked, control])["selection_edge"]["exec_return_gap"] == 0.0


def test_review_separates_closed_returns_floating_values_and_simulation_assumptions() -> None:
    rows = [outcome(horizon=1), outcome(horizon=2, symbol="000001.SZ")]
    content = render_review(
        date(2026, 8, 31), {"checked": 2, "resolved": 2, "notes": []}, summarize(rows)
    )
    assert "已平仓 1 条" in content
    assert "未平仓估值 1 条" in content
    assert "均不计入已实现收益" in content
    assert "日线价格模拟" in content
    assert "固定成本情景" in content
    assert "未计滑点、最低佣金" in content
    assert "曲线及回撤不是账户净值指标" in content
