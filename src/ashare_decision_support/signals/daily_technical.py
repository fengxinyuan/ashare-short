from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import fmean

from ashare_decision_support.models.enums import TrendState
from ashare_decision_support.models.schemas import DailyTechnicalSnapshot, PriceLevel
from ashare_decision_support.numeric import clamp
from ashare_decision_support.schedule.timezone import beijing_naive_now
from ashare_decision_support.signals.series import normalize_bars

# 止损距离的上下限，单位是标的自身的 ATR。低于下限的止损是随机噪声（必被打掉），
# 高于上限的止损保护不了任何东西。用 ATR 而非固定百分比，理由见 `_stop_reference`。
MIN_STOP_ATR = 1.0
MAX_STOP_ATR = 3.0

# ATR 算不出来时的兜底波幅，取 A 股日均振幅的经验量级。
DEFAULT_ATR_PCT = 0.02


@dataclass
class _Pivot:
    side: str
    price: float
    ts: datetime
    index: int


class DailyTechnicalAnalyzer:
    def __init__(
        self,
        trend_window: int = 60,
        support_resistance_window: int = 3,
        max_levels: int = 3,
    ) -> None:
        self.trend_window = trend_window
        self.support_resistance_window = support_resistance_window
        self.max_levels = max_levels

    def analyze_many(self, bars_by_symbol: dict[str, list[dict]]) -> dict[str, DailyTechnicalSnapshot]:
        return {symbol: self.analyze(symbol, bars) for symbol, bars in bars_by_symbol.items()}

    def analyze(self, symbol: str, bars: list[dict]) -> DailyTechnicalSnapshot:
        rows = self._normalize_rows(bars)
        if not rows:
            now = beijing_naive_now()
            return DailyTechnicalSnapshot(
                symbol=symbol,
                bar_count=0,
                last_close=0.01,
                last_open=0.01,
                last_high=0.01,
                last_low=0.01,
                trend_state=TrendState.RANGE,
                explanation="无可用日K数据，无法计算技术快照。",
                source_ts=now,
            )

        last = rows[-1]
        closes = [row["close"] for row in rows]
        ma5 = self._moving_average(closes, 5)
        ma10 = self._moving_average(closes, 10)
        ma20 = self._moving_average(closes, 20)
        ma60 = self._moving_average(closes, 60)
        atr14 = self._atr(rows, 14)
        volume_ratio_20d = self._volume_ratio(rows, 20)
        range_position_60d = self._range_position(rows, 60)
        trend_state = self._trend_state(last["close"], ma20, ma60)

        supports, resistances = self._levels(rows, last["close"], atr14 or last["close"] * 0.02)
        stop_reference = self._stop_reference(last["close"], supports, ma10 or ma20, atr14)
        target_reference = resistances[0].price if resistances else None
        explanation = self._explanation(trend_state, supports, resistances, ma20, ma60, atr14)
        setups = self._setups(last["close"], trend_state, supports, resistances, ma20, volume_ratio_20d)

        return DailyTechnicalSnapshot(
            symbol=symbol,
            bar_count=len(rows),
            last_close=last["close"],
            last_open=last["open"],
            last_high=last["high"],
            last_low=last["low"],
            ma5=ma5,
            ma10=ma10,
            ma20=ma20,
            ma60=ma60,
            atr14=atr14,
            volume_ratio_20d=volume_ratio_20d,
            range_position_60d=range_position_60d,
            trend_state=trend_state,
            supports=supports,
            resistances=resistances,
            stop_reference=stop_reference,
            target_reference=target_reference,
            setups=setups,
            explanation=explanation,
            source_ts=last["timestamp"],
        )

    def _stop_reference(
        self,
        close: float,
        supports: list[PriceLevel],
        fallback: float | None,
        atr14: float | None,
    ) -> float | None:
        """止损参考位，带一个按标的自身波动定的最小距离。

        直接取最近支撑是错的。2026-08-02 实测：603508.SH 现价 23.16、最近支撑
        23.15，止损距离 **0.04%**——A 股日内正常振幅 3–5%，这种止损是随机噪声，
        必然被打掉，而且它会让盈亏比算出 **172.84** 这种数直接拿满分。

        所以止损必须至少落在**一个日均波幅之外**：低于这个距离的不是止损，是
        在赌下一跳的方向。用 ATR 而不是固定百分比，理由和 `signals/dipbuy.py`
        的档位一样——同一个百分比对低波和高波标的意义完全不同。

        另一头也要管：超过 MAX_STOP_ATR 个波幅的「止损」已经保护不了任何东西，
        此时收回到上限，让盈亏比反映真实的风险敞口而不是一个假的大分母。
        """
        if close <= 0:
            return None

        unit = atr14 if atr14 and atr14 > 0 else close * DEFAULT_ATR_PCT
        floor = close - MAX_STOP_ATR * unit
        ceiling = close - MIN_STOP_ATR * unit

        candidate = supports[0].price if supports else fallback
        if not candidate or candidate <= 0:
            candidate = ceiling
        # 支撑在现价上方时它不是支撑，退回到按波幅算的位置。
        return round(clamp(min(candidate, ceiling), floor, ceiling), 4)

    def _normalize_rows(self, bars: list[dict]) -> list[dict]:
        return normalize_bars(bars)

    def _moving_average(self, values: list[float], window: int) -> float | None:
        if len(values) < window:
            return None
        return round(fmean(values[-window:]), 4)

    def _atr(self, rows: list[dict], window: int) -> float | None:
        if len(rows) < window + 1:
            return None
        true_ranges: list[float] = []
        for index in range(1, len(rows)):
            row = rows[index]
            prev_close = rows[index - 1]["close"]
            true_ranges.append(max(row["high"] - row["low"], abs(row["high"] - prev_close), abs(row["low"] - prev_close)))
        if len(true_ranges) < window:
            return None
        return round(fmean(true_ranges[-window:]), 4)

    def _volume_ratio(self, rows: list[dict], window: int) -> float | None:
        if len(rows) < window + 1:
            return None
        baseline = [row["volume"] for row in rows[-(window + 1):-1] if row["volume"] > 0]
        if not baseline:
            return None
        return round(rows[-1]["volume"] / fmean(baseline), 4)

    def _range_position(self, rows: list[dict], window: int) -> float | None:
        data = rows[-window:] if len(rows) >= window else rows
        if not data:
            return None
        low = min(row["low"] for row in data)
        high = max(row["high"] for row in data)
        span = high - low
        if span <= 0:
            return 0.5
        return round(clamp((rows[-1]["close"] - low) / span, 0.0, 1.0), 4)

    def _trend_state(self, last_close: float, ma20: float | None, ma60: float | None) -> TrendState:
        if ma20 is None or ma60 is None:
            return TrendState.RANGE
        if last_close > ma20 > ma60:
            return TrendState.UPTREND
        if last_close > ma20 and ma20 <= ma60:
            return TrendState.REBOUND
        if last_close < ma20 < ma60:
            return TrendState.DOWNTREND
        return TrendState.RANGE

    def _levels(self, rows: list[dict], last_close: float, atr: float) -> tuple[list[PriceLevel], list[PriceLevel]]:
        pivots = self._find_pivots(rows)
        threshold = max(atr * 0.8, last_close * 0.012)
        supports = self._cluster_levels([pivot for pivot in pivots if pivot.side == "support"], threshold, last_close, "support")
        resistances = self._cluster_levels([pivot for pivot in pivots if pivot.side == "resistance"], threshold, last_close, "resistance")
        return supports[: self.max_levels], resistances[: self.max_levels]

    def _find_pivots(self, rows: list[dict]) -> list[_Pivot]:
        result: list[_Pivot] = []
        w = self.support_resistance_window
        for index in range(w, len(rows) - w):
            row = rows[index]
            lows = [rows[idx]["low"] for idx in range(index - w, index + w + 1)]
            highs = [rows[idx]["high"] for idx in range(index - w, index + w + 1)]
            if row["low"] == min(lows):
                result.append(_Pivot("support", row["low"], row["timestamp"], index))
            if row["high"] == max(highs):
                result.append(_Pivot("resistance", row["high"], row["timestamp"], index))
        return result

    def _cluster_levels(
        self,
        pivots: list[_Pivot],
        threshold: float,
        last_close: float,
        side: str,
    ) -> list[PriceLevel]:
        filtered = [
            pivot
            for pivot in pivots
            if (side == "support" and pivot.price <= last_close) or (side == "resistance" and pivot.price >= last_close)
        ]
        filtered.sort(key=lambda item: item.price)

        clusters: list[list[_Pivot]] = []
        for pivot in filtered:
            if not clusters or abs(clusters[-1][-1].price - pivot.price) > threshold:
                clusters.append([pivot])
            else:
                clusters[-1].append(pivot)

        levels: list[PriceLevel] = []
        for cluster in clusters:
            price = round(fmean([item.price for item in cluster]), 4)
            touches = len(cluster)
            last_touch = max(cluster, key=lambda item: item.ts)
            distance_pct = round((price / last_close - 1.0) * 100.0, 4)
            strength = round(touches * 1.0 + max(0.0, 3.0 - abs(distance_pct)), 4)
            label = "支撑" if side == "support" else "压力"
            levels.append(
                PriceLevel(
                    side=side,
                    price=price,
                    touches=touches,
                    strength=strength,
                    distance_pct=distance_pct,
                    last_touch_ts=last_touch.ts,
                    label=f"{label}{len(levels) + 1}",
                )
            )

        if side == "support":
            levels.sort(key=lambda item: (abs(item.distance_pct), -item.strength))
        else:
            levels.sort(key=lambda item: (abs(item.distance_pct), -item.strength))
        return levels

    def _explanation(
        self,
        trend_state: TrendState,
        supports: list[PriceLevel],
        resistances: list[PriceLevel],
        ma20: float | None,
        ma60: float | None,
        atr14: float | None,
    ) -> str:
        parts = [f"日线趋势为 {trend_state.value}"]
        if ma20 is not None and ma60 is not None:
            parts.append(f"MA20={ma20:.2f} / MA60={ma60:.2f}")
        if supports:
            parts.append(f"最近支撑参考 {supports[0].price:.2f}")
        if resistances:
            parts.append(f"最近压力参考 {resistances[0].price:.2f}")
        if atr14 is not None:
            parts.append(f"ATR14={atr14:.2f}")
        return "，".join(parts) + "。"

    def _setups(
        self,
        last_close: float,
        trend_state: TrendState,
        supports: list[PriceLevel],
        resistances: list[PriceLevel],
        ma20: float | None,
        volume_ratio_20d: float | None,
    ) -> list[dict]:
        setups: list[dict] = []
        nearest_support = supports[0] if supports else None
        nearest_resistance = resistances[0] if resistances else None

        if trend_state in {TrendState.UPTREND, TrendState.REBOUND} and nearest_resistance is not None:
            setups.append(
                {
                    "setup": "breakout_follow",
                    "bias": "bullish",
                    "trigger_price": round(nearest_resistance.price * 1.003, 4),
                    "stop_price": round((nearest_support.price if nearest_support else (ma20 or last_close * 0.97)) * 0.997, 4),
                    "target_price": round(nearest_resistance.price * 1.05, 4),
                    "confidence": round(0.66 + (0.08 if volume_ratio_20d and volume_ratio_20d >= 1.1 else 0.0), 4),
                    "reason": "趋势未坏，接近上沿，适合做突破跟随。",
                }
            )

        if nearest_support is not None and abs(nearest_support.distance_pct) <= 4.0:
            setups.append(
                {
                    "setup": "support_reclaim",
                    "bias": "bullish" if trend_state != TrendState.DOWNTREND else "neutral",
                    "trigger_price": round(nearest_support.price * 1.01, 4),
                    "stop_price": round(nearest_support.price * 0.985, 4),
                    "target_price": round((nearest_resistance.price if nearest_resistance else last_close * 1.04), 4),
                    "confidence": round(0.58 + min(nearest_support.strength / 20.0, 0.15), 4),
                    "reason": "靠近有效支撑，适合观察回踩修复。",
                }
            )

        if trend_state == TrendState.RANGE and nearest_support is not None and nearest_resistance is not None:
            setups.append(
                {
                    "setup": "range_rotation",
                    "bias": "neutral",
                    "trigger_price": round(nearest_support.price * 1.008, 4),
                    "stop_price": round(nearest_support.price * 0.984, 4),
                    "target_price": round(nearest_resistance.price * 0.992, 4),
                    "confidence": 0.52,
                    "reason": "仍处区间结构，更适合低吸高抛而不是追价。",
                }
            )

        if trend_state == TrendState.DOWNTREND and nearest_resistance is not None:
            setups.append(
                {
                    "setup": "rebound_fail_watch",
                    "bias": "bearish",
                    "trigger_price": round(nearest_resistance.price * 0.995, 4),
                    "stop_price": round(nearest_resistance.price * 1.015, 4),
                    "target_price": round((nearest_support.price if nearest_support else last_close * 0.96), 4),
                    "confidence": 0.55,
                    "reason": "下跌趋势未修复，优先防守反弹受阻。",
                }
            )

        return setups[:3]
