"""执行口径重放：先成交、成交当日不能卖、跳空按开盘价——以及参考日锚定。

改造前的核对把盘前晨报的百分比价位套在 `trade_date`（计划日）的收盘上：那根 K 线
在建议发出时还不存在，计划日本身被整个跳过，而「先到目标」也不管有没有买到、
能不能卖。这里的每一条都是一个此前会被算错的具体交易。
"""

from __future__ import annotations

from datetime import date

import pytest

from ashare_decision_support.jobs.review import render_review
from ashare_decision_support.journal import Journal, Recommendation, verify_pending
from ashare_decision_support.journal.stats import ROUND_TRIP_COST_PCT, summarize
from ashare_decision_support.journal.verify import (
    EXEC_FILL_LIMIT,
    EXEC_FILL_OPEN,
    EXEC_LOCKED_UP,
    EXEC_NOT_FILLED,
    EXEC_SKIP_STOP,
    EXEC_SKIP_TARGET,
    EXIT_LIMIT_LOCKED,
    EXIT_STOP,
    EXIT_T1_LOCKED,
    EXIT_TARGET,
    EXIT_TIME,
)

SYMBOL = "600519.SH"
REFERENCE = date(2026, 8, 14)  # 周五：晨报用到的最后一根日线
PLAN_DAY = date(2026, 8, 17)  # 周一：晨报的 trade_date，计划执行日
REVIEW = date(2026, 8, 31)


def near(value: float, tolerance: float = 1e-6) -> object:
    """核对结果按 6 位小数落盘，统计按 4 位；比较时给对应的绝对容差。"""
    return pytest.approx(value, abs=tolerance)


def bar(day: int, open_: float, high: float, low: float, close: float) -> dict:
    return {"date": f"2026-08-{day:02d}", "open": open_, "high": high, "low": low, "close": close}


def flat(day: int, price: float = 100.0) -> dict:
    return bar(day, price, price + 0.5, price - 0.5, price)


class Adapter:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def get_historical_bars(self, symbols, start, end):
        return dict.fromkeys(symbols, self.rows)


def morning_book(tmp_path, **fields) -> Journal:
    """盘前晨报那种记录：参考价是上一交易日收盘，没有买入区间，只有止损和目标。"""
    book = Journal(root=tmp_path / "journal")
    values = {
        "symbol": SYMBOL,
        "trade_date": str(PLAN_DAY),
        "source": "morning",
        "reference_price": 100.0,
        "reference_date": str(REFERENCE),
        "stop_pct": -0.04,
        "target_pcts": [0.06],
        "rrr": 1.5,
        "dip": {"atr_pct": 0.02},
    }
    book.record(Recommendation(**(values | fields)))
    return book


def resolve(tmp_path, rows: list[dict], horizon: int = 5, **fields) -> dict:
    result = verify_pending(
        morning_book(tmp_path, **fields), Adapter(rows), today=REVIEW, horizon=horizon
    )
    assert result["resolved"] == 1, result
    return result["outcomes"][0]


# 周五收 100。周一开 101、盘中冲 107（越过目标 106）、收 103；周三再次触及 106。
FILL_DAY_TOUCHES_TARGET = [
    bar(13, 99.0, 101.0, 98.0, 99.5),
    bar(14, 99.5, 101.0, 99.0, 100.0),
    bar(17, 101.0, 107.0, 100.5, 103.0),
    bar(18, 103.0, 104.0, 102.0, 103.5),
    bar(19, 103.0, 106.5, 102.5, 105.0),
    bar(20, 105.0, 105.5, 104.0, 104.5),
    bar(21, 104.5, 105.0, 103.5, 104.0),
    bar(24, 104.0, 104.5, 103.0, 103.5),
]


# 参考日锚定


def test_morning_plan_is_anchored_on_the_reference_close_not_the_plan_day(tmp_path) -> None:
    """百分比价位套在参考价那根 K 线上，核对窗口从计划日当天开始。"""
    outcome = resolve(tmp_path, FILL_DAY_TOUCHES_TARGET)

    assert outcome["anchor_date"] == "2026-08-14"
    assert outcome["sessions"] == 5
    # 隔夜跳空是周五收盘 → 周一开盘，那正是照计划下单要面对的第一个价。
    assert outcome["next_open_pct"] == near(0.01)
    assert outcome["next_close_pct"] == near(0.03)
    assert outcome["first_hit"] == "target"


def test_legacy_rows_recover_the_anchor_from_the_reference_price(tmp_path) -> None:
    """老记录没有 reference_date，但参考价 100 只和周五收盘对得上——按它锚定。"""
    outcome = resolve(tmp_path, FILL_DAY_TOUCHES_TARGET, reference_date=None)

    assert outcome["anchor_date"] == "2026-08-14"
    assert outcome["first_hit"] == "target"


def test_legacy_rows_whose_reference_is_the_plan_day_close_keep_that_anchor(tmp_path) -> None:
    """盘后问价的参考价就是当日收盘：两根都对得上时维持旧口径，锚在 trade_date。"""
    outcome = resolve(tmp_path, FILL_DAY_TOUCHES_TARGET, reference_date=None, reference_price=103.0)

    assert outcome["anchor_date"] == "2026-08-17"
    assert outcome["next_open_pct"] == near(0.0)
    # 目标 106 按周一收盘 103 重算成 109.18，窗口内碰不到——旧口径就是这样算的。
    assert outcome["first_hit"] is None


def test_legacy_rows_without_a_matching_close_fall_back_to_the_trade_date(tmp_path) -> None:
    """复权因子变过之后两根都对不上：维持旧口径，不猜。"""
    outcome = resolve(tmp_path, FILL_DAY_TOUCHES_TARGET, reference_date=None, reference_price=97.0)

    assert outcome["anchor_date"] == "2026-08-17"


def test_a_reference_date_after_the_trade_date_is_ignored_and_the_price_match_decides(
    tmp_path,
) -> None:
    """晚于 trade_date 的 reference_date 是坏数据：当作没有，退回参考价匹配。"""
    recovered = resolve(tmp_path, FILL_DAY_TOUCHES_TARGET, reference_date="2026-08-18")
    unmatched = resolve(
        tmp_path / "b", FILL_DAY_TOUCHES_TARGET, reference_date="2026-08-18", reference_price=97.0
    )

    assert recovered["anchor_date"] == "2026-08-14"
    assert unmatched["anchor_date"] == "2026-08-17"


# 先成交，再 T+1


def test_target_touched_on_the_fill_day_cannot_be_sold_until_the_next_session(tmp_path) -> None:
    """信号口径说「先到目标」；执行口径按开盘价 101 买入，当天卖不出，
    周二没碰到，周三才在 106 兑现。两套口径并存，各说各的。"""
    outcome = resolve(tmp_path, FILL_DAY_TOUCHES_TARGET)
    execution = outcome["execution"]

    assert execution["filled"] is True
    assert execution["basis"] == EXEC_FILL_OPEN
    assert execution["fill_session"] == "2026-08-17"
    assert execution["fill_pct"] == near(0.01)
    assert execution["exit"] == EXIT_TARGET
    assert execution["exit_session"] == "2026-08-19"
    assert execution["gap_exit"] is False
    assert execution["hold_sessions"] == 2
    assert execution["return_pct"] == near(106.0 / 101.0 - 1.0)
    # 开盘成交，成交日整根 K 线都在持仓期内：MFE 含周一那根 107，MAE 含周一低点。
    assert execution["mfe_pct"] == near(107.0 / 101.0 - 1.0)
    assert execution["mae_pct"] == near(100.5 / 101.0 - 1.0)


def test_a_gap_through_the_stop_fills_at_the_open_not_at_the_stop(tmp_path) -> None:
    rows = [
        bar(14, 99.5, 101.0, 99.0, 100.0),
        bar(17, 100.5, 101.0, 99.0, 99.0),
        bar(18, 94.0, 95.0, 93.0, 94.5),  # 低开跌穿 96 的止损
        flat(19, 95.0),
        flat(20, 95.0),
        flat(21, 95.0),
    ]
    execution = resolve(tmp_path, rows)["execution"]

    assert execution["exit"] == EXIT_STOP
    assert execution["gap_exit"] is True
    assert execution["exit_session"] == "2026-08-18"
    # 拿到的是开盘价 94，不是止损价 96：亏 6.5%，不是计划里的 4%。
    assert execution["return_pct"] == near(94.0 / 100.5 - 1.0)
    assert execution["return_pct"] < -0.04


def test_stop_and_target_in_the_same_bar_count_as_a_stop(tmp_path) -> None:
    rows = [
        bar(14, 99.5, 101.0, 99.0, 100.0),
        bar(17, 100.5, 101.0, 100.0, 100.5),
        bar(18, 100.0, 107.0, 95.0, 100.0),  # 同一根既碰止损又碰目标
        flat(19),
        flat(20),
        flat(21),
    ]
    execution = resolve(tmp_path, rows)["execution"]

    assert execution["exit"] == EXIT_STOP
    assert execution["gap_exit"] is False
    assert execution["return_pct"] == near(96.0 / 100.5 - 1.0)


def test_no_touch_inside_the_window_exits_at_the_last_close(tmp_path) -> None:
    rows = [bar(14, 99.5, 101.0, 99.0, 100.0), *(flat(day, 101.0) for day in (17, 18, 19, 20, 21))]
    execution = resolve(tmp_path, rows)["execution"]

    assert execution["exit"] == EXIT_TIME
    assert execution["exit_session"] == "2026-08-21"
    assert execution["hold_sessions"] == 4
    assert execution["return_pct"] == near(0.0)


def test_a_one_session_window_is_still_locked_by_t_plus_one(tmp_path) -> None:
    rows = [bar(14, 99.5, 101.0, 99.0, 100.0), bar(17, 101.0, 107.0, 100.5, 103.0)]
    execution = resolve(tmp_path, rows, horizon=1)["execution"]

    assert execution["exit"] == EXIT_T1_LOCKED
    assert execution["hold_sessions"] == 0
    assert execution["return_pct"] == near(103.0 / 101.0 - 1.0)


# 不进场的三种情况


def test_opening_below_the_stop_means_the_plan_is_dead_not_a_stop_loss(tmp_path) -> None:
    rows = [
        bar(14, 99.5, 101.0, 99.0, 100.0),
        bar(17, 95.0, 97.0, 94.0, 96.5),  # 开盘已在 96 下方
        flat(18, 97.0),
        flat(19, 97.0),
        flat(20, 97.0),
        flat(21, 97.0),
    ]
    outcome = resolve(tmp_path, rows)

    # 信号口径照样记「先到止损」；执行口径说的是这笔根本没做。
    assert outcome["first_hit"] == "stop"
    assert outcome["execution"]["filled"] is False
    assert outcome["execution"]["basis"] == EXEC_SKIP_STOP
    assert outcome["execution"]["open_pct"] == near(-0.05)
    assert "return_pct" not in outcome["execution"]


def test_opening_above_the_target_is_not_chased(tmp_path) -> None:
    rows = [
        bar(14, 99.5, 101.0, 99.0, 100.0),
        bar(17, 106.5, 108.0, 106.0, 107.0),
        flat(18, 107.0),
        flat(19, 107.0),
        flat(20, 107.0),
        flat(21, 107.0),
    ]
    execution = resolve(tmp_path, rows)["execution"]

    assert execution["filled"] is False
    assert execution["basis"] == EXEC_SKIP_TARGET


def test_a_limit_order_that_is_never_reached_is_not_filled(tmp_path) -> None:
    rows = [
        bar(14, 99.5, 101.0, 99.0, 100.0),
        bar(17, 101.0, 103.0, 99.0, 102.0),  # 最低 99，限价 98 没碰到
        flat(18, 104.0),
        flat(19, 104.0),
        flat(20, 104.0),
        flat(21, 104.0),
    ]
    outcome = resolve(tmp_path, rows, entry_high_pct=-0.02)

    assert outcome["entered"] is False
    assert outcome["execution"]["filled"] is False
    assert outcome["execution"]["basis"] == EXEC_NOT_FILLED


def test_a_limit_fill_excludes_the_fill_days_high_from_its_mfe(tmp_path) -> None:
    """成交日的最高价可能出现在成交之前，日线分不出先后——不算自己的。"""
    rows = [
        bar(14, 99.5, 101.0, 99.0, 100.0),
        bar(17, 101.0, 102.0, 97.5, 98.5),  # 盘中回到 98 成交
        bar(18, 99.0, 100.5, 98.0, 100.0),
        flat(19, 100.0),
        flat(20, 100.0),
        flat(21, 100.0),
    ]
    execution = resolve(tmp_path, rows, entry_high_pct=-0.02)["execution"]

    assert execution["basis"] == EXEC_FILL_LIMIT
    assert execution["fill_pct"] == near(-0.02)
    assert execution["mfe_pct"] == near(100.5 / 98.0 - 1.0)
    assert execution["mae_pct"] == near(97.5 / 98.0 - 1.0)


def test_an_open_inside_the_limit_band_fills_at_the_open(tmp_path) -> None:
    rows = [
        bar(14, 99.5, 101.0, 99.0, 100.0),
        bar(17, 97.0, 99.0, 96.5, 98.0),  # 低开到限价下方，按开盘价 97 成交
        flat(18, 99.0),
        flat(19, 99.0),
        flat(20, 99.0),
        flat(21, 99.0),
    ]
    execution = resolve(tmp_path, rows, entry_high_pct=-0.02)["execution"]

    assert execution["basis"] == EXEC_FILL_LIMIT
    assert execution["fill_pct"] == near(-0.03)


# 统计与渲染


def _outcome(
    group: str,
    day: str,
    change: float,
    *,
    filled: bool = True,
    exit_kind: str = EXIT_TARGET,
    **extra,
) -> dict:
    execution = {"fill_session": day, "t_plus_one": True}
    if filled:
        execution |= {
            "filled": True,
            "basis": EXEC_FILL_OPEN,
            "exit": exit_kind,
            "return_pct": change,
            "mae_pct": -0.01,
            "hold_sessions": 2,
        }
    else:
        execution |= {"filled": False, "basis": EXEC_SKIP_STOP}
    return {
        "kind": "outcome",
        "source": "morning",
        "rec_kind": group,
        "trade_date": day,
        "sessions": 5,
        "next_close_pct": change,
        "close_pct": change,
        "mfe_pct": change + 0.02,
        "mae_pct": -0.01,
        "entered": False,
        "rrr": 2.0,
        "stop_pct": -0.04,
        "dip": {"atr_pct": 0.02},
        "execution": execution,
        **extra,
    }


def test_execution_statistics_only_count_filled_plans() -> None:
    rows = [
        _outcome("recommendation", "2026-08-17", 0.05),
        _outcome("recommendation", "2026-08-17", -0.04, exit_kind=EXIT_STOP),
        _outcome("recommendation", "2026-08-17", 0.02, exit_kind=EXIT_TIME),
        _outcome("recommendation", "2026-08-17", 0.0, filled=False),
    ]
    execution = summarize(rows)["overall"]["execution"]

    assert execution["samples"] == 4
    assert execution["filled"] == 3
    assert execution["fill_rate"] == near(0.75, 1e-4)
    assert execution["target_rate"] == near(1 / 3, 1e-4)
    assert execution["stop_rate"] == near(1 / 3, 1e-4)
    assert execution["time_exit_rate"] == near(1 / 3, 1e-4)
    assert execution["median_return_pct"] == near(0.02, 1e-4)
    assert execution["positive_rate"] == near(2 / 3, 1e-4)
    assert execution["skipped"] == {EXEC_SKIP_STOP: 1}
    assert execution["trusted"] is False


def test_selection_edge_reports_the_realised_gap_alongside_the_signal_gap() -> None:
    rows = []
    for day in range(17, 22):
        rows.append(_outcome("recommendation", f"2026-08-{day}", 0.03))
        rows.append(_outcome("control", f"2026-08-{day}", 0.01))
        # 对照组里没成交的那条不能拉低对照组的已实现收益。
        rows.append(_outcome("control", f"2026-08-{day}", -0.09, filled=False))

    edge = summarize(rows)["selection_edge"]

    # 信号口径：对照组每日中位是 (0.01, -0.09) 的中位 −0.04，差值 +0.07。
    assert edge["next_close_gap"] == near(0.07, 1e-4)
    # 执行口径：没成交的那条不参与，差值只剩 0.03 − 0.01。
    assert edge["exec_return_gap"] == near(0.02, 1e-4)
    assert edge["metric_coverage"]["exec_return_gap"]["right_samples"] == 5


def test_reward_risk_and_stop_distance_buckets_exist() -> None:
    stats = summarize(
        [
            _outcome("recommendation", "2026-08-17", 0.03),
            _outcome("recommendation", "2026-08-17", 0.03, rrr=0.7, stop_pct=-0.08),
            _outcome("recommendation", "2026-08-17", 0.03, rrr=None, dip={}),
        ]
    )

    assert {row["bucket"] for row in stats["by_rrr"]} == {
        "盈亏比 1.5-2.5",
        "盈亏比 0.0-1.0",
        "未记录",
    }
    assert {row["bucket"] for row in stats["by_stop_distance"]} == {
        "止损 2.0-3.0 个日波幅",
        "止损 ≥3.0 个日波幅",
        "未记录",
    }


def test_review_renders_the_execution_view_next_to_the_signal_view() -> None:
    rows = [
        _outcome(group, f"2026-08-{day}", change, playbook="回踩")
        for day in range(17, 22)
        for group, change in (("recommendation", 0.03), ("control", 0.01))
        for _ in range(4)
    ]
    rows.append(_outcome("recommendation", "2026-08-17", 0.0, filled=False, playbook="回踩"))

    content = render_review(REVIEW, {"checked": 0, "resolved": 0, "notes": []}, summarize(rows))

    assert "执行口径" in content
    assert "已实现收益中位" in content
    assert f"未成交：{EXEC_SKIP_STOP} 1 条" in content
    assert "执行口径已实现收益差 **+2.00%**" in content
    assert "## 执行口径（先成交，T+1 起可卖）" in content
    assert "| 按打法：回踩 |" in content
    assert "## 按盈亏比分桶" in content
    assert "## 按止损距离（日波幅倍数）" in content


# ---- 一字板：没有对手盘 --------------------------------------------------------


def one_price(day: int, price: float) -> dict:
    """涨停或跌停一字板：全天只有一个价。"""
    return bar(day, price, price, price, price)


def test_a_limit_up_one_price_bar_cannot_be_bought(tmp_path) -> None:
    rows = [
        bar(14, 99.5, 101.0, 99.0, 100.0),
        one_price(17, 110.0),  # 一字涨停，买单排在封单后面
        flat(18, 112.0),
        flat(19, 112.0),
        flat(20, 112.0),
        flat(21, 112.0),
    ]
    outcome = resolve(tmp_path, rows)

    assert outcome["first_hit"] == "target"  # 信号口径照样记「先到目标」
    assert outcome["execution"]["filled"] is False
    assert outcome["execution"]["basis"] == EXEC_LOCKED_UP
    assert outcome["execution"]["open_pct"] == near(0.10)


def test_a_limit_down_one_price_bar_defers_the_stop_to_the_next_session(tmp_path) -> None:
    """周二一字跌停卖不出；周三低开在止损下方，按周三开盘价止损。"""
    rows = [
        bar(14, 99.5, 101.0, 99.0, 100.0),
        bar(17, 100.5, 101.0, 100.0, 100.5),
        one_price(18, 90.45),  # 跌停一字：低于止损 96，却卖不出
        bar(19, 88.0, 91.0, 87.0, 90.0),
        flat(20, 90.0),
        flat(21, 90.0),
    ]
    execution = resolve(tmp_path, rows)["execution"]

    assert execution["exit"] == EXIT_STOP
    assert execution["exit_session"] == "2026-08-19"
    assert execution["locked_sessions"] == 1
    assert execution["gap_exit"] is True
    # 实际亏损 12.4%，不是计划里的 4%：这就是一字板对止损纪律的真实含义。
    assert execution["return_pct"] == near(88.0 / 100.5 - 1.0)


def test_locked_through_the_window_marks_to_the_last_close(tmp_path) -> None:
    rows = [
        bar(14, 99.5, 101.0, 99.0, 100.0),
        bar(17, 100.5, 101.0, 100.0, 100.5),
        one_price(18, 90.45),
        one_price(19, 81.41),
        one_price(20, 73.27),
        one_price(21, 65.94),
    ]
    execution = resolve(tmp_path, rows)["execution"]

    assert execution["exit"] == EXIT_LIMIT_LOCKED
    assert execution["realized"] is False
    assert execution["exit_session"] is None
    assert execution["locked_sessions"] == 4
    assert execution["hold_sessions"] == 4
    assert execution["return_pct"] == near(65.94 / 100.5 - 1.0)


def test_a_limit_up_one_price_bar_still_lets_you_sell_at_the_target(tmp_path) -> None:
    rows = [
        bar(14, 99.5, 101.0, 99.0, 100.0),
        bar(17, 100.5, 101.0, 100.0, 100.5),
        one_price(18, 110.55),  # 一字涨停：卖单排在最前，目标 106 兑现在开盘价
        flat(19, 110.0),
        flat(20, 110.0),
        flat(21, 110.0),
    ]
    execution = resolve(tmp_path, rows)["execution"]

    assert execution["exit"] == EXIT_TARGET
    assert execution["gap_exit"] is True
    assert execution["locked_sessions"] == 0
    assert execution["return_pct"] == near(110.55 / 100.5 - 1.0)


# ---- 成本、逐日曲线与复查项 -----------------------------------------------------


def test_net_returns_subtract_one_round_trip_of_cost() -> None:
    rows = [
        _outcome("recommendation", "2026-08-17", 0.0105),
        _outcome("recommendation", "2026-08-17", 0.0005),
    ]
    execution = summarize(rows)["overall"]["execution"]

    assert execution["median_return_pct"] == near(0.0055, 1e-4)
    assert execution["median_net_return_pct"] == near(0.0055 - ROUND_TRIP_COST_PCT, 1e-4)
    # 毛收益两笔都为正，扣费后只剩一笔。
    assert execution["positive_rate"] == near(1.0, 1e-4)
    assert execution["net_positive_rate"] == near(0.5, 1e-4)


def test_locked_exits_are_counted() -> None:
    row = _outcome("recommendation", "2026-08-17", -0.12, exit_kind=EXIT_STOP)
    row["execution"]["locked_sessions"] = 1

    assert summarize([row])["overall"]["execution"]["locked_exits"] == 1


def test_equity_curve_compounds_daily_means_net_of_cost() -> None:
    rows = [
        _outcome("recommendation", "2026-08-17", 0.031),
        _outcome("recommendation", "2026-08-17", 0.011),
        _outcome("recommendation", "2026-08-18", -0.019),
        _outcome("recommendation", "2026-08-18", 0.0, filled=False),
        _outcome("control", "2026-08-17", 0.051),
    ]
    equity = summarize(rows)["equity"]
    picked = equity["picked"]

    # 08-17 均值 (0.03 + 0.01) / 2 = +2.0%，08-18 −2.0%；没成交的那条不进曲线。
    assert picked["sessions"] == 2
    assert picked["trades"] == 3
    assert [item["mean_net_return_pct"] for item in picked["daily"]] == [
        near(0.02, 1e-4),
        near(-0.02, 1e-4),
    ]
    assert picked["cumulative_pct"] == near(1.02 * 0.98 - 1.0, 1e-4)
    assert picked["max_drawdown_pct"] == near(-0.02, 1e-4)
    assert picked["positive_sessions"] == 1
    assert equity["control"]["cumulative_pct"] == near(0.05, 1e-4)
    assert equity["cost_pct"] == ROUND_TRIP_COST_PCT


def test_execution_feedback_flags_a_realised_shortfall_the_signal_view_hides() -> None:
    """信号口径入选领先，执行口径入选落后：两条复查项必须各说各的。"""
    rows = []
    for day in range(17, 22):
        for _ in range(20):
            rows.append(_outcome("recommendation", f"2026-08-{day}", 0.02))
            rows.append(_outcome("control", f"2026-08-{day}", 0.01))
    for row in rows:
        if row["rec_kind"] == "recommendation":
            row["execution"]["return_pct"] = -0.01  # 买到之后被跳空吃掉了
    feedback = {item["rule"]: item for item in summarize(rows)["rule_feedback"]}

    assert feedback["selection"]["status"] == "保留观察"
    assert feedback["execution"]["status"] == "建议复查"
    assert feedback["execution"]["gap"] == near(-0.02, 1e-4)
    assert feedback["execution"]["gap_label"] == "已实现收益差"
    # 老字段名保留给事实包的既有消费者。
    assert feedback["selection"]["next_close_gap"] == near(0.01, 1e-4)


def test_score_feedback_flags_an_inverted_ranking() -> None:
    rows = []
    for day in range(17, 22):
        for _ in range(20):
            rows.append(_outcome("recommendation", f"2026-08-{day}", -0.01, score=85))
            rows.append(_outcome("recommendation", f"2026-08-{day}", 0.02, score=65))
    feedback = {item["rule"]: item for item in summarize(rows)["rule_feedback"]}

    assert feedback["score"]["status"] == "建议复查"
    assert feedback["score"]["gap"] == near(-0.03, 1e-4)
    assert feedback["score"]["evidence"]["top_bucket"] == "80-100"
    assert feedback["score"]["evidence"]["bottom_bucket"] == "60-70"


def test_score_feedback_waits_for_two_trusted_buckets() -> None:
    rows = [_outcome("recommendation", f"2026-08-{day}", 0.01, score=75) for day in range(17, 22)]

    feedback = {item["rule"]: item for item in summarize(rows * 4)["rule_feedback"]}

    assert feedback["score"]["status"] == "继续观察"
    assert summarize([])["rule_feedback"][3]["status"] == "待补数据"


def test_review_renders_costs_locks_equity_and_the_new_review_items() -> None:
    rows = []
    for day in range(17, 22):
        for _ in range(20):
            rows.append(
                _outcome("recommendation", f"2026-08-{day}", 0.02, score=85, playbook="回踩")
            )
            rows.append(_outcome("control", f"2026-08-{day}", 0.01, score=65, playbook="回踩"))
    rows[0]["execution"]["locked_sessions"] = 2

    content = render_review(REVIEW, {"checked": 0, "resolved": 0, "notes": []}, summarize(rows))

    assert "扣费后" in content
    assert "单一跌价阻碍离场 1 条" in content
    assert "## 成交日归集研究曲线（每笔等权，固定成本情景 0.10%）" in content
    assert "| 成交日 | 入选笔数 | 入选持有期均值 | 对照笔数 | 对照持有期均值 |" in content
    assert "- **执行口径排序：" in content
    assert "- **分数分档：" in content
    assert "| 按止损距离：止损 2.0-3.0 个日波幅 |" in content
