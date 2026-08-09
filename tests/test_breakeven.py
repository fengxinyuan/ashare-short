"""回本价 × 阻力带。

核心用例是 2026-08-04 牧原：成本 39.12 正好落在 MA5 39.00 / MA20 39.01 /
MA10 39.15 这条粘合带里——解套价和压力位是同一个价。
"""

from __future__ import annotations

from ashare_decision_support.signals.breakeven import measure_breakeven

MUYUAN_LEVELS = {
    "MA5": 39.00,
    "MA20": 39.01,
    "MA10": 39.15,
    "箱体下沿": 38.42,
    "分时均价": 38.11,
    "MA60": 37.61,
}


def test_the_muyuan_cost_sits_inside_the_moving_average_cluster() -> None:
    """成本 39.12 与三条均线重合，这是全场最难啃的位置。"""
    read = measure_breakeven(cost=39.12, last_price=38.02, levels=MUYUAN_LEVELS)

    assert read.underwater is True
    assert round(read.unrealised_pct, 4) == -0.0281
    names = [name for name, _ in read.cluster_at_cost]
    assert names == ["MA5", "MA20", "MA10"]
    body = " ".join(read.describe())
    assert "解套价和压力位是同一个价" in body


def test_only_levels_between_price_and_cost_are_counted() -> None:
    """回本价上方的位再多也和这次解套无关；下方的已经跌穿了。"""
    read = measure_breakeven(cost=39.12, last_price=38.02, levels=MUYUAN_LEVELS)

    names = [name for name, _ in read.levels_between]
    assert names == ["分时均价", "箱体下沿", "MA5", "MA20"]
    assert "MA60" not in names  # 37.61 在现价下方，已经跌穿的不算
    assert "MA10" not in names  # 39.15 在成本上方，和这次解套无关


def test_stacked_resistance_gets_called_out() -> None:
    read = measure_breakeven(cost=39.12, last_price=38.02, levels=MUYUAN_LEVELS)

    assert len(read.levels_between) >= 3
    assert "压力层数已达密集区间" in " ".join(read.describe())


def test_gap_to_breakeven_is_measured_from_the_current_price() -> None:
    """回本涨幅的分母是现价，不是成本——这两个数不一样。"""
    read = measure_breakeven(cost=100.0, last_price=80.0, levels={})

    assert round(read.gap_to_breakeven_pct, 4) == 0.25
    assert round(read.unrealised_pct, 4) == -0.20


def test_a_profitable_holding_reports_without_resistance_analysis() -> None:
    read = measure_breakeven(cost=38.0, last_price=39.5, levels=MUYUAN_LEVELS)

    assert read.underwater is False
    assert read.levels_between == []
    assert "当前浮盈" in " ".join(read.describe())


def test_no_resistance_between_is_stated_rather_than_left_blank() -> None:
    read = measure_breakeven(cost=39.12, last_price=39.10, levels={"MA60": 37.61})

    assert read.levels_between == []
    assert "没有识别到阻力位" in " ".join(read.describe())


def test_missing_levels_do_not_sink_the_whole_read() -> None:
    read = measure_breakeven(cost=39.12, last_price=38.02, levels={"MA5": None, "MA20": 39.01})

    assert [name for name, _ in read.cluster_at_cost] == ["MA20"]


def test_missing_cost_abstains() -> None:
    read = measure_breakeven(cost=None, last_price=38.02, levels=MUYUAN_LEVELS)

    assert read.cost is None
    assert "不出回本结构" in " ".join(read.describe())


def test_non_positive_inputs_abstain() -> None:
    assert measure_breakeven(cost=0.0, last_price=38.0, levels={}).cost is None
    assert measure_breakeven(cost=39.0, last_price=-1.0, levels={}).last_price is None
