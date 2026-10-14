"""市场流动性自适应因子（Market Liquidity Scaling Factor）。

依据全市场总成交额水平，动态调节超短线筛选的成交门槛与防守姿态：
地量冰点期（<6500 亿）适度降低个股成交额硬门槛，同时收紧持仓暴露；
放量高潮期（>1.3 万亿）提升成交额要求，放宽题材溢价与进攻弹性。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# 基准健康中轴成交额（亿元）
BASELINE_TURNOVER_YI = 10000.0

# 市场流动性分档阈值（亿元）
TURNOVER_FREEZE = 6500.0
TURNOVER_CONTRACTION = 9000.0
TURNOVER_EXPANSION = 13000.0

# 缩放因子上下限保护
MIN_FACTOR = 0.70
MAX_FACTOR = 1.40


@dataclass(frozen=True)
class LiquidityRegimeReport:
    """全市场流动性自适应分析报告。"""

    turnover_yi: float
    scaling_factor: float
    regime: str  # 冰点地量 / 缩量分化 / 常态充沛 / 放量高潮
    defense_bias: float  # 防守偏好系数 (0.8 ~ 1.5, 越高越需保守)
    position_cap_mult: float  # 仓位上限乘数 (0.6 ~ 1.0)
    notes: list[str] = field(default_factory=list)

    def scale_threshold(self, base_val: float) -> float:
        """根据当前流动性环境缩放指标门槛。"""
        return round(base_val * self.scaling_factor, 2)


def calculate_liquidity_factor(
    turnover_yi: float | None,
    index_ret: float | None = None,
) -> LiquidityRegimeReport:
    """计算市场流动性自适应因子与环境分档。

    结合全市场成交额与宽基指数涨跌幅（量价配合度）：
    - 放量下跌（>1.3万亿且跌幅明显）：判定为【放量分歧杀跌】，防守偏好上升防接盘；
    - 放量上涨（>1.3万亿且涨幅明显）：判定为【放量上攻突破】，做多进攻弹性增强；
    - 地量缩量：区分【地量企稳】与【缩量阴跌】。
    """
    if turnover_yi is None or turnover_yi <= 0:
        return LiquidityRegimeReport(
            turnover_yi=0.0,
            scaling_factor=1.0,
            regime="数据缺失",
            defense_bias=1.0,
            position_cap_mult=1.0,
            notes=["缺少全市场成交额数据，使用标准基准参数"],
        )

    # 因子 = sqrt(实际成交额 / 万亿基准)
    raw_factor = math.sqrt(turnover_yi / BASELINE_TURNOVER_YI)
    clamped_factor = round(max(MIN_FACTOR, min(MAX_FACTOR, raw_factor)), 4)

    ret_pct: float | None = None
    if index_ret is not None:
        ret_pct = index_ret * 100.0 if abs(index_ret) <= 0.2 else index_ret

    notes: list[str] = []
    if turnover_yi < TURNOVER_FREEZE:
        if ret_pct is not None and ret_pct < -0.3:
            regime = "缩量阴跌"
            defense_bias = 1.45
            position_cap = 0.55
            notes.append(
                f"全市场成交额 {turnover_yi:.0f} 亿地量阴跌（{ret_pct:+.2f}%），市场承接力弱，防守系数提高至 1.45"
            )
        else:
            regime = "冰点地量"
            defense_bias = 1.4
            position_cap = 0.6
            notes.append(
                f"全市场成交额 {turnover_yi:.0f} 亿处于地量冰点期，个股门槛按 {clamped_factor:.2f}x 适度下调，防守系数提高至 1.4"
            )
    elif turnover_yi < TURNOVER_CONTRACTION:
        regime = "缩量分化"
        defense_bias = 1.15
        position_cap = 0.75
        notes.append(
            f"全市场成交额 {turnover_yi:.0f} 亿偏缩量，个股流动性门槛乘数 {clamped_factor:.2f}x"
        )
    elif turnover_yi <= TURNOVER_EXPANSION:
        regime = "常态充沛"
        defense_bias = 1.0
        position_cap = 1.0
        notes.append(f"全市场成交额 {turnover_yi:.0f} 亿处于健康万亿中轴区间")
    elif ret_pct is not None and ret_pct <= -0.5:
        regime = "放量杀跌"
        defense_bias = 1.30  # 放量大跌防守加码，绝不盲目当多头高潮
        position_cap = 0.70
        notes.append(
            f"全市场成交额 {turnover_yi:.0f} 亿巨量，但宽基指数下挫（{ret_pct:+.2f}%），呈放量分歧派发特征，防守偏好提高至 1.30"
        )
    elif ret_pct is not None and ret_pct >= 0.5:
        regime = "放量进攻"
        defense_bias = 0.85
        position_cap = 1.0
        notes.append(
            f"全市场成交额 {turnover_yi:.0f} 亿放量上攻（{ret_pct:+.2f}%），做多情绪高涨，题材溢价充沛"
        )
    else:
        regime = "放量高潮"
        defense_bias = 0.95
        position_cap = 0.90
        notes.append(
            f"全市场成交额 {turnover_yi:.0f} 亿放量高位分歧，个股门槛按 {clamped_factor:.2f}x 适当提高防接盘"
        )

    return LiquidityRegimeReport(
        turnover_yi=round(turnover_yi, 1),
        scaling_factor=clamped_factor,
        regime=regime,
        defense_bias=defense_bias,
        position_cap_mult=position_cap,
        notes=notes,
    )
