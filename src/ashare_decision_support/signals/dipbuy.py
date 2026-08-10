"""低吸特征：把「回调到位了吗」拆成可记录、可统计的几个数。

低吸和打板要的读数是**反的**。打板问「有多强」——涨幅、封板质量、连板高度；
低吸问「趋势还在不在、回调够不够深、缩量了没有、是否回到成交成本附近」。同一个
量比在两种策略下的含义甚至相反：打板要放量，低吸的回调段要**缩量**（回调放量
是出货）。

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

from ashare_decision_support.numeric import finite
from ashare_decision_support.signals.series import change, mean, series

# 回调深度的档位，单位是**这只票自己的日均真实波幅（ATR%）**，不是绝对百分比。
#
# 用绝对百分比是错的，2026-08-02 实测八只票一年日线，「回撤 5%~12% 算理想」这套
# 固定档位两头都坏：
#
#   长江电力（日振幅 1.82%）  ideal 只占  8% 的交易日，deep 一次都没到过
#   计算机题材（日振幅 6.23%） deep 占了 49% —— 一个本该意味着「趋势可能已破坏」
#                             的档位成了它的常态，等于没有告警能力
#
# 归一化之后同一档位在不同波动的票上含义才一致：建行一年最大回撤 -11.47%、长电
# -7.85%，绝对值差 46%，除以各自日振幅后是 4.4 与 4.3 —— 几乎相同。
#
# 下面三个数是拿八只票（五只红利蓝筹 + 计算机/传媒/茅台）的档位分布标定出来的，
# 目标是**每只票四个档位都填得满、且没有哪一档吞掉一半以上**。这是先验校准，
# 不是收益验证——哪一档真的更值得买，要等台账样本回答。
SHALLOW_ATR = 0.8
IDEAL_ATR = 2.0
DEEP_ATR = 4.0

# 缩量判定。近 3 日均量掉到 20 日均量的这个比例以下，视作缩量回调。
CONTRACTION_FULL = 0.75

MEDIUM_WINDOW = 20
LONG_WINDOW = 60
RECENT_VOLUME_WINDOW = 3
COST_SHORT_WINDOW = 15
COST_MEDIUM_WINDOW = 30
COST_TOUCH_TOLERANCE = 0.01

# 成交额 / (成交量 * 价格) 用来识别成交量单位：
# 约 1 = 股，约 10 = 十股，约 100 = 手。无法落在这些区间时不算成本，
# 防止上游换了量纲后静默产生一条错误的成本线。
COST_VOLUME_UNIT_BANDS: tuple[tuple[float, float, float], ...] = (
    (0.8, 1.2, 1.0),
    (8.0, 12.0, 10.0),
    (80.0, 120.0, 100.0),
)


@dataclass(frozen=True)
class DipFeatures:
    """一只票在某个交易日收盘后的低吸画像。

    每个字段都可能是 None——数据不足时返回 None 而不是 0，因为「没算出来」和
    「算出来是零」在事后统计里必须能区分开。
    """

    # L1 趋势还在吗
    ret_1d: float | None = None
    ret_20d: float | None = None
    ret_60d: float | None = None
    above_ma20: bool | None = None
    dist_to_ma20_pct: float | None = None

    # L2 回调到位了吗
    pullback_pct: float | None = None          # 距 20 日最高的回撤，≤0。原始百分比，给人看
    atr_pct: float | None = None               # 日均真实波幅占价格的比例，归一化的分母
    pullback_atr: float | None = None          # 回撤 ÷ 日振幅：回调了「几个日常波动」
    pullback_band: str | None = None           # none | shallow | ideal | deep，按 ATR 口径分
    down_days: int | None = None               # 从 20 日高点算起，走了几天
    dist_to_support_pct: float | None = None   # 距最近支撑，正=支撑在下方

    # 回调质量：缩量回调是健康的，放量回调是出货
    volume_contraction: float | None = None    # 近 3 日均量 / 20 日均量
    shrinking: bool | None = None

    # 成交成本代理。它不是筹码平均成本，只是窗口内成交额 / 成交量。
    turnover_cost_15: float | None = None
    turnover_cost_30: float | None = None
    dist_to_turnover_cost_15_pct: float | None = None
    dist_to_turnover_cost_30_pct: float | None = None
    turnover_cost_15_reclaimed: bool | None = None
    turnover_cost_30_reclaimed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def measure_dip(bars: list[dict], snapshot: Any = None) -> DipFeatures:
    """从前复权日线算低吸特征。

    `snapshot` 是可选的 `DailyTechnicalSnapshot`——只用来取已经算好的支撑位，
    不重复造轮子。给 None 时 `dist_to_support_pct` 为 None，其余照算。

    任何情况下都不抛异常：盘前一只票算不出特征，不该让整条链路停下来。
    """
    closes = series(bars, "close")
    if len(closes) < 2:
        return DipFeatures()

    last = closes[-1]
    if last <= 0:
        return DipFeatures()

    highs = series(bars, "high") or closes
    volumes = series(bars, "volume")

    ret_20d = change(closes, MEDIUM_WINDOW)
    ret_60d = change(closes, LONG_WINDOW)
    ret_1d = change(closes, 1)

    ma20 = mean(closes[-MEDIUM_WINDOW:]) if len(closes) >= MEDIUM_WINDOW else None
    dist_to_ma20 = None if not ma20 else round(last / ma20 - 1.0, 6)
    above_ma20 = None if dist_to_ma20 is None else dist_to_ma20 >= 0

    pullback, down_days = _pullback(highs[-MEDIUM_WINDOW:], closes[-MEDIUM_WINDOW:], last)
    contraction = _contraction(volumes)
    atr_pct = _atr_pct(bars[-MEDIUM_WINDOW - 1:])
    normalised = _normalise(pullback, atr_pct)
    turnover_cost_15 = _turnover_cost(bars, COST_SHORT_WINDOW)
    turnover_cost_30 = _turnover_cost(bars, COST_MEDIUM_WINDOW)
    last_bar = bars[-1] if bars else {}

    return DipFeatures(
        ret_1d=ret_1d,
        ret_20d=ret_20d,
        ret_60d=ret_60d,
        above_ma20=above_ma20,
        dist_to_ma20_pct=dist_to_ma20,
        pullback_pct=pullback,
        atr_pct=atr_pct,
        pullback_atr=normalised,
        pullback_band=_band(normalised),
        down_days=down_days,
        dist_to_support_pct=_support_distance(snapshot, last),
        volume_contraction=contraction,
        shrinking=None if contraction is None else contraction <= CONTRACTION_FULL,
        turnover_cost_15=turnover_cost_15,
        turnover_cost_30=turnover_cost_30,
        dist_to_turnover_cost_15_pct=_distance_to_cost(last, turnover_cost_15),
        dist_to_turnover_cost_30_pct=_distance_to_cost(last, turnover_cost_30),
        turnover_cost_15_reclaimed=_reclaimed_cost(last_bar, turnover_cost_15),
        turnover_cost_30_reclaimed=_reclaimed_cost(last_bar, turnover_cost_30),
    )


def _normalise(pullback: float | None, atr_pct: float | None) -> float | None:
    """回撤除以日振幅。分母缺失或为零时返回 None——宁可不分档，也不拿绝对值凑。"""
    if pullback is None or not atr_pct:
        return None
    return round(-pullback / atr_pct, 4)


def _atr_pct(bars: list[dict]) -> float | None:
    """日均真实波幅占价格的比例。

    用真实波幅（含跳空）而不是简单的 `high-low`：A 股一字板和跳空开盘时，当日
    最高最低可以贴在一起，只看日内区间会把「跳空 5%」读成「几乎没波动」。
    """
    ranges: list[float] = []
    previous_close: float | None = None
    for bar in bars or []:
        high, low = finite(bar.get("high")), finite(bar.get("low"))
        close = finite(bar.get("close"))
        if high is None or low is None or close is None:
            continue
        true_range = high - low
        if previous_close is not None:
            true_range = max(
                true_range, abs(high - previous_close), abs(low - previous_close)
            )
        if close > 0:
            ranges.append(true_range / close)
        previous_close = close

    if len(ranges) < 5:
        return None
    return round(sum(ranges) / len(ranges), 6)


def _band(normalised: float | None) -> str | None:
    """按「回调了几个日常波动」分档。

    `ideal` 是低吸的目标窗口：跌得够多、明显超出日常噪声，但还没到该怀疑趋势的
    地步。`deep` 要当成风险读数而不是机会——那时「回调」和「趋势走坏」已经分不开。
    """
    if normalised is None:
        return None
    if normalised < SHALLOW_ATR:
        return "none"
    if normalised < IDEAL_ATR:
        return "shallow"
    if normalised <= DEEP_ATR:
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
    baseline = mean(volumes[-MEDIUM_WINDOW:])
    recent = mean(volumes[-RECENT_VOLUME_WINDOW:])
    if not baseline or recent is None:
        return None
    return round(recent / baseline, 4)


def _turnover_cost(bars: list[dict], window: int) -> float | None:
    """用成交额 / 成交量估算窗口成交成本。

    TickFlow 的日线成交量可能以「手」返回，新浪通常以「股」返回。这里按每根
    K 线的 `amount / (volume * close)` 反推单位；任何一根无法确认单位的 K 线都会
    让整条窗口成本返回 None，而不是拿错量纲继续算。
    """
    if len(bars) < window:
        return None

    amount_total = 0.0
    volume_total = 0.0
    for row in bars[-window:]:
        amount = finite(row.get("amount"))
        volume = _normalised_volume(row)
        if amount is None or amount <= 0 or volume is None or volume <= 0:
            return None
        amount_total += amount
        volume_total += volume

    if volume_total <= 0:
        return None
    return round(amount_total / volume_total, 4)


def normalised_volume(row: dict) -> float | None:
    """把一根 K 线的成交量归一成**股**，单位靠 `amount / (volume * close)` 反推。

    公开而不是私有，是因为 `signals/volume_price.py` 的换手率需要同一份推断——
    量纲搞错不会报错，只会让换手率差 100 倍，然后每只票看起来都像天量。反推不出
    单位时返回 None：**宁可不给换手率，也不给一个错了两个数量级的数。**
    """
    volume = finite(row.get("volume"))
    amount = finite(row.get("amount"))
    close = finite(row.get("close"))
    if volume is None or volume <= 0 or amount is None or amount <= 0 or close is None or close <= 0:
        return None

    implied_unit = amount / (volume * close)
    for low, high, multiplier in COST_VOLUME_UNIT_BANDS:
        if low <= implied_unit <= high:
            return volume * multiplier
    return None


def _normalised_volume(row: dict) -> float | None:
    return normalised_volume(row)


def _distance_to_cost(price: float, cost: float | None) -> float | None:
    if price <= 0 or cost is None or cost <= 0:
        return None
    return round(price / cost - 1.0, 6)


def _reclaimed_cost(row: dict, cost: float | None) -> bool | None:
    """当日最低触及成本附近，收盘重新站回成本线上方。"""
    if cost is None or cost <= 0:
        return None
    close = finite(row.get("close"))
    low = finite(row.get("low"))
    if close is None or close <= 0 or low is None:
        return None
    return low <= cost * (1.0 + COST_TOUCH_TOLERANCE) and close >= cost


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
