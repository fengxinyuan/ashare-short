"""Placeholder for an alpha-factor / LightGBM scoring provider.

Not implemented: there is no trained model in this repository yet. This module
only fixes the integration shape so wiring a model in later is additive.

To implement:

1. Build a feature frame from `ScoreInput.bars` (momentum, volatility, volume,
   turnover, plus whatever alpha factors you maintain). Compute features from
   `bars` only — never from anything dated after the decision timestamp, or the
   backtest leaks.
2. Load the booster once in `__init__` (`lightgbm.Booster(model_file=...)`) and
   reuse it; loading per call dominates runtime.
3. Map the raw model output onto the same 0-100 scale the rule-based provider
   uses, so `post-close-brief` rankings stay comparable across providers.
4. Populate `ScoreResult.reasons` from feature attributions (e.g. SHAP) so the
   brief stays explainable.

Note that daily bars are fetched forward-adjusted by default (see
`TickFlowAdapter.adjust`). Train and infer on the same adjustment mode.
"""

from __future__ import annotations

from ashare_decision_support.scoring.base import ScoreInput, ScoreProvider, ScoreResult


class LightGbmScoreProvider(ScoreProvider):
    name = "lightgbm"

    def __init__(self, model_path: str | None = None) -> None:
        self.model_path = model_path

    def score(self, item: ScoreInput) -> ScoreResult:
        raise NotImplementedError(
            "LightGbmScoreProvider is a placeholder. Train a model first, then implement "
            "feature extraction and inference here; see this module's docstring."
        )
