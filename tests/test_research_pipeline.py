from datetime import date

from ashare_decision_support.models.enums import Route
from ashare_decision_support.models.schemas import (
    BlacklistEntry,
    CandidateBlueprint,
    MarketContext,
    ResearchInput,
    SectorBlueprint,
)
from ashare_decision_support.research import ResearchPipeline


def build_research_input() -> ResearchInput:
    return ResearchInput(
        trade_date=date(2026, 7, 31),
        strategy_version="v1.0",
        market_context=MarketContext(
            limit_down_count=20,
            blowup_rate=0.25,
            breadth_score=70,
            style_decay_factor=0.8,
        ),
        sectors=[
            SectorBlueprint(
                name="AI算力",
                route=Route.MAINLINE,
                policy_score=22,
                policy_weight=1.5,
                anchor_score=24,
                flow_score=20,
                priority=10,
            ),
            SectorBlueprint(
                name="消费脉冲",
                route=Route.PULSE,
                policy_score=24,
                policy_weight=1.0,
                anchor_score=28,
                flow_score=30,
                fake_tail_penalty=2,
                priority=9,
            ),
            SectorBlueprint(
                name="尾盘伪放量题材",
                route=Route.PULSE,
                policy_score=15,
                policy_weight=1.0,
                anchor_score=16,
                flow_score=14,
                fake_tail_penalty=18,
                decay_factor=0.7,
                priority=2,
            ),
        ],
        candidates=[
            CandidateBlueprint(
                symbol="300001.SZ",
                sector="AI算力",
                thesis="主线分歧转强",
                priority_score=90,
                liquidity_score=0.9,
            ),
            CandidateBlueprint(
                symbol="600001.SH",
                sector="消费脉冲",
                thesis="脉冲高开验证",
                priority_score=86,
                liquidity_score=0.73,
            ),
            CandidateBlueprint(
                symbol="300999.SZ",
                sector="AI算力",
                thesis="减持风险",
                priority_score=91,
                liquidity_score=0.9,
            ),
            CandidateBlueprint(
                symbol="301188.SZ",
                sector="AI算力",
                thesis="流动性不足",
                priority_score=80,
                liquidity_score=0.2,
            ),
            CandidateBlueprint(
                symbol="002999.SZ",
                sector="尾盘伪放量题材",
                thesis="题材质量不足",
                priority_score=77,
                liquidity_score=0.68,
            ),
        ],
        blacklist=[BlacklistEntry(symbol="300999.SZ", reason="减持窗口")],
    )


def test_research_pipeline_builds_filtered_matrix() -> None:
    matrix = ResearchPipeline().build_matrix(build_research_input())
    symbols = [candidate.symbol for candidate in matrix.candidates]

    assert matrix.market_regime == "balanced"
    assert round(matrix.position_band_factor, 4) == 0.7
    assert "AI算力" in matrix.sector_whitelist
    assert "消费脉冲" in matrix.sector_whitelist
    assert "尾盘伪放量题材" not in matrix.sector_whitelist
    assert "300999.SZ" not in symbols
    assert "301188.SZ" not in symbols
    assert "002999.SZ" not in symbols
    assert symbols == ["300001.SZ", "600001.SH"]


def test_research_pipeline_applies_route_defaults_and_scaling() -> None:
    matrix = ResearchPipeline().build_matrix(build_research_input())
    mainline = next(candidate for candidate in matrix.candidates if candidate.symbol == "300001.SZ")
    pulse = next(candidate for candidate in matrix.candidates if candidate.symbol == "600001.SH")

    assert mainline.route == Route.MAINLINE
    assert mainline.stop_bps == 350
    assert mainline.rrr_min == 2.0
    assert mainline.min_position_pct == 0.07
    assert mainline.max_position_pct == 0.105

    assert pulse.route == Route.PULSE
    assert pulse.stop_bps == 200
    assert pulse.rrr_min == 3.0
    assert pulse.min_position_pct == 0.035
    assert pulse.max_position_pct == 0.07
