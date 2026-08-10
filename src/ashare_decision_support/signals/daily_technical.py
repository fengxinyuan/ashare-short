from __future__ import annotations

import html
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
        setups = self._setups(last["close"], trend_state, supports, resistances, ma20, ma60, volume_ratio_20d)

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
            chart_bars=[self._chart_bar(row) for row in rows[-80:]],
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
        ma60: float | None,
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

    def _chart_bar(self, row: dict) -> dict:
        return {
            "ts": row["timestamp"].isoformat(),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        }


class DailyKChartRenderer:
    def __init__(self, width: int = 1200, height: int = 760) -> None:
        self.width = width
        self.height = height

    def render_svg(self, snapshot: DailyTechnicalSnapshot) -> str:
        bars = snapshot.chart_bars[-80:]
        if not bars:
            return self._empty_svg(snapshot.symbol)

        pad_left = 70
        pad_right = 180
        pad_top = 50
        price_height = 450
        volume_height = 120
        gap = 30
        chart_width = self.width - pad_left - pad_right
        price_bottom = pad_top + price_height
        volume_top = price_bottom + gap
        volume_bottom = volume_top + volume_height

        lows = [bar["low"] for bar in bars]
        highs = [bar["high"] for bar in bars]
        price_min = min(lows) * 0.985
        price_max = max(highs) * 1.015
        price_span = max(price_max - price_min, 0.01)
        max_volume = max(bar["volume"] for bar in bars) or 1.0
        candle_width = max(chart_width / max(len(bars), 1) * 0.55, 3.0)

        def px(index: int) -> float:
            return pad_left + chart_width * (index + 0.5) / max(len(bars), 1)

        def py(price: float) -> float:
            return pad_top + (price_max - price) / price_span * price_height

        def vy(volume: float) -> float:
            return volume_bottom - volume / max_volume * volume_height

        parts: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" viewBox="0 0 {self.width} {self.height}">',
            '<rect width="100%" height="100%" fill="#f7f3ea"/>',
            f'<text x="{pad_left}" y="28" font-size="22" font-family="monospace" fill="#1d2a39">{html.escape(snapshot.symbol)} 日K技术图</text>',
            f'<text x="{pad_left}" y="{self.height - 18}" font-size="12" font-family="monospace" fill="#5f6b7a">{html.escape(snapshot.explanation)}</text>',
        ]

        for step in range(6):
            y = pad_top + price_height * step / 5
            price = price_max - price_span * step / 5
            parts.append(f'<line x1="{pad_left}" y1="{y:.2f}" x2="{pad_left + chart_width}" y2="{y:.2f}" stroke="#d7d2c8" stroke-width="1"/>')
            parts.append(f'<text x="10" y="{y + 4:.2f}" font-size="12" font-family="monospace" fill="#6d7a88">{price:.2f}</text>')

        for index, bar in enumerate(bars):
            x = px(index)
            color = "#d1495b" if bar["close"] >= bar["open"] else "#2b7a78"
            parts.append(f'<line x1="{x:.2f}" y1="{py(bar["high"]):.2f}" x2="{x:.2f}" y2="{py(bar["low"]):.2f}" stroke="{color}" stroke-width="1.2"/>')
            top = py(max(bar["open"], bar["close"]))
            bottom = py(min(bar["open"], bar["close"]))
            height = max(bottom - top, 1.2)
            parts.append(f'<rect x="{x - candle_width / 2:.2f}" y="{top:.2f}" width="{candle_width:.2f}" height="{height:.2f}" fill="{color}" opacity="0.85"/>')
            v_top = vy(bar["volume"])
            parts.append(f'<rect x="{x - candle_width / 2:.2f}" y="{v_top:.2f}" width="{candle_width:.2f}" height="{volume_bottom - v_top:.2f}" fill="{color}" opacity="0.40"/>')

        self._add_ma(parts, bars, 5, "#f08c00", px, py)
        self._add_ma(parts, bars, 20, "#355070", px, py)
        self._add_ma(parts, bars, 60, "#6d597a", px, py)

        for level in snapshot.supports:
            y = py(level.price)
            parts.append(f'<line x1="{pad_left}" y1="{y:.2f}" x2="{pad_left + chart_width}" y2="{y:.2f}" stroke="#2a9d8f" stroke-width="1.4" stroke-dasharray="7 5"/>')
            parts.append(f'<text x="{pad_left + chart_width + 12}" y="{y + 4:.2f}" font-size="12" font-family="monospace" fill="#2a9d8f">{html.escape(level.label)} {level.price:.2f}</text>')

        for level in snapshot.resistances:
            y = py(level.price)
            parts.append(f'<line x1="{pad_left}" y1="{y:.2f}" x2="{pad_left + chart_width}" y2="{y:.2f}" stroke="#d1495b" stroke-width="1.4" stroke-dasharray="7 5"/>')
            parts.append(f'<text x="{pad_left + chart_width + 12}" y="{y + 4:.2f}" font-size="12" font-family="monospace" fill="#d1495b">{html.escape(level.label)} {level.price:.2f}</text>')

        info_x = pad_left + chart_width + 10
        info_y = volume_top
        lines = [
            f"Trend: {snapshot.trend_state.value}",
            f"Close: {snapshot.last_close:.2f}",
            f"MA5: {snapshot.ma5:.2f}" if snapshot.ma5 is not None else "MA5: -",
            f"MA20: {snapshot.ma20:.2f}" if snapshot.ma20 is not None else "MA20: -",
            f"MA60: {snapshot.ma60:.2f}" if snapshot.ma60 is not None else "MA60: -",
            f"ATR14: {snapshot.atr14:.2f}" if snapshot.atr14 is not None else "ATR14: -",
            f"VolRatio20: {snapshot.volume_ratio_20d:.2f}" if snapshot.volume_ratio_20d is not None else "VolRatio20: -",
        ]
        if snapshot.stop_reference is not None:
            lines.append(f"StopRef: {snapshot.stop_reference:.2f}")
        if snapshot.target_reference is not None:
            lines.append(f"TargetRef: {snapshot.target_reference:.2f}")
        if snapshot.setups:
            top_setup = snapshot.setups[0]
            lines.append(f"Setup: {top_setup['setup']}")
            lines.append(f"Trigger: {top_setup['trigger_price']:.2f}")
            lines.append(f"Stop: {top_setup['stop_price']:.2f}")
            lines.append(f"Target: {top_setup['target_price']:.2f}")
        for idx, line in enumerate(lines):
            parts.append(f'<text x="{info_x}" y="{info_y + idx * 18:.2f}" font-size="12" font-family="monospace" fill="#1d2a39">{html.escape(line)}</text>')

        parts.append("</svg>")
        return "".join(parts)

    def _add_ma(self, parts: list[str], bars: list[dict], window: int, color: str, px, py) -> None:
        if len(bars) < window:
            return
        points: list[str] = []
        for idx in range(window - 1, len(bars)):
            ma = fmean([bar["close"] for bar in bars[idx - window + 1 : idx + 1]])
            points.append(f"{px(idx):.2f},{py(ma):.2f}")
        if points:
            parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.8" points="{" ".join(points)}"/>')

    def _empty_svg(self, symbol: str) -> str:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}">'
            '<rect width="100%" height="100%" fill="#f7f3ea"/>'
            f'<text x="40" y="80" font-size="24" font-family="monospace" fill="#1d2a39">{html.escape(symbol)} 无日K数据</text>'
            '</svg>'
        )
