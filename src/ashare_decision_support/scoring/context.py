"""Cross-layer factor context used by the candidate scorer.

The technical score answers "what did this symbol's tape do?".  This module
adds the missing relationship layer: "is the symbol moving with a supported
sector, and is there a current fundamental, flow, or event reason?"

All fields are optional.  Missing data means "unknown", not negative evidence.
That rule is important because the news, earnings, and capital-flow feeds are
independent and can fail on different runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from ashare_decision_support.news.lexicon import RISK_TERMS, SUPPLY_TERMS
from ashare_decision_support.numeric import finite
from ashare_decision_support.symbols import normalize_symbol

POLICY_TERMS = (
    "政策",
    "规划",
    "专项",
    "支持",
    "补贴",
    "以旧换新",
    "人工智能+",
    "国产替代",
    "设备更新",
    "扩内需",
    "产业链",
)
PRICE_TERMS = (
    "涨价",
    "提价",
    "价格上调",
    "报价上调",
    "价格上涨",
    "纸价",
    "浆价",
    "库存拐点",
    "供给收缩",
    "产能受限",
    "库存下降",
    "供不应求",
)
# 催化事件词表：比 `lexicon.EVENT_TERMS` 窄，只留**能在两只技术形态相近的票之间
# 打破平局**的硬事件。名字带 CATALYST_ 前缀是为了不和那张表重名——它们是两件事，
# 曾经同名同姓地各存一份，读代码的人 grep 一个名字会拿到两个答案。
CATALYST_EVENT_TERMS = (
    "中标",
    "订单",
    "签约",
    "获批",
    "回购",
    "增持",
    "扩产",
    "投产",
    "突破",
    "并购",
)


@dataclass(frozen=True)
class CatalystReading:
    """Keyword evidence for policy, price, event, and risk headlines."""

    policy_score: float = 0.0
    price_score: float = 0.0
    event_score: float = 0.0
    supply_score: float = 0.0
    risk_score: float = 0.0
    hits: tuple[str, ...] = ()
    risk_hits: tuple[str, ...] = ()
    risk_types: tuple[str, ...] = ()

    @property
    def net_score(self) -> float:
        return (
            self.policy_score
            + self.price_score
            + self.event_score
            + self.supply_score
            + self.risk_score
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"net_score": round(self.net_score, 3)}


@dataclass(frozen=True)
class SectorFactorContext:
    """A sector snapshot attached to every constituent candidate."""

    name: str
    score: float | None = None
    trusted: bool = True
    ret_1d: float | None = None
    ret_5d: float | None = None
    ret_20d: float | None = None
    breadth: float | None = None
    volume_ratio: float | None = None
    above_ma20: float | None = None
    net_inflow: float | None = None
    flow_norm: float | None = None
    sentiment_polarity: float | None = None
    sentiment_intensity: float | None = None
    headlines: tuple[str, ...] = ()
    news_event_types: tuple[str, ...] = ()
    news_hits: tuple[str, ...] = ()
    catalyst: CatalystReading = field(default_factory=CatalystReading)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "trusted": self.trusted,
            "ret_1d": self.ret_1d,
            "ret_5d": self.ret_5d,
            "ret_20d": self.ret_20d,
            "breadth": self.breadth,
            "volume_ratio": self.volume_ratio,
            "above_ma20": self.above_ma20,
            "net_inflow": self.net_inflow,
            "flow_norm": self.flow_norm,
            "sentiment_polarity": self.sentiment_polarity,
            "sentiment_intensity": self.sentiment_intensity,
            "headlines": list(self.headlines),
            "news_event_types": list(self.news_event_types),
            "news_hits": list(self.news_hits),
            "catalyst": self.catalyst.to_dict(),
        }


@dataclass(frozen=True)
class StockFactorContext:
    """Per-symbol flow, sector, and fundamental context."""

    sector: SectorFactorContext | None = None
    net_inflow: float | None = None
    large_net_inflow: float | None = None
    flow_norm: float | None = None
    flow_ratio: float | None = None
    amount: float | None = None
    turnover_rate: float | None = None
    earnings: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        earnings = self.earnings
        return {
            "sector": self.sector.to_dict() if self.sector is not None else None,
            "net_inflow": self.net_inflow,
            "large_net_inflow": self.large_net_inflow,
            "flow_norm": self.flow_norm,
            "flow_ratio": self.flow_ratio,
            "amount": self.amount,
            "turnover_rate": self.turnover_rate,
            "earnings": earnings.to_dict() if hasattr(earnings, "to_dict") else None,
        }


def read_catalysts(
    texts: Iterable[str] | None,
    *,
    sector: bool = False,
) -> CatalystReading:
    """Turn related headlines into bounded event evidence.

    This is intentionally separate from general sentiment.  A headline can be
    positive without containing a tradable catalyst, while "涨价", "中标", or
    "政策支持" is the kind of information that should break ties between two
    technically similar names.
    """

    text = " ".join(str(item) for item in (texts or []) if item)
    if not text:
        return CatalystReading()

    def hits(terms: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(term for term in terms if term in text)

    policy = hits(POLICY_TERMS)
    price = hits(PRICE_TERMS)
    event = hits(CATALYST_EVENT_TERMS)
    supply = hits(SUPPLY_TERMS) if sector else ()
    # 「停产 / 停机」在个股口径下是经营中断，在板块口径下是供给收缩——同一个词
    # 方向相反。按板块看时把它从风险侧减掉，否则一条利好会同时加供给分、扣风险分。
    risk_matched = [
        (category, term)
        for category, terms in RISK_TERMS.items()
        for term in terms
        if term in text and not (sector and term in SUPPLY_TERMS)
    ]
    risk = tuple(dict.fromkeys(term for _, term in risk_matched))
    risk_types = tuple(dict.fromkeys(category for category, _ in risk_matched))
    return CatalystReading(
        policy_score=min(2.0, len(policy) * 0.8),
        price_score=min(2.5, len(price) * 1.0),
        event_score=min(2.5, len(event) * 0.8),
        supply_score=min(2.5, len(supply) * 0.8),
        # 按**类**扣分而不是按词：「关于公司股票被实施退市风险警示的公告」一句里
        # 有「退市」和「风险警示」两个词，说的是同一件事，罚一次。
        risk_score=max(-3.5, -len(risk_types) * 1.2),
        hits=tuple(dict.fromkeys((*policy, *price, *event, *supply))),
        risk_hits=risk,
        risk_types=risk_types,
    )


def build_factor_contexts(
    sector_outlooks: Iterable[Any] | None,
    sector_of: Mapping[str, str] | None = None,
    *,
    net_inflow_by_symbol: Mapping[str, float] | None = None,
    market_scan: Any | None = None,
    earnings_by_symbol: Mapping[str, Any] | None = None,
    trade_flow_by_symbol: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, StockFactorContext]:
    """Build normalized contexts for all known sector constituents.

    The normalization is cross-sectional: a raw inflow is not comparable
    across a 20 billion yuan stock and a 2 billion yuan stock.  `flow_norm`
    therefore describes the symbol's position relative to the largest absolute
    flow in this candidate universe, while `flow_ratio` compares flow with the
    symbol's own turnover when available.
    """

    outlooks = list(sector_outlooks or [])
    sector_map = {
        str(item.name): _sector_context(item, _max_sector_flow(outlooks)) for item in outlooks
    }
    symbol_sectors = {
        normalize_symbol(symbol): sector for symbol, sector in (sector_of or {}).items()
    }
    inflows = _normalise_numeric_map(net_inflow_by_symbol)
    trade_flows = {
        normalize_symbol(symbol): dict(value)
        for symbol, value in (trade_flow_by_symbol or {}).items()
        if isinstance(value, Mapping)
    }
    earnings = {
        normalize_symbol(symbol): value for symbol, value in (earnings_by_symbol or {}).items()
    }

    merged_flows: dict[str, float] = dict(inflows)
    large_flows: dict[str, float] = {}
    for symbol, flow in trade_flows.items():
        net = finite(flow.get("net_amount"))
        large = finite(flow.get("large_net_amount"))
        if net is not None:
            merged_flows[symbol] = net
        if large is not None:
            large_flows[symbol] = large
    max_flow = max((abs(value) for value in merged_flows.values()), default=0.0)

    result: dict[str, StockFactorContext] = {}
    known_symbols = set(symbol_sectors) | set(merged_flows) | set(large_flows) | set(earnings)
    for symbol in known_symbols:
        sector_name = symbol_sectors.get(symbol, "")
        row = market_scan.get(symbol) if market_scan is not None else None
        amount = finite(getattr(row, "amount", None))
        turnover = finite(getattr(row, "turnover_ratio", None))
        flow = merged_flows.get(symbol)
        flow_ratio = flow / amount if flow is not None and amount and amount > 0 else None
        result[symbol] = StockFactorContext(
            sector=sector_map.get(sector_name) if sector_name else None,
            net_inflow=flow,
            large_net_inflow=large_flows.get(symbol),
            flow_norm=(flow / max_flow if max_flow > 0 and flow is not None else None),
            flow_ratio=flow_ratio,
            amount=amount,
            turnover_rate=(turnover / 100.0 if turnover is not None else None),
            earnings=earnings.get(symbol),
        )
    return result


def _sector_context(outlook: Any, max_flow: float) -> SectorFactorContext:
    metrics = outlook.metrics
    flow = finite(getattr(outlook, "net_inflow", None))
    headlines = tuple(getattr(outlook, "headlines", ()) or ())
    news_hits = tuple(getattr(outlook, "news_hits", ()) or ())
    return SectorFactorContext(
        name=str(outlook.name),
        score=finite(getattr(outlook, "score", None)),
        trusted=bool(getattr(outlook, "trusted", True)),
        ret_1d=finite(getattr(metrics, "ret_1d", None)),
        ret_5d=finite(getattr(metrics, "ret_5d", None)),
        ret_20d=finite(getattr(metrics, "ret_20d", None)),
        breadth=finite(getattr(metrics, "breadth", None)),
        volume_ratio=finite(getattr(metrics, "volume_ratio", None)),
        above_ma20=finite(getattr(metrics, "above_ma20", None)),
        net_inflow=flow,
        flow_norm=(flow / max_flow if max_flow > 0 and flow is not None else None),
        sentiment_polarity=finite(getattr(outlook, "sentiment_polarity", None)),
        sentiment_intensity=finite(getattr(outlook, "sentiment_intensity", None)),
        headlines=headlines,
        news_event_types=tuple(getattr(outlook, "news_event_types", ()) or ()),
        news_hits=news_hits,
        catalyst=read_catalysts((*headlines, *news_hits), sector=True),
    )


def _max_sector_flow(outlooks: list[Any]) -> float:
    return max(
        (
            abs(value)
            for value in (finite(getattr(item, "net_inflow", None)) for item in outlooks)
            if value is not None
        ),
        default=0.0,
    )


def _normalise_numeric_map(values: Mapping[str, float] | None) -> dict[str, float]:
    result: dict[str, float] = {}
    for symbol, value in (values or {}).items():
        number = finite(value)
        if number is not None:
            result[normalize_symbol(symbol)] = number
    return result
