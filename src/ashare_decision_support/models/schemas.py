from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ashare_decision_support.models.enums import (
    EffortResult,
    ResearchTargetType,
    RiskTag,
    Route,
    SignalMode,
    TrendState,
    WyckoffBias,
)


class Candidate(BaseModel):
    symbol: str
    route: Route
    thesis: str
    stop_bps: int = Field(gt=0)
    rrr_min: float = Field(gt=0)
    min_position_pct: float = Field(ge=0, le=1, default=0.05)
    max_position_pct: float = Field(ge=0, le=1, default=0.15)
    sector: str | None = None
    priority_score: float | None = None
    liquidity_score: float | None = Field(default=None, ge=0, le=1)
    research_score: float | None = None
    conviction_score: float | None = None
    catalyst_score: float | None = None
    risk_score: float | None = None
    support_score: float | None = None
    warnings: list[str] = Field(default_factory=list)
    agent_notes: list[str] = Field(default_factory=list)

    @field_validator("max_position_pct")
    @classmethod
    def validate_position_band(cls, value: float, info) -> float:
        min_value = info.data.get("min_position_pct", 0.0)
        if value < min_value:
            raise ValueError("max_position_pct must be >= min_position_pct")
        return value


class DecisionMatrix(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    trade_date: date
    strategy_version: str
    market_regime: str
    position_band_factor: float = Field(ge=0, le=1)
    sector_whitelist: list[str] = Field(default_factory=list)
    sector_hedge: list[str] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    market_notes: list[str] = Field(default_factory=list)
    agent_summary: list[str] = Field(default_factory=list)


class NewsItem(BaseModel):
    id: str | None = None
    title: str
    content: str | None = None
    source: str | None = None
    published_at: datetime | None = None
    symbols: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    importance: float = Field(default=0.5, ge=0, le=1)


class AnnouncementItem(BaseModel):
    symbol: str
    title: str
    category: str | None = None
    summary: str | None = None
    published_at: datetime | None = None
    severity: float = Field(default=0.5, ge=0, le=1)


class CapitalFlowItem(BaseModel):
    symbol: str | None = None
    sector: str | None = None
    net_inflow: float = 0.0
    tail_inflow_ratio: float | None = Field(default=None, ge=0)
    large_order_inflow: float | None = None
    note: str | None = None


class BlacklistEntry(BaseModel):
    symbol: str
    reason: str


class MarketContext(BaseModel):
    limit_down_count: int = Field(ge=0)
    blowup_rate: float = Field(ge=0, le=1)
    breadth_score: float = Field(ge=0, le=100)
    style_decay_factor: float = Field(default=1.0, ge=0, le=1)
    manual_position_factor: float | None = Field(default=None, ge=0, le=1)
    market_regime_override: str | None = None


class SectorBlueprint(BaseModel):
    name: str
    route: Route
    policy_score: float = Field(ge=0)
    policy_weight: float = Field(default=1.0, ge=0)
    anchor_score: float = Field(default=0.0)
    flow_score: float = Field(default=0.0)
    fake_tail_penalty: float = Field(default=0.0, ge=0)
    decay_factor: float = Field(default=1.0, ge=0, le=1)
    priority: int = Field(default=0)


class CandidateBlueprint(BaseModel):
    symbol: str
    sector: str
    thesis: str
    route: Route | None = None
    stop_bps: int | None = Field(default=None, gt=0)
    rrr_min: float | None = Field(default=None, gt=0)
    min_position_pct: float | None = Field(default=None, ge=0, le=1)
    max_position_pct: float | None = Field(default=None, ge=0, le=1)
    priority_score: float = Field(default=0.0)
    liquidity_score: float = Field(default=1.0, ge=0, le=1)

    @field_validator("max_position_pct")
    @classmethod
    def validate_optional_position_band(cls, value: float | None, info) -> float | None:
        min_value = info.data.get("min_position_pct")
        if value is not None and min_value is not None and value < min_value:
            raise ValueError("max_position_pct must be >= min_position_pct")
        return value


class ResearchInput(BaseModel):
    trade_date: date
    strategy_version: str
    market_context: MarketContext
    sectors: list[SectorBlueprint] = Field(default_factory=list)
    candidates: list[CandidateBlueprint] = Field(default_factory=list)
    blacklist: list[BlacklistEntry] = Field(default_factory=list)
    news_items: list[NewsItem] = Field(default_factory=list)
    announcements: list[AnnouncementItem] = Field(default_factory=list)
    capital_flows: list[CapitalFlowItem] = Field(default_factory=list)
    note: str | None = None


class MatrixBuildConfig(BaseModel):
    mainline_min_sector_score: float = 45.0
    pulse_min_sector_score: float = 55.0
    hedge_min_sector_score: float = 35.0
    max_mainline_sectors: int = Field(default=2, ge=0)
    max_pulse_sectors: int = Field(default=1, ge=0)
    max_hedge_sectors: int = Field(default=1, ge=0)
    max_candidates_per_sector: int = Field(default=3, ge=0)
    min_liquidity_score: float = Field(default=0.4, ge=0, le=1)
    mainline_min_position_pct: float = Field(default=0.10, ge=0, le=1)
    mainline_max_position_pct: float = Field(default=0.15, ge=0, le=1)
    pulse_min_position_pct: float = Field(default=0.05, ge=0, le=1)
    pulse_max_position_pct: float = Field(default=0.10, ge=0, le=1)
    hedge_min_position_pct: float = Field(default=0.05, ge=0, le=1)
    hedge_max_position_pct: float = Field(default=0.08, ge=0, le=1)


class AuctionFeatures(BaseModel):
    symbol: str
    matched_qty: int = Field(ge=0)
    # None when the feed cannot observe auction microstructure at all. Engines
    # must treat None as "unknown", never as zero.
    matched_amount: float | None = Field(default=None, ge=0)
    unmatched_qty: int = Field(ge=0)
    unmatched_side: Literal["B", "S", "N"] = "N"
    indicative_match_price: float = Field(gt=0)
    open_gap_pct: float
    cancel_pre920_ratio: float | None = Field(default=None, ge=0, le=1)
    seal_ratio: float | None = Field(default=None, ge=0)
    tail_flow_distortion: bool = False
    data_stale: bool = False
    source_ts: datetime


class OpenFeatures(BaseModel):
    symbol: str
    minutes_since_open: int = Field(ge=0)
    price_vs_open_pct: float
    price_vs_vwap_pct: float
    volume_ratio: float = Field(ge=0)
    open_gap_pct: float
    data_stale: bool = False
    source_ts: datetime


class DecisionCard(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    symbol: str
    signal_mode: SignalMode
    route: Route
    confidence: float = Field(ge=0, le=1)
    fill_difficulty: float = Field(ge=0, le=1)
    expected_edge_bps: float
    suggested_position_band: str
    risk_tags: list[RiskTag] = Field(default_factory=list)
    entry_window: str
    fallback_window: str | None = None
    explanation: str
    source_ts: datetime
    strategy_version: str


class WyckoffSignal(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    symbol: str
    bias: WyckoffBias
    score: float = Field(ge=-100, le=100)
    close_position: float = Field(ge=0, le=1)
    short_return_pct: float
    medium_return_pct: float
    volume_ratio: float = Field(ge=0)
    compression_ratio: float = Field(ge=0, le=1)
    spring: bool = False
    upthrust: bool = False
    sign_of_strength: bool = False
    last_point_of_support: bool = False
    effort_result: EffortResult = EffortResult.NEUTRAL
    explanation: str
    source_ts: datetime


class PriceLevel(BaseModel):
    side: Literal["support", "resistance"]
    price: float = Field(gt=0)
    touches: int = Field(ge=1)
    strength: float = Field(ge=0)
    distance_pct: float
    last_touch_ts: datetime | None = None
    label: str


class DailyTechnicalSnapshot(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    symbol: str
    bar_count: int = Field(ge=0)
    last_close: float = Field(gt=0)
    last_open: float = Field(gt=0)
    last_high: float = Field(gt=0)
    last_low: float = Field(gt=0)
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    atr14: float | None = None
    volume_ratio_20d: float | None = Field(default=None, ge=0)
    range_position_60d: float | None = Field(default=None, ge=0, le=1)
    trend_state: TrendState
    supports: list[PriceLevel] = Field(default_factory=list)
    resistances: list[PriceLevel] = Field(default_factory=list)
    stop_reference: float | None = None
    target_reference: float | None = None
    chart_bars: list[dict] = Field(default_factory=list)
    setups: list[dict] = Field(default_factory=list)
    explanation: str
    source_ts: datetime
