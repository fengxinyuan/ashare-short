"""Lexicon-based Chinese sentiment scoring.

Why a lexicon and not a model: the output has to be *explainable at 08:30 in
the morning*. When the brief says a sector is hot, you need to see which words
in which headlines caused that, otherwise the number is unauditable. A lexicon
gives you the hit list for free.

This is a heuristic. Treat the polarity as an ordering aid between sectors on
the same day, not as a cross-day calibrated measure.
"""

from __future__ import annotations

from ashare_decision_support.news.base import SentimentAnalyzer, SentimentScore
from ashare_decision_support.news.lexicon import (
    AMPLIFIERS,
    BEARISH_TERMS,
    BULLISH_TERMS,
    NEGATION_WINDOW,
    NEGATIONS,
    NOISE_TERMS,
)

# Hit weight at which a single document is considered fully saturated. Above
# this, extra matched words stop increasing the magnitude — a headline that
# stacks six bullish words is not three times more informative than one with
# two, it is usually just promotional copy.
SATURATION_WEIGHT = 6.0


class LexiconSentiment(SentimentAnalyzer):
    name = "lexicon"

    def __init__(
        self,
        bullish: dict[str, float] | None = None,
        bearish: dict[str, float] | None = None,
        drop_noise: bool = True,
    ) -> None:
        self.bullish = dict(bullish if bullish is not None else BULLISH_TERMS)
        self.bearish = dict(bearish if bearish is not None else BEARISH_TERMS)
        self.drop_noise = drop_noise
        # Longest first so "限售股解禁" is consumed before the shorter "解禁"
        # can match inside it and double-count the same span.
        self._ordered = sorted(
            [(term, weight, 1.0) for term, weight in self.bullish.items()]
            + [(term, weight, -1.0) for term, weight in self.bearish.items()],
            key=lambda entry: len(entry[0]),
            reverse=True,
        )

    def score_text(self, text: str) -> SentimentScore:
        if not text:
            return SentimentScore(polarity=0.0, intensity=0.0)
        if self.drop_noise and any(noise in text for noise in NOISE_TERMS):
            return SentimentScore(polarity=0.0, intensity=0.0)

        consumed = [False] * len(text)
        bullish_weight = 0.0
        bearish_weight = 0.0
        bullish_hits: list[str] = []
        bearish_hits: list[str] = []

        for term, weight, sign in self._ordered:
            start = text.find(term)
            while start != -1:
                end = start + len(term)
                if any(consumed[start:end]):
                    start = text.find(term, start + 1)
                    continue
                for index in range(start, end):
                    consumed[index] = True

                effective = weight * self._amplifier(text, start)
                effective_sign = sign
                if self._negated(text, start):
                    effective_sign = -sign

                if effective_sign > 0:
                    bullish_weight += effective
                    bullish_hits.append(term)
                else:
                    bearish_weight += effective
                    bearish_hits.append(term)
                start = text.find(term, end)

        total = bullish_weight + bearish_weight
        if total <= 0:
            return SentimentScore(polarity=0.0, intensity=0.0)

        polarity = (bullish_weight - bearish_weight) / total
        intensity = min(1.0, total / SATURATION_WEIGHT)
        return SentimentScore(
            polarity=round(max(-1.0, min(1.0, polarity)), 4),
            intensity=round(intensity, 4),
            bullish_hits=tuple(dict.fromkeys(bullish_hits)),
            bearish_hits=tuple(dict.fromkeys(bearish_hits)),
        )

    def _amplifier(self, text: str, start: int) -> float:
        """Look just behind the match for a qualifier like 大幅 / 重大."""
        window = text[max(0, start - 4) : start]
        factor = 1.0
        for term, boost in AMPLIFIERS.items():
            if term in window:
                factor = max(factor, boost)
        return factor

    def _negated(self, text: str, start: int) -> bool:
        window = text[max(0, start - NEGATION_WINDOW) : start]
        return any(negation in window for negation in NEGATIONS)
