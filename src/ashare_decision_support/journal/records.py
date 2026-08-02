"""台账记录的两种形态：建议（当时）与兑现（事后）。

**对照组。** 建议分两类：`recommendation` 是真的推给你的，`control` 是同一轮
筛选里紧挨着落选的那些——不推送、不进候选池，**只记录**。没有它，台账里全是
正样本，永远回答不了「筛选规则本身有没有用」：入选组次日平均涨 1.2% 听起来
不错，但如果落选组涨 1.4%，这套规则就是负价值。两组都记，差值才有意义。

对照组和建议走**完全相同的兑现流程**，`Outcome.rec_kind` 保留它来自哪一组。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

KIND_RECOMMENDATION = "recommendation"
KIND_CONTROL = "control"
KIND_OUTCOME = "outcome"

# 会被兑现流程核对的两类记录。对照组必须在内，否则它记了也白记。
VERIFIABLE_KINDS = (KIND_RECOMMENDATION, KIND_CONTROL)


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
    # 低吸画像（`signals/dipbuy.py`）。回撤深度、缩量程度、距支撑距离——这几个数
    # 决定了「这是不是一个低吸位置」，而它们此前完全没有被记录过。
    dip: dict[str, Any] = field(default_factory=dict)
    # 建议发出当时的**大盘状态**。此前台账 24 个字段全是个股维度，一个都没记
    # 当天市场什么样，后果是三个月后有了几百条记录却**无法按情绪周期分层**——
    # 而这恰恰是超短最重要的分层维度。同一套规则在退潮期和启动期的期望值可以
    # 是反号的，混在一起统计只会得到一个谁都不像的平均数。
    market_state: dict[str, Any] = field(default_factory=dict)
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
    """建议发出后 N 个交易日里实际发生了什么。

    **两套口径并存，因为持有期本身还没有定论。** T+5 的 MFE/MAE 对应低吸的
    自然持有期（等回调结束→反弹到前高，通常 2–5 日）；`next_*` 三个数对应
    「次日就卖」。哪一种更适合这套规则，现在拍板都是猜——两个都记下来，等
    样本够了让数据回答。

    `next_open_pct` 单独拿出来是有原因的：持仓过夜的收益里，隔夜跳空往往比
    日内择时贡献大得多。把跳空和日内分开记，才能归因到底是选股选对了，还是
    单纯吃到了普涨。
    """

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
    # T+1 口径：隔夜跳空、次日最高（当天能卖到的最好价）、次日收盘
    next_open_pct: float | None = None
    next_high_pct: float | None = None
    next_low_pct: float | None = None
    next_close_pct: float | None = None
    score: float | None = None
    sector: str | None = None
    trend_state: str | None = None
    wyckoff_bias: str | None = None
    # 来自建议还是对照组。少了这个字段，两组数据混在一起就再也分不开。
    rec_kind: str = KIND_RECOMMENDATION
    # 特征与市场状态原样带过来，让一条兑现记录自带全部分层维度，统计时不必回头
    # 关联建议记录。台账是 JSONL，冗余几个字段远比 join 便宜。
    dip: dict[str, Any] = field(default_factory=dict)
    market_state: dict[str, Any] = field(default_factory=dict)
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
