"""台账记录的两种形态：建议（当时）与兑现（事后）。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

KIND_RECOMMENDATION = "recommendation"
KIND_OUTCOME = "outcome"


@dataclass
class Recommendation:
    """一条建议在发出当时的完整状态。

    价位存两份：`*_price` 是当时报出的不复权价，给人看；`*_pct` 是相对参考价的
    百分比，给核对用。核对只读百分比——见模块 docstring。
    """

    symbol: str
    trade_date: str
    source: str  # morning | advise | monitor
    name: str | None = None
    sector: str | None = None
    score: float | None = None
    reference_price: float | None = None
    entry_low_price: float | None = None
    entry_high_price: float | None = None
    stop_price: float | None = None
    target_prices: list[float] = field(default_factory=list)
    entry_low_pct: float | None = None
    entry_high_pct: float | None = None
    stop_pct: float | None = None
    target_pcts: list[float] = field(default_factory=list)
    rrr: float | None = None
    trend_state: str | None = None
    wyckoff_bias: str | None = None
    # 建议发出当时的个股舆情读数。存下来是为了将来能回答一个现在答不上来的问题：
    # 带正面舆情的建议兑现率是不是真的更高。样本够之前不参与任何加权。
    news_polarity: float | None = None
    news_intensity: float | None = None
    news_doc_count: int | None = None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    facts_hash: str | None = None
    kind: str = KIND_RECOMMENDATION

    @classmethod
    def from_advice(
        cls,
        advice: Any,
        trade_date: date,
        source: str,
        sector: str | None = None,
        facts: Any = None,
    ) -> "Recommendation | None":
        """建。参考价缺失或为零时返回 None——没有参考价就算不出相对值，
        存一条无法核对的记录只会污染统计。"""
        reference = advice.last_price
        if not reference:
            return None

        def pct(value: float | None) -> float | None:
            return None if value is None else round(value / reference - 1.0, 6)

        return cls(
            symbol=advice.symbol,
            trade_date=str(trade_date),
            source=source,
            name=advice.name,
            sector=sector,
            score=advice.score,
            reference_price=reference,
            entry_low_price=advice.entry_low,
            entry_high_price=advice.entry_high,
            stop_price=advice.stop,
            target_prices=list(advice.targets),
            entry_low_pct=pct(advice.entry_low),
            entry_high_pct=pct(advice.entry_high),
            stop_pct=pct(advice.stop),
            target_pcts=[pct(level) for level in advice.targets if pct(level) is not None],
            rrr=advice.rrr,
            trend_state=advice.trend_state,
            wyckoff_bias=advice.wyckoff_bias,
            reasons=list(advice.reasons),
            warnings=list(advice.warnings),
            facts_hash=facts_hash(facts) if facts is not None else None,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.trade_date}|{self.source}"


@dataclass
class Outcome:
    """建议发出后 N 个交易日里实际发生了什么。"""

    symbol: str
    trade_date: str
    source: str
    resolved_on: str
    sessions: int
    entered: bool
    stop_hit: bool
    target_hit: bool
    first_hit: str | None  # stop | target | None
    mfe_pct: float | None  # 最大有利偏移
    mae_pct: float | None  # 最大不利偏移
    close_pct: float | None  # 期末收益
    score: float | None = None
    sector: str | None = None
    trend_state: str | None = None
    wyckoff_bias: str | None = None
    note: str | None = None
    kind: str = KIND_OUTCOME

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.trade_date}|{self.source}"


def facts_hash(facts: Any) -> str:
    """输入事实的指纹，让「当时到底看到了什么」可复现。"""
    payload = json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
