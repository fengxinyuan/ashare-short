"""Rule-based scoring baseline.

This exists so the daily pipeline produces a ranked list before any model is
trained. The weights are hand-set heuristics, not fitted parameters — treat the
output as an ordering aid, not as a calibrated probability. Replace this with an
alpha-factor / LightGBM provider once you have one.
"""

from __future__ import annotations

from ashare_decision_support.models.enums import TrendState, WyckoffBias
from ashare_decision_support.models.schemas import DailyTechnicalSnapshot, WyckoffSignal
from ashare_decision_support.news.base import SymbolSentiment
from ashare_decision_support.scoring.base import ScoreInput, ScoreProvider, ScoreResult

TREND_WEIGHTS: dict[TrendState, float] = {
    TrendState.UPTREND: 12.0,
    TrendState.REBOUND: 6.0,
    TrendState.RANGE: 0.0,
    TrendState.DOWNTREND: -15.0,
}

BIAS_WEIGHTS: dict[WyckoffBias, float] = {
    WyckoffBias.MARKUP: 12.0,
    WyckoffBias.ACCUMULATION: 10.0,
    WyckoffBias.NEUTRAL: 0.0,
    WyckoffBias.DISTRIBUTION: -12.0,
    WyckoffBias.MARKDOWN: -15.0,
}

MIN_BARS_FOR_CONFIDENCE = 60

# Ceiling on what per-stock news may contribute, in either direction. Kept small
# on purpose: the reading is keyword matching over headlines, it mis-attributes,
# and it must never outweigh what the tape actually did. Same discipline as the
# ±12 sector-sentiment cap in `sectors/predict.py`, scaled to this component
# budget. Its job is to break ties between names the price data ranks equally,
# not to promote a name the price data does not support.
NEWS_CAP = 6.0

# Below this the reading is one weak keyword in one stale headline. Reporting it
# as a reason makes the brief noisier without changing any ordering.
NEWS_MIN_POINTS = 1.0

# Best and worst reachable component totals, excluding the base. Kept explicit
# rather than derived so a weight change that breaks the mapping shows up as a
# failing test instead of as silently squashed scores.
#
#   trend        +12 / -15
#   wyckoff      +12 bias +10 structure +6 spring +5 SOS +4 LPS   /  -15 -10 -8
#   volume        +8 /  -5
#   range         +5 /  -6
#   risk-reward   +8 /  -8
#   news          +6 /  -6
MAX_COMPONENT_TOTAL = 12.0 + 37.0 + 8.0 + 5.0 + 8.0 + NEWS_CAP
MIN_COMPONENT_TOTAL = -15.0 - 33.0 - 5.0 - 6.0 - 8.0 - NEWS_CAP


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


class RuleBasedScoreProvider(ScoreProvider):
    name = "rule-based"

    def __init__(self, base_score: float = 50.0) -> None:
        self.base_score = base_score

    def score(self, item: ScoreInput) -> ScoreResult:
        reasons: list[str] = []
        warnings: list[str] = []
        raw = 0.0

        raw += self._score_trend(item.snapshot, reasons)
        raw += self._score_wyckoff(item.wyckoff, reasons)
        raw += self._score_volume(item.snapshot, reasons, warnings)
        raw += self._score_range_position(item.snapshot, reasons, warnings)
        raw += self._score_risk_reward(item.snapshot, reasons, warnings)
        raw += self._score_news(item.news, reasons, warnings)

        if item.snapshot.bar_count < MIN_BARS_FOR_CONFIDENCE:
            warnings.append(f"历史样本仅 {item.snapshot.bar_count} 根，结构判断置信度偏低")

        return ScoreResult(
            symbol=item.symbol,
            score=round(self._to_scale(raw), 2),
            reasons=reasons,
            warnings=warnings,
        )

    def _to_scale(self, raw: float) -> float:
        """Map the component total onto 0-100 without clipping the top.

        Adding the components to a base of 50 and clamping at 100 reaches the
        ceiling at +50, but the components can total +70 — so every reasonably
        good name lands on exactly 100.0 and the ranking stops discriminating
        precisely among the candidates it is being asked to choose between.
        Scaling by the reachable range keeps 50 as neutral and leaves the top
        of the scale for a name that actually earns it.
        """
        span = MAX_COMPONENT_TOTAL if raw >= 0 else abs(MIN_COMPONENT_TOTAL)
        headroom = (100.0 - self.base_score) if raw >= 0 else self.base_score
        return _clamp(self.base_score + raw / span * headroom, 0.0, 100.0)

    def _score_trend(self, snapshot: DailyTechnicalSnapshot, reasons: list[str]) -> float:
        weight = TREND_WEIGHTS.get(snapshot.trend_state, 0.0)
        if weight:
            reasons.append(f"日线趋势 {snapshot.trend_state.value}（{weight:+.0f}）")
        return weight

    def _score_wyckoff(self, wyckoff: WyckoffSignal | None, reasons: list[str]) -> float:
        if wyckoff is None:
            return 0.0

        total = BIAS_WEIGHTS.get(wyckoff.bias, 0.0)
        if total:
            reasons.append(f"威科夫阶段 {wyckoff.bias.value}（{total:+.0f}）")

        # WyckoffSignal.score is already bounded to [-100, 100].
        structure = wyckoff.score * 0.1
        if abs(structure) >= 1.0:
            reasons.append(f"结构评分 {wyckoff.score:.0f}（{structure:+.1f}）")
        total += structure

        for flag, delta, label in (
            (wyckoff.spring, 6.0, "Spring 弹簧"),
            (wyckoff.sign_of_strength, 5.0, "SOS 强势信号"),
            (wyckoff.last_point_of_support, 4.0, "LPS 支撑确认"),
            (wyckoff.upthrust, -8.0, "Upthrust 假突破"),
        ):
            if flag:
                total += delta
                reasons.append(f"{label}（{delta:+.0f}）")
        return total

    def _score_volume(
        self,
        snapshot: DailyTechnicalSnapshot,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        ratio = snapshot.volume_ratio_20d
        if ratio is None:
            return 0.0
        if ratio > 4.0:
            warnings.append(f"量比 {ratio:.2f} 异常放大，注意情绪透支")
            reasons.append(f"量能过热 {ratio:.2f}（-5）")
            return -5.0
        if ratio >= 1.0:
            reasons.append(f"量能温和放大 {ratio:.2f}（+8）")
            return 8.0
        if ratio < 0.6:
            reasons.append(f"量能萎缩 {ratio:.2f}（-5）")
            return -5.0
        return 0.0

    def _score_range_position(
        self,
        snapshot: DailyTechnicalSnapshot,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        position = snapshot.range_position_60d
        if position is None:
            return 0.0
        if position > 0.95:
            warnings.append("已贴近 60 日高点，追高风险偏大")
            reasons.append(f"60日分位 {position:.2f}（-6）")
            return -6.0
        if 0.3 <= position <= 0.75:
            reasons.append(f"60日分位 {position:.2f}，上方有空间（+5）")
            return 5.0
        if position < 0.1:
            reasons.append(f"60日分位 {position:.2f}，处于弱势区（-3）")
            return -3.0
        return 0.0

    def _score_risk_reward(
        self,
        snapshot: DailyTechnicalSnapshot,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        close = snapshot.last_close
        stop = snapshot.stop_reference
        target = snapshot.target_reference
        if not stop or not target or close <= 0:
            warnings.append("缺少有效支撑或压力位，无法评估盈亏比")
            return 0.0

        downside = 1.0 - stop / close
        upside = target / close - 1.0
        if downside <= 0 or upside <= 0:
            warnings.append("当前价已跌破支撑或站上压力，盈亏比参考失效")
            return 0.0

        rrr = upside / downside
        if rrr >= 2.0:
            reasons.append(f"盈亏比 {rrr:.2f}（+8）")
            return 8.0
        if rrr >= 1.5:
            reasons.append(f"盈亏比 {rrr:.2f}（+4）")
            return 4.0
        if rrr < 0.8:
            warnings.append(f"盈亏比仅 {rrr:.2f}，风险收益不划算")
            reasons.append(f"盈亏比 {rrr:.2f}（-8）")
            return -8.0
        return 0.0

    def _score_news(
        self,
        news: SymbolSentiment | None,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        """Per-stock news, capped at ±NEWS_CAP and multiplied by its own intensity.

        `intensity` is the confidence dial: one matched keyword in one headline
        gives a small number even when the polarity is ±1.0, so a single
        loosely-worded item cannot move the ranking on its own.
        """
        if news is None or news.is_neutral:
            return 0.0

        points = _clamp(news.polarity * news.intensity * NEWS_CAP * 2.0, -NEWS_CAP, NEWS_CAP)
        if abs(points) < NEWS_MIN_POINTS:
            return 0.0

        hits = news.bullish_hits if points > 0 else news.bearish_hits
        label = "偏多" if points > 0 else "偏空"
        detail = f"个股舆情 {news.doc_count} 条{label}（{points:+.1f}，关键词：{'、'.join(hits[:4])}）"
        reasons.append(detail)
        if points <= -NEWS_CAP / 2:
            # Loud enough to survive into the pre-open card, where warnings are
            # what actually gets read at decision time.
            warnings.append(f"个股舆情偏空：{'、'.join(hits[:4])}")
        return points
