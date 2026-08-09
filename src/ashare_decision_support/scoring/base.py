"""Pluggable candidate scoring.

The scoring layer is deliberately thin: it turns per-symbol daily research
artifacts into a single comparable number plus human-readable reasons. The
rule-based provider is the default so the pipeline runs without a trained
model; an alpha-factor / LightGBM provider can be dropped in later by
implementing the same `ScoreProvider` interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.models.schemas import DailyTechnicalSnapshot, WyckoffSignal
from ashare_decision_support.news.base import SymbolSentiment
from ashare_decision_support.scoring.context import StockFactorContext
from ashare_decision_support.signals.dipbuy import DipFeatures


@dataclass(frozen=True)
class ScoreInput:
    """Everything a provider may look at for one symbol.

    `bars` is kept alongside the derived artifacts so a model-based provider can
    compute its own features instead of being limited to the rule-based ones.

    `news` is optional and defaults to None, which means "not collected" rather
    than "collected and neutral" — the rule provider contributes nothing in that
    case instead of reading absence as a negative.

    `dip` carries the pullback picture (`signals/dipbuy.py`). Same convention:
    None means "not computed", and the provider scores nothing rather than
    treating a missing pullback as "no pullback".
    """

    symbol: str
    bars: list[dict]
    snapshot: DailyTechnicalSnapshot
    wyckoff: WyckoffSignal | None = None
    news: SymbolSentiment | None = None
    dip: DipFeatures | None = None
    volume_price: Any | None = None
    context: StockFactorContext | None = None


@dataclass(frozen=True)
class ScoreResult:
    symbol: str
    score: float
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)


class ScoreProvider(ABC):
    """Scores a candidate on a 0-100 scale, higher is more attractive."""

    name: str = "base"

    @abstractmethod
    def score(self, item: ScoreInput) -> ScoreResult:
        """Score a single symbol."""

    def score_many(self, items: list[ScoreInput]) -> list[ScoreResult]:
        """Score every symbol, best first. Providers may override for batching."""
        results = [self.score(item) for item in items]
        return sorted(results, key=lambda result: result.score, reverse=True)
