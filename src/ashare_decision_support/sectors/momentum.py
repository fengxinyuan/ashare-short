"""Aggregate constituent daily bars into sector behaviour.

Everything here is computed from bars the adapter already returns, which is why
it works on the free tier: the 东财 board-quote endpoints that would give this
directly are unreachable, but the constituent bars are not.

Forward-adjusted closes are fine for every metric below — adjustment is a
multiplicative rescale of a single symbol's series, so ratios inside one series
are unaffected. Do not reuse these bars for limit-up detection at the price
level; `strong_count` deliberately uses a return threshold instead.
"""

from __future__ import annotations

from statistics import fmean

from ashare_decision_support.numeric import median_of
from ashare_decision_support.sectors.base import SectorDefinition, SectorMetrics
from ashare_decision_support.sectors.panel import rounded
from ashare_decision_support.signals.dipbuy import measure_dip

# A 9.5% daily gain is at the main-board limit. 创业板/科创板 stocks cap at 20%,
# so this counts "moved hard", not "sealed the limit".
#
# 付费档现在能取到真实涨跌停价了——`instruments.batch` 对全 A 5534 只 100% 覆盖，
# 一次 0.97 秒，见 `adapters/tickflow_market.py`。这里**刻意还没改用它**：
# `strong_count` 直接喂进 `sectors/predict.py` 的板块打分，换判据会让所有历史
# 板块分数不可比。要改就得连同重新标定阈值一起做，不能顺手改。
STRONG_MOVE_THRESHOLD = 0.095

VOLUME_BASELINE_BARS = 20
MA_BARS = 20

# 覆盖率闸门**不在这里**：这一层只负责测量，`covered / constituents` 原样带出去，
# 由 `sectors/predict.py:MIN_TRUSTED_COVERAGE` 决定多少算达标。此处原来还有一个
# 同名不同值的 MIN_COVERAGE_RATIO，从未被读过——两个覆盖率阈值放在两个模块里，
# 迟早有人去调那个不生效的。


class SectorMomentum:
    """Turns {symbol: bars} into per-sector metrics."""

    def __init__(self, strong_threshold: float = STRONG_MOVE_THRESHOLD) -> None:
        self.strong_threshold = strong_threshold

    def measure(
        self,
        definitions: list[SectorDefinition],
        bars_by_symbol: dict[str, list[dict]],
    ) -> dict[str, SectorMetrics]:
        result: dict[str, SectorMetrics] = {}
        for definition in definitions:
            result[definition.name] = self._measure_one(definition, bars_by_symbol)
        return result

    def _measure_one(
        self,
        definition: SectorDefinition,
        bars_by_symbol: dict[str, list[dict]],
    ) -> SectorMetrics:
        rets_1d: list[float] = []
        rets_5d: list[float] = []
        rets_20d: list[float] = []
        pullbacks: list[float] = []
        pullbacks_atr: list[float] = []
        volume_ratios: list[float] = []
        above_ma = 0
        ma_samples = 0
        covered = 0

        for symbol in definition.symbols:
            bars = bars_by_symbol.get(symbol) or []
            closes = _closes(bars)
            if len(closes) < 2:
                continue
            covered += 1

            ret_1d = _pct_change(closes[-2], closes[-1])
            if ret_1d is not None:
                rets_1d.append(ret_1d)
            if len(closes) >= 6:
                ret_5d = _pct_change(closes[-6], closes[-1])
                if ret_5d is not None:
                    rets_5d.append(ret_5d)
            if len(closes) >= MA_BARS + 1:
                ret_20d = _pct_change(closes[-(MA_BARS + 1)], closes[-1])
                if ret_20d is not None:
                    rets_20d.append(ret_20d)

            # 成分股的回调画像沿用个股那一套，保证板块层与个股层口径一致。
            features = measure_dip(bars)
            if features.pullback_pct is not None:
                pullbacks.append(features.pullback_pct)
            if features.pullback_atr is not None:
                pullbacks_atr.append(features.pullback_atr)

            ratio = _volume_ratio(bars)
            if ratio is not None:
                volume_ratios.append(ratio)

            if len(closes) >= MA_BARS:
                ma_samples += 1
                if closes[-1] > fmean(closes[-MA_BARS:]):
                    above_ma += 1

        return SectorMetrics(
            name=definition.name,
            constituents=definition.size,
            covered=covered,
            ret_1d=rounded(median_of(rets_1d), 6),
            ret_5d=rounded(median_of(rets_5d), 6),
            ret_20d=rounded(median_of(rets_20d), 6),
            pullback=rounded(median_of(pullbacks), 6),
            pullback_atr=rounded(median_of(pullbacks_atr), 4),
            breadth=round(sum(1 for value in rets_1d if value > 0) / len(rets_1d), 4) if rets_1d else None,
            strong_count=sum(1 for value in rets_1d if value >= self.strong_threshold),
            volume_ratio=rounded(median_of(volume_ratios), 4),
            above_ma20=round(above_ma / ma_samples, 4) if ma_samples else None,
        )


def _closes(bars: list[dict]) -> list[float]:
    values: list[float] = []
    for bar in bars:
        close = bar.get("close")
        if isinstance(close, (int, float)) and close > 0:
            values.append(float(close))
    return values


def _pct_change(previous: float, current: float) -> float | None:
    if previous <= 0:
        return None
    return current / previous - 1.0


def _volume_ratio(bars: list[dict]) -> float | None:
    """Latest volume against the mean of the preceding baseline window."""
    volumes = [
        float(bar["volume"])
        for bar in bars
        if isinstance(bar.get("volume"), (int, float)) and bar["volume"] > 0
    ]
    if len(volumes) < VOLUME_BASELINE_BARS + 1:
        return None
    baseline = fmean(volumes[-(VOLUME_BASELINE_BARS + 1) : -1])
    if baseline <= 0:
        return None
    return volumes[-1] / baseline
