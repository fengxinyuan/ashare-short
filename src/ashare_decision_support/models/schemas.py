from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ashare_decision_support.models.enums import (
    EffortResult,
    TrendState,
    WyckoffBias,
)


class AuctionFeatures(BaseModel):
    """集合竞价读数。**这是适配器协议的一部分，不是策略层的输入。**

    `adapters/base.py` 的 `get_auction_features` 与五个适配器实现都按它出参。策略
    层目前不消费它——QMT 补不上撮合量与未匹配量（见 `docs/DECISIONS.md`），所以
    竞价那条线还没建起来。留着是因为归一层（`adapters/mapping.py`）已经在用它当
    券商字段的落点。
    """

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
    """开盘后前若干分钟的读数。与 `AuctionFeatures` 同属适配器协议。"""

    symbol: str
    minutes_since_open: int = Field(ge=0)
    price_vs_open_pct: float
    price_vs_vwap_pct: float
    volume_ratio: float = Field(ge=0)
    open_gap_pct: float
    data_stale: bool = False
    source_ts: datetime


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
