"""Pluggable candidate scoring providers."""

from ashare_decision_support.scoring.base import ScoreInput, ScoreProvider, ScoreResult
from ashare_decision_support.scoring.rule_based import RuleBasedScoreProvider

__all__ = ["RuleBasedScoreProvider", "ScoreInput", "ScoreProvider", "ScoreResult"]
