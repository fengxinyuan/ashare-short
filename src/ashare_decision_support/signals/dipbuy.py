"""低吸特征：把「回调到位了吗」拆成可记录、可统计的几个数。

低吸和打板要的读数是**反的**。打板问「有多强」——涨幅、封板质量、连板高度；
低吸问「趋势还在不在、回调够不够深、缩量了没有」。同一个量比在两种策略下的
含义甚至相反：打板要放量，低吸的回调段要**缩量**（回调放量是出货）。

这个模块只负责**算出这些数**，不负责打分。分开的理由有两条：

1. 打分口径还在观察期，随时会调；特征定义不该跟着抖。
2. **台账字段必须在采集之前定好。** 明天开始每条建议都要把这些数存进台账，
   两个月后才有足够样本回答「回撤 5% 买和回撤 8% 买哪个次日溢价高」——那时
   再想补字段，之前的样本已经永久失去分层能力。

口径统一用**前复权日线**，和 `journal/verify.py` 的核对序列一致，相对百分比
因此可比。绝对价一律不进这里。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# 回调深度的三段划分。低于 SHALLOW 说明还没怎么调，追进去和打板无异；高于 DEEP
# 时「回调」和「趋势走坏」已经难以区分，此时买入是接刀不是低吸。
# 这两个数是**先验假设，不是实测结论**——它们的作用是给台账分层提供一个初始
# 切分，等积累够样本后由数据修正。
SHALLOW_PULLBACK = 0.02
DEEP_PULLBACK = 0.12

# 缩量判定。近 3 日均量掉到 20 日均量的这个比例以下，视作缩量回调。
CONTRACTION_FULL = 0.75

MEDIUM_WINDOW = 20
LONG_WINDOW = 60
RECENT_VOLUME_WINDOW = 3


@dataclass(frozen=True)
class DipFeatures:
    """一只票在某个交易日收盘后的低吸画像。

    每个字段都可能是 None——数据不足时返回 None 而不是 0，因为「没算出来」和
    「算出来是零」在事后统计里必须能区分开。
    """

    # L1 趋势还在吗
    ret_20d: float | None = None
    ret_60d: float | None = None
    above_ma20: bool | None = None
    dist_to_ma20_pct: float | None = None

    # L2 回调到位了吗
    pullback_pct: float | None = None          # 距 20 日最高的回撤，≤0
    pullback_band: str | None = None           # none | shallow | ideal | deep
    down_days: int | None = None               # 从 20 日高点算起，连续下跌天数
    dist_to_support_pct: float | None = None   # 距最近支撑，正=支撑在下方

    # 回调质量：缩量回调是健康的，放量回调是出货
    volume_contraction: float | None = None    # 近 3 日均量 / 20 日均量
    shrinking: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def measure_dip(bars: list[dict], snapshot: Any = None) -> DipFeatures:
    """从前复权日线算低吸特征。

    `snapshot` 是可选的 `DailyTechnicalSnapshot`——只用来取已经算好的支撑位，
    不重复造轮子。给 None 时 `dist_to_support_pct` 为 None，其余照算。

    任何情况下都不抛异常：盘前一只票算不出特征，不该让整条链路停下来。
    """
    closes = _series(bars, "close")
    if len(closes) < 2:
        return DipFeatures()

    last = closes[-1]
    if last <= 0:
        return DipFeatures()

    highs = _series(bars, "high") or closes
    volumes = _series(bars, "volume")

    ret_20d = _change(closes, MEDIUM_WINDOW)
    ret_60d = _change(closes, LONG_WINDOW)

    ma20 = _mean(closes[-MEDIUM_WINDOW:]) if len(closes) >= MEDIUM_WINDOW else None
    dist_to_ma20 = None if not ma20 else round(last / ma20 - 1.0, 6)
    above_ma20 = None if dist_to_ma20 is None else dist_to_ma20 >= 0

    pullback, down_days = _pullback(highs[-MEDIUM_WINDOW:], closes[-MEDIUM_WINDOW:], last)
    contraction = _contraction(volumes)

    return DipFeatures(
        ret_20d=ret_20d,
        ret_60d=ret_60d,
        above_ma20=above_ma20,
        dist_to_ma20_pct=dist_to_ma20,
        pullback_pct=pullback,
        pullback_band=_band(pullback),
        down_days=down_days,
        dist_to_support_pct=_support_distance(snapshot, last),
        volume_contraction=contraction,
        shrinking=None if contraction is None else contraction <= CONTRACTION_FULL,
    )


def _band(pullback: float | None) -> str | None:
    """把回调深度切成四段。`ideal` 是低吸的目标窗口，`deep` 要当成风险而非机会。"""
    if pullback is None:
        return None
    depth = -pullback
    if depth < SHALLOW_PULLBACK:
        return "none"
    if depth < 0.05:
        return "shallow"
    if depth <= DEEP_PULLBACK:
        return "ideal"
    return "deep"


def _pullback(highs: list[float], closes: list[float], last: float) -> tuple[float | None, int | None]:
    """回撤幅度与「从高点以来跌了几天」。

    down_days 数的是**高点之后的自然日数**，不是连续阴线数——低吸关心的是回调
    走了多久（时间够不够消化），中间穿插一根小阳线并不改变这件事。
    """
    if not highs:
        return None, None
    peak = max(highs)
    if peak <= 0:
        return None, None
    pullback = round(min(last / peak - 1.0, 0.0), 6)

    peak_index = max(range(len(highs)), key=lambda i: highs[i])
    down_days = len(highs) - 1 - peak_index
    return pullback, down_days


def _contraction(volumes: list[float]) -> float | None:
    """近 3 日均量相对 20 日均量。<1 缩量，>1 放量。"""
    if len(volumes) < MEDIUM_WINDOW:
        return None
    baseline = _mean(volumes[-MEDIUM_WINDOW:])
    recent = _mean(volumes[-RECENT_VOLUME_WINDOW:])
    if not baseline or recent is None:
        return None
    return round(recent / baseline, 4)


def _support_distance(snapshot: Any, last: float) -> float | None:
    """距最近的下方支撑。返回正数表示支撑在现价下方。"""
    supports = getattr(snapshot, "supports", None) or []
    below = [
        float(level.price)
        for level in supports
        if getattr(level, "price", None) and float(level.price) < last
    ]
    if not below:
        return None
    return round(last / max(below) - 1.0, 6)


def _series(bars: list[dict], key: str) -> list[float]:
    values: list[float] = []
    for bar in bars or []:
        value = bar.get(key)
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return values


def _change(closes: list[float], window: int) -> float | None:
    if len(closes) <= window:
        return None
    previous = closes[-window - 1]
    if previous <= 0:
        return None
    return round(closes[-1] / previous - 1.0, 6)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)
