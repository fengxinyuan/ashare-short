from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any

from ashare_decision_support.models.enums import EffortResult, WyckoffBias
from ashare_decision_support.models.schemas import WyckoffSignal


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


class WyckoffAnalyzer:
    """Lightweight Wyckoff-style structure analyzer for research overlays."""

    def __init__(
        self,
        signal_window: int = 10,
        trend_window: int = 20,
        breakout_buffer_pct: float = 0.5,
        volume_confirm_ratio: float = 1.2,
    ) -> None:
        self.signal_window = signal_window
        self.trend_window = trend_window
        self.breakout_buffer_pct = breakout_buffer_pct
        self.volume_confirm_ratio = volume_confirm_ratio

    def analyze_many(self, bars_by_symbol: dict[str, list[dict]]) -> list[WyckoffSignal]:
        return [self.analyze(symbol, bars) for symbol, bars in bars_by_symbol.items()]

    def analyze(self, symbol: str, bars: list[dict]) -> WyckoffSignal:
        normalized = self._normalize_rows(bars)
        if len(normalized) < max(self.signal_window + 2, self.trend_window + 1):
            source_ts = normalized[-1]["timestamp"] if normalized else datetime.now()
            return WyckoffSignal(
                symbol=symbol,
                bias=WyckoffBias.NEUTRAL,
                score=0.0,
                close_position=0.5,
                short_return_pct=0.0,
                medium_return_pct=0.0,
                volume_ratio=1.0,
                compression_ratio=0.0,
                effort_result=EffortResult.NEUTRAL,
                explanation="历史样本不足，暂不输出威科夫结构倾向。",
                source_ts=source_ts,
            )

        last = normalized[-1]
        prior_signal_window = normalized[-(self.signal_window + 1) : -1]
        prior_trend_window = normalized[-(self.trend_window + 1) : -1]
        recent_short = normalized[-6:]
        recent_medium = normalized[-(self.trend_window + 1) :]

        support = min(row["low"] for row in prior_signal_window)
        resistance = max(row["high"] for row in prior_signal_window)
        range_low = min(row["low"] for row in prior_trend_window)
        range_high = max(row["high"] for row in prior_trend_window)
        range_span = max(range_high - range_low, 0.01)
        close_position = _clamp((last["close"] - range_low) / range_span, 0.0, 1.0)

        short_return_pct = self._return_pct(recent_short[0]["close"], last["close"])
        medium_return_pct = self._return_pct(recent_medium[0]["close"], last["close"])
        volume_ratio = self._volume_ratio(normalized)
        compression_ratio = self._compression_ratio(normalized)

        spring = (
            last["low"] < support * (1.0 - self.breakout_buffer_pct / 100.0)
            and last["close"] >= support
            and close_position >= 0.55
            and volume_ratio >= self.volume_confirm_ratio
        )
        upthrust = (
            last["high"] > resistance * (1.0 + self.breakout_buffer_pct / 100.0)
            and last["close"] <= resistance
            and close_position <= 0.45
            and volume_ratio >= self.volume_confirm_ratio
        )
        sign_of_strength = (
            last["close"] > resistance * (1.0 + self.breakout_buffer_pct / 200.0)
            and last["close"] > last["open"]
            and volume_ratio >= self.volume_confirm_ratio
        )
        last_point_of_support = (
            not spring
            and short_return_pct > 0
            and close_position >= 0.58
            and min(row["low"] for row in recent_short[-3:]) >= support
            and 0.85 <= volume_ratio <= 1.35
        )
        effort_result = self._effort_result(normalized, volume_ratio)

        score = 0.0
        score += 12.0 if medium_return_pct > 0 else -12.0
        score += 8.0 if short_return_pct > 0 else -8.0
        score += (close_position - 0.5) * 32.0
        score += compression_ratio * 12.0
        if spring:
            score += 24.0
        if sign_of_strength:
            score += 18.0
        if last_point_of_support:
            score += 12.0
        if upthrust:
            score -= 24.0
        if effort_result == EffortResult.UP_EFFORT_CONFIRMED:
            score += 10.0
        elif effort_result == EffortResult.DOWN_EFFORT_CONFIRMED:
            score -= 10.0
        elif effort_result == EffortResult.ABSORPTION:
            score += 8.0 if close_position >= 0.5 else -8.0
        elif effort_result == EffortResult.EXHAUSTION:
            score -= 8.0 if close_position >= 0.5 else 8.0
        elif effort_result == EffortResult.COMPRESSION:
            score += 4.0 if short_return_pct >= 0 else -4.0
        score = round(_clamp(score, -100.0, 100.0), 2)

        bias = self._classify_bias(
            score=score,
            spring=spring,
            upthrust=upthrust,
            sign_of_strength=sign_of_strength,
            medium_return_pct=medium_return_pct,
        )
        explanation = self._explanation(
            bias=bias,
            spring=spring,
            upthrust=upthrust,
            sign_of_strength=sign_of_strength,
            last_point_of_support=last_point_of_support,
            effort_result=effort_result,
            compression_ratio=compression_ratio,
        )

        return WyckoffSignal(
            symbol=symbol,
            bias=bias,
            score=score,
            close_position=round(close_position, 4),
            short_return_pct=round(short_return_pct, 4),
            medium_return_pct=round(medium_return_pct, 4),
            volume_ratio=round(volume_ratio, 4),
            compression_ratio=round(compression_ratio, 4),
            spring=spring,
            upthrust=upthrust,
            sign_of_strength=sign_of_strength,
            last_point_of_support=last_point_of_support,
            effort_result=effort_result,
            explanation=explanation,
            source_ts=last["timestamp"],
        )

    def _normalize_rows(self, bars: list[dict]) -> list[dict]:
        normalized: list[dict] = []
        for row in bars:
            if not isinstance(row, dict):
                continue
            open_price = self._float_from_keys(row, "open", "开盘")
            high = self._float_from_keys(row, "high", "最高")
            low = self._float_from_keys(row, "low", "最低")
            close = self._float_from_keys(row, "close", "收盘")
            volume = self._float_from_keys(row, "volume", "成交量")
            timestamp = self._datetime_from_keys(row, "timestamp", "date", "日期", "时间")
            if close is None:
                continue
            open_price = close if open_price is None else open_price
            high = max(open_price, close) if high is None else high
            low = min(open_price, close) if low is None else low
            normalized.append(
                {
                    "timestamp": timestamp,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": 0.0 if volume is None else volume,
                }
            )
        return sorted(normalized, key=lambda row: row["timestamp"])

    def _classify_bias(
        self,
        score: float,
        spring: bool,
        upthrust: bool,
        sign_of_strength: bool,
        medium_return_pct: float,
    ) -> WyckoffBias:
        if upthrust and score <= -10:
            return WyckoffBias.DISTRIBUTION if medium_return_pct >= -3.0 else WyckoffBias.MARKDOWN
        if spring and score >= 10:
            return WyckoffBias.ACCUMULATION if medium_return_pct <= 8.0 else WyckoffBias.MARKUP
        if sign_of_strength and score >= 30:
            return WyckoffBias.MARKUP
        if score >= 35:
            return WyckoffBias.MARKUP
        if score >= 12:
            return WyckoffBias.ACCUMULATION
        if score <= -35:
            return WyckoffBias.MARKDOWN
        if score <= -12:
            return WyckoffBias.DISTRIBUTION
        return WyckoffBias.NEUTRAL

    def _explanation(
        self,
        bias: WyckoffBias,
        spring: bool,
        upthrust: bool,
        sign_of_strength: bool,
        last_point_of_support: bool,
        effort_result: EffortResult,
        compression_ratio: float,
    ) -> str:
        parts = [f"结构偏向 {bias.value}"]
        if spring:
            parts.append("出现 spring 式假跌破回收")
        if upthrust:
            parts.append("出现 upthrust 式假突破回落")
        if sign_of_strength:
            parts.append("存在 SOS 放量上推")
        if last_point_of_support:
            parts.append("具备 LPS 弱回踩承接")
        if compression_ratio >= 0.2:
            parts.append("波动压缩后有扩张准备")
        if effort_result != EffortResult.NEUTRAL:
            parts.append(f"努力-结果关系为 {effort_result.value}")
        return "，".join(parts) + "。"

    def _effort_result(self, rows: list[dict], volume_ratio: float) -> EffortResult:
        last = rows[-1]
        recent = rows[-10:]
        body_pct = abs(last["close"] - last["open"]) / max(last["close"], 0.01) * 100.0
        body_baseline = median(
            abs(row["close"] - row["open"]) / max(row["close"], 0.01) * 100.0
            for row in recent
        )
        short_move_pct = self._return_pct(rows[-4]["close"], last["close"])
        if volume_ratio >= 1.4 and short_move_pct >= 1.5:
            return EffortResult.UP_EFFORT_CONFIRMED
        if volume_ratio >= 1.4 and short_move_pct <= -1.5:
            return EffortResult.DOWN_EFFORT_CONFIRMED
        if volume_ratio >= 1.4 and body_pct <= body_baseline * 0.8:
            return EffortResult.ABSORPTION
        if volume_ratio <= 0.75 and abs(short_move_pct) >= 1.2:
            return EffortResult.EXHAUSTION
        if volume_ratio <= 0.85 and body_pct <= body_baseline:
            return EffortResult.COMPRESSION
        return EffortResult.NEUTRAL

    def _compression_ratio(self, rows: list[dict]) -> float:
        recent = rows[-5:]
        baseline = rows[-20:-5]
        if not baseline:
            return 0.0
        recent_range = median(self._range_pct(row) for row in recent)
        baseline_range = median(self._range_pct(row) for row in baseline)
        if baseline_range <= 0:
            return 0.0
        return _clamp(1.0 - recent_range / baseline_range, 0.0, 1.0)

    def _volume_ratio(self, rows: list[dict]) -> float:
        baseline = [row["volume"] for row in rows[-21:-1] if row["volume"] > 0]
        if not baseline:
            return 1.0
        base = median(baseline)
        if base <= 0:
            return 1.0
        return rows[-1]["volume"] / base

    def _range_pct(self, row: dict) -> float:
        return (row["high"] - row["low"]) / max(row["close"], 0.01) * 100.0

    def _return_pct(self, start_price: float, end_price: float) -> float:
        if start_price == 0:
            return 0.0
        return (end_price / start_price - 1.0) * 100.0

    def _float_from_keys(self, row: dict, *keys: str) -> float | None:
        for key in keys:
            value = row.get(key)
            if value in (None, "", "-"):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _datetime_from_keys(self, row: dict, *keys: str) -> datetime:
        for key in keys:
            value = row.get(key)
            if value in (None, ""):
                continue
            return self._parse_datetime(value)
        return datetime.now()

    def _parse_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts)
        text = str(value)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return datetime.fromisoformat(text)
