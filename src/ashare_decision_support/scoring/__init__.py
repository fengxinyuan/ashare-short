"""Pluggable candidate scoring providers."""

from ashare_decision_support.scoring.base import ScoreInput, ScoreProvider, ScoreResult
from ashare_decision_support.scoring.context import (
    CatalystReading,
    SectorFactorContext,
    StockFactorContext,
    build_factor_contexts,
    read_catalysts,
)
from ashare_decision_support.scoring.rule_based import RuleBasedScoreProvider

__all__ = [
    "CatalystReading",
    "RuleBasedScoreProvider",
    "ScoreInput",
    "ScoreProvider",
    "ScoreResult",
    "SectorFactorContext",
    "StockFactorContext",
    "build_factor_contexts",
    "read_catalysts",
]
