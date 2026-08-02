from __future__ import annotations

import json
from pathlib import Path

from ashare_decision_support.models.enums import Route
from ashare_decision_support.models.schemas import (
    Candidate,
    CandidateBlueprint,
    DecisionMatrix,
    MarketContext,
    MatrixBuildConfig,
    ResearchInput,
    SectorBlueprint,
)


def load_research_input(path: str | Path) -> ResearchInput:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return ResearchInput.model_validate(payload)


class ResearchPipeline:
    def __init__(self, config: MatrixBuildConfig | None = None) -> None:
        self.config = config or MatrixBuildConfig()

    def build_matrix(self, research_input: ResearchInput) -> DecisionMatrix:
        position_band_factor = self._position_band_factor(research_input.market_context)
        market_regime = self._market_regime(research_input.market_context, position_band_factor)

        selected_sectors = self._select_sectors(research_input.sectors, position_band_factor)
        allowed_sector_names = {sector.name for sector in selected_sectors}
        blacklist_map = {entry.symbol: entry.reason for entry in research_input.blacklist}

        sector_counts: dict[str, int] = {}
        candidates: list[Candidate] = []
        sorted_blueprints = sorted(
            research_input.candidates,
            key=lambda item: (item.priority_score, item.liquidity_score),
            reverse=True,
        )

        sector_by_name = {sector.name: sector for sector in selected_sectors}
        for blueprint in sorted_blueprints:
            if blueprint.symbol in blacklist_map:
                continue
            if blueprint.liquidity_score < self.config.min_liquidity_score:
                continue
            if blueprint.sector not in allowed_sector_names:
                continue
            if sector_counts.get(blueprint.sector, 0) >= self.config.max_candidates_per_sector:
                continue

            sector = sector_by_name[blueprint.sector]
            candidate = self._build_candidate(blueprint, sector, position_band_factor)
            candidates.append(candidate)
            sector_counts[blueprint.sector] = sector_counts.get(blueprint.sector, 0) + 1

        sector_whitelist = [sector.name for sector in selected_sectors if sector.route != Route.HEDGE]
        sector_hedge = [sector.name for sector in selected_sectors if sector.route == Route.HEDGE]

        return DecisionMatrix(
            trade_date=research_input.trade_date,
            strategy_version=research_input.strategy_version,
            market_regime=market_regime,
            position_band_factor=round(position_band_factor, 4),
            sector_whitelist=sector_whitelist,
            sector_hedge=sector_hedge,
            candidates=candidates,
        )

    def _position_band_factor(self, context: MarketContext) -> float:
        if context.manual_position_factor is not None:
            return context.manual_position_factor
        return min(
            self._limit_down_factor(context.limit_down_count),
            self._blowup_factor(context.blowup_rate),
            self._breadth_factor(context.breadth_score),
            context.style_decay_factor,
        )

    def _limit_down_factor(self, count: int) -> float:
        if count <= 15:
            return 1.0
        if count <= 50:
            return max(0.1, 1.0 - (count - 15) / 50.0)
        return 0.1

    def _blowup_factor(self, rate: float) -> float:
        if rate <= 0.2:
            return 1.0
        if rate <= 0.4:
            return 0.85
        if rate <= 0.55:
            return 0.65
        return 0.4

    def _breadth_factor(self, breadth_score: float) -> float:
        return max(0.1, min(1.0, breadth_score / 100.0))

    def _market_regime(self, context: MarketContext, position_band_factor: float) -> str:
        if context.market_regime_override:
            return context.market_regime_override
        if position_band_factor >= 0.8:
            return "strong"
        if position_band_factor >= 0.55:
            return "balanced"
        if position_band_factor >= 0.3:
            return "cautious"
        return "defensive"

    def _select_sectors(
        self,
        sectors: list[SectorBlueprint],
        position_band_factor: float,
    ) -> list[SectorBlueprint]:
        scored: dict[Route, list[tuple[float, SectorBlueprint]]] = {
            Route.MAINLINE: [],
            Route.PULSE: [],
            Route.HEDGE: [],
        }

        for sector in sectors:
            score = self._sector_score(sector, position_band_factor)
            if not self._sector_passes_threshold(sector.route, score):
                continue
            scored[sector.route].append((score, sector))

        return (
            self._top_by_route(scored[Route.MAINLINE], self.config.max_mainline_sectors)
            + self._top_by_route(scored[Route.PULSE], self.config.max_pulse_sectors)
            + self._top_by_route(scored[Route.HEDGE], self.config.max_hedge_sectors)
        )

    def _top_by_route(self, items: list[tuple[float, SectorBlueprint]], limit: int) -> list[SectorBlueprint]:
        ordered = sorted(items, key=lambda item: (item[0], item[1].priority), reverse=True)
        return [sector for _, sector in ordered[:limit]]

    def _sector_score(self, sector: SectorBlueprint, position_band_factor: float) -> float:
        base = sector.policy_score * sector.policy_weight + sector.anchor_score + sector.flow_score - sector.fake_tail_penalty
        return base * position_band_factor * sector.decay_factor

    def _sector_passes_threshold(self, route: Route, score: float) -> bool:
        if route == Route.MAINLINE:
            return score >= self.config.mainline_min_sector_score
        if route == Route.PULSE:
            return score >= self.config.pulse_min_sector_score
        return score >= self.config.hedge_min_sector_score

    def _build_candidate(
        self,
        blueprint: CandidateBlueprint,
        sector: SectorBlueprint,
        position_band_factor: float,
    ) -> Candidate:
        route = blueprint.route or sector.route
        stop_bps = blueprint.stop_bps if blueprint.stop_bps is not None else self._default_stop(route)
        rrr_min = blueprint.rrr_min if blueprint.rrr_min is not None else self._default_rrr(route)
        min_position_pct, max_position_pct = self._position_band(
            route=route,
            min_override=blueprint.min_position_pct,
            max_override=blueprint.max_position_pct,
            position_band_factor=position_band_factor,
        )

        return Candidate(
            symbol=blueprint.symbol,
            route=route,
            thesis=blueprint.thesis,
            stop_bps=stop_bps,
            rrr_min=rrr_min,
            min_position_pct=min_position_pct,
            max_position_pct=max_position_pct,
        )

    def _default_stop(self, route: Route) -> int:
        if route == Route.PULSE:
            return 200
        return 350

    def _default_rrr(self, route: Route) -> float:
        if route == Route.PULSE:
            return 3.0
        return 2.0

    def _position_band(
        self,
        route: Route,
        min_override: float | None,
        max_override: float | None,
        position_band_factor: float,
    ) -> tuple[float, float]:
        if min_override is not None and max_override is not None:
            return min_override, max_override

        base_min, base_max = self._default_position_band(route)
        scaled_min = round(base_min * position_band_factor, 4)
        scaled_max = round(base_max * position_band_factor, 4)
        final_min = scaled_min if min_override is None else min_override
        final_max = scaled_max if max_override is None else max_override
        return final_min, final_max

    def _default_position_band(self, route: Route) -> tuple[float, float]:
        if route == Route.PULSE:
            return self.config.pulse_min_position_pct, self.config.pulse_max_position_pct
        if route == Route.HEDGE:
            return self.config.hedge_min_position_pct, self.config.hedge_max_position_pct
        return self.config.mainline_min_position_pct, self.config.mainline_max_position_pct
