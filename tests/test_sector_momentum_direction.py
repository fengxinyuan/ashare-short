"""板块动量里两条方向性规则：放量看方向，回调看趋势。

2026-09-14 晨报实测：燃气当日中位 -2.36%、上涨家数 7%，拿到「温和放量 +3.0」；
东数西算 20 日中位 -5.03%、站上 20 日线 27%，拿到满分「回调到位 +10」。两个都是
把下跌当成了进场信号。
"""

from __future__ import annotations

from ashare_decision_support.news.sentiment import LexiconSentiment
from ashare_decision_support.sectors.base import SectorMetrics
from ashare_decision_support.sectors.predict import SectorPredictor


def _momentum(metric: SectorMetrics) -> tuple[float, list[str], list[str]]:
    reasons: list[str] = []
    warnings: list[str] = []
    points = SectorPredictor(LexiconSentiment())._momentum_component(metric, reasons, warnings)
    return points, reasons, warnings


def _metric(**kwargs) -> SectorMetrics:
    base = {"name": "测试", "constituents": 30, "covered": 30}
    base.update(kwargs)
    return SectorMetrics(**base)


def test_volume_on_a_broad_down_day_is_distribution_not_interest() -> None:
    up = _metric(ret_1d=0.02, breadth=0.75, volume_ratio=1.25)
    down = _metric(ret_1d=-0.024, breadth=0.07, volume_ratio=1.25)

    up_points, up_reasons, _ = _momentum(up)
    down_points, down_reasons, _ = _momentum(down)

    assert any("温和放量（+3.0）" in item for item in up_reasons)
    assert any("放量下跌（-3.0）" in item for item in down_reasons)
    # 同一个量比，方向相反的两天差 6 分（量能项）+ 当日与宽度项本身的差。
    assert up_points - down_points >= 6.0


def test_heavy_volume_on_a_down_day_is_negative_not_a_mere_warning() -> None:
    _, reasons, warnings = _momentum(_metric(ret_1d=-0.03, breadth=0.0, volume_ratio=3.4))
    assert any("放量下跌（-4.0）" in item for item in reasons)
    assert not warnings


def test_shrinking_volume_on_a_pullback_day_is_not_penalised() -> None:
    _, reasons, _ = _momentum(_metric(ret_1d=-0.01, breadth=0.2, volume_ratio=0.6))
    assert any("缩量回调（+0.0）" in item for item in reasons)
    _, reasons, _ = _momentum(_metric(ret_1d=0.01, breadth=0.6, volume_ratio=0.6))
    assert any("缩量（-3.0）" in item for item in reasons)


def test_split_session_is_not_a_broad_down_day() -> None:
    """中位微跌但一半股票在涨，是分歧不是普跌，放量仍按温和放量算。"""
    _, reasons, _ = _momentum(_metric(ret_1d=-0.001, breadth=0.5, volume_ratio=1.3))
    assert any("温和放量（+3.0）" in item for item in reasons)


def test_pullback_bonus_needs_the_trend_to_still_be_there() -> None:
    intact = _metric(ret_20d=0.05, above_ma20=0.7, pullback=-0.065, pullback_atr=2.1)
    broken = _metric(ret_20d=-0.05, above_ma20=0.27, pullback=-0.107, pullback_atr=2.6)

    _, intact_reasons, _ = _momentum(intact)
    _, broken_reasons, _ = _momentum(broken)

    assert any("板块回调到位" in item and "+10.0" in item for item in intact_reasons)
    gated = [item for item in broken_reasons if "中期趋势未在" in item]
    assert gated, broken_reasons
    assert "折减" in gated[0]
    assert not any("板块回调到位" in item for item in broken_reasons)

    # 趋势只是偏弱（20 日略负、多数仍在 20 日线上）：措辞不同，折减幅度小。
    soft = _metric(ret_20d=-0.01, above_ma20=0.55, pullback=-0.05, pullback_atr=1.9)
    _, soft_reasons, _ = _momentum(soft)
    assert any("中期趋势偏弱" in item for item in soft_reasons)


def test_pullback_gate_is_a_ramp_not_a_cliff() -> None:
    def bonus(ret_20d: float, above: float) -> float:
        _, reasons, _ = _momentum(
            _metric(ret_20d=ret_20d, above_ma20=above, pullback=-0.08, pullback_atr=2.0)
        )
        line = next(item for item in reasons if "回调" in item)
        return float(line.rsplit("（", 1)[1].rstrip("）").split("，")[-1])

    full = bonus(0.01, 0.6)
    half = bonus(-0.03, 0.35)
    none = bonus(-0.08, 0.1)
    assert full == 10.0
    assert 0.0 < half < full
    assert none == 0.0


def test_pullback_gate_without_trend_readings_does_not_penalise() -> None:
    _, reasons, _ = _momentum(_metric(pullback=-0.08, pullback_atr=2.0))
    assert any("板块回调到位" in item and "+10.0" in item for item in reasons)
