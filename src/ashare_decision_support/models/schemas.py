from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ashare_decision_support.models.enums import (
    EffortResult,
    TrendState,
    WyckoffBias,
)


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
