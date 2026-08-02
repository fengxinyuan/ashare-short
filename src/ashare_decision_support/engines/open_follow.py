from __future__ import annotations

from ashare_decision_support.models.enums import RiskTag, SignalMode
from ashare_decision_support.models.schemas import Candidate, DecisionCard, OpenFeatures, OpenFollowConfig


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def _format_position_band(min_pct: float, max_pct: float) -> str:
    return f"{min_pct * 100:.0f}%-{max_pct * 100:.0f}%"


class OpenFollowEngine:
    def __init__(self, config: OpenFollowConfig | None = None) -> None:
        self.config = config or OpenFollowConfig()

    def analyze(
        self,
        candidate: Candidate,
        features: OpenFeatures,
        strategy_version: str,
    ) -> DecisionCard:
        if candidate.symbol != features.symbol:
            raise ValueError("candidate and feature symbol mismatch")

        risk_tags: list[RiskTag] = []
        if features.data_stale:
            risk_tags.append(RiskTag.DATA_STALE)
            return self._build_card(
                candidate,
                SignalMode.WATCH,
                confidence=0.2,
                fill_difficulty=0.6,
                expected_edge_bps=-10.0,
                risk_tags=risk_tags,
                explanation="开盘后数据已过期，仅保留观察信号。",
                source_ts=features.source_ts,
                strategy_version=strategy_version,
            )

        if features.minutes_since_open <= 5 and features.volume_ratio < self.config.fail_volume_ratio and features.price_vs_open_pct < 0:
            risk_tags.append(RiskTag.OPENING_WEAKNESS)
            return self._build_card(
                candidate,
                SignalMode.WATCH,
                confidence=0.28,
                fill_difficulty=0.45,
                expected_edge_bps=-5.0,
                risk_tags=risk_tags,
                explanation="开盘 5 分钟内量比不足且跌破开盘价，承接偏弱。",
                source_ts=features.source_ts,
                strategy_version=strategy_version,
            )

        if features.minutes_since_open > 30 and features.price_vs_vwap_pct < 0 and features.volume_ratio < self.config.vwap_exit_volume_ratio:
            risk_tags.append(RiskTag.BELOW_VWAP)
            return self._build_card(
                candidate,
                SignalMode.BLOCK,
                confidence=0.18,
                fill_difficulty=0.35,
                expected_edge_bps=-25.0,
                risk_tags=risk_tags,
                explanation="超过 30 分钟仍弱于 VWAP 且量比偏低，不建议继续跟随。",
                source_ts=features.source_ts,
                strategy_version=strategy_version,
            )

        confidence = 0.45
        if features.price_vs_open_pct >= self.config.strong_price_vs_open_pct:
            confidence += 0.15
        if features.price_vs_vwap_pct >= 0:
            confidence += 0.15
        if features.volume_ratio >= self.config.favorable_volume_ratio:
            confidence += 0.12
        confidence = _clamp(confidence, 0.05, 0.95)

        fill_difficulty = _clamp(0.25 + max(0.0, 0.9 - features.volume_ratio) * 0.4, 0.0, 1.0)
        expected_edge_bps = round(confidence * 160.0 - (1.0 - confidence) * candidate.stop_bps - fill_difficulty * 20.0, 2)

        signal_mode = SignalMode.OPEN_FOLLOW if confidence >= 0.65 else SignalMode.WATCH
        explanation = (
            "开盘后承接、量比与 VWAP 结构同步改善，适合进入开盘后重点观察。"
            if signal_mode == SignalMode.OPEN_FOLLOW
            else "开盘后结构尚可，但确定性不足，继续观察。"
        )

        return self._build_card(
            candidate,
            signal_mode=signal_mode,
            confidence=confidence,
            fill_difficulty=fill_difficulty,
            expected_edge_bps=expected_edge_bps,
            risk_tags=risk_tags,
            explanation=explanation,
            source_ts=features.source_ts,
            strategy_version=strategy_version,
        )

    def _build_card(
        self,
        candidate: Candidate,
        signal_mode: SignalMode,
        confidence: float,
        fill_difficulty: float,
        expected_edge_bps: float,
        risk_tags: list[RiskTag],
        explanation: str,
        source_ts,
        strategy_version: str,
    ) -> DecisionCard:
        if signal_mode == SignalMode.OPEN_FOLLOW:
            position_band = _format_position_band(candidate.min_position_pct * 0.5, candidate.max_position_pct * 0.8)
        elif signal_mode == SignalMode.BLOCK:
            position_band = "0%"
        else:
            position_band = "0%-5%"

        return DecisionCard(
            symbol=candidate.symbol,
            signal_mode=signal_mode,
            route=candidate.route,
            confidence=round(confidence, 4),
            fill_difficulty=round(fill_difficulty, 4),
            expected_edge_bps=expected_edge_bps,
            suggested_position_band=position_band,
            risk_tags=list(dict.fromkeys(risk_tags)),
            entry_window="09:30:00-10:00:00",
            fallback_window=None,
            explanation=explanation,
            source_ts=source_ts,
            strategy_version=strategy_version,
        )
