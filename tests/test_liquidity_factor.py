"""市场流动性自适应因子单元测试。"""

from __future__ import annotations

from ashare_decision_support.scoring.liquidity_factor import (
    calculate_liquidity_factor,
)


def test_missing_or_zero_turnover() -> None:
    res = calculate_liquidity_factor(None)
    assert res.regime == "数据缺失"
    assert res.scaling_factor == 1.0
    assert res.scale_threshold(0.8) == 0.8

    res_zero = calculate_liquidity_factor(0.0)
    assert res_zero.regime == "数据缺失"


def test_freeze_regime_scales_down_and_increases_defense() -> None:
    # 5000 亿地量
    res = calculate_liquidity_factor(5000.0)
    assert res.regime == "冰点地量"
    assert res.scaling_factor < 0.80
    assert res.defense_bias == 1.4
    assert res.position_cap_mult == 0.6
    # 0.8 亿门槛缩放到约 0.57 亿
    scaled = res.scale_threshold(0.8)
    assert scaled < 0.8


def test_contraction_regime() -> None:
    # 8000 亿缩量
    res = calculate_liquidity_factor(8000.0)
    assert res.regime == "缩量分化"
    assert 0.85 < res.scaling_factor < 0.95
    assert res.position_cap_mult == 0.75


def test_healthy_regime_around_one_trillion() -> None:
    # 10000 亿常态
    res = calculate_liquidity_factor(10000.0)
    assert res.regime == "常态充沛"
    assert res.scaling_factor == 1.0
    assert res.defense_bias == 1.0
    assert res.scale_threshold(1.0) == 1.0


def test_expansion_regime_clamps_to_max() -> None:
    # 2.5 万亿巨量放量
    res = calculate_liquidity_factor(25000.0)
    assert res.regime == "放量高潮"
    assert res.scaling_factor == 1.40  # 触发上限 clamp


def test_volume_price_coordination() -> None:
    # 放量杀跌：1.4 万亿但指数 -1.2%
    res_dump = calculate_liquidity_factor(14000.0, index_ret=-1.2)
    assert res_dump.regime == "放量杀跌"
    assert res_dump.defense_bias == 1.30
    assert res_dump.position_cap_mult == 0.70

    # 放量进攻：1.4 万亿且指数 +1.5%
    res_surge = calculate_liquidity_factor(14000.0, index_ret=1.5)
    assert res_surge.regime == "放量进攻"
    assert res_surge.defense_bias == 0.85
    assert res_surge.position_cap_mult == 1.0
