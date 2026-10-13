"""板块层契约：分类定义、成分股统计与排序结论。

分类、行情和舆情分别降级；缺少舆情时仍可保留已取得的板块动量。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field

# 同花顺行业与概念。行业使用新标识，避免与历史申万大类排名混在一起。
LEVEL_INDUSTRY = "THS_INDUSTRY"
LEVEL_CONCEPT = "CONCEPT"


class SectorTaxonomyError(RuntimeError):
    """分类不可用；不能将取数失败解释成市场没有板块。"""


@dataclass(frozen=True)
class SectorDefinition:
    name: str
    level: str
    symbols: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.symbols)


@dataclass(frozen=True)
class SectorMetrics:
    """Measured behaviour of one sector over the covered window.

    Central tendency is the *median*, not the mean: one constituent at +10%
    would drag a 30-name mean far enough to reorder the whole ranking, and
    ultra-short sector rotation is about the body of the sector moving, not its
    single best name.
    """

    name: str
    constituents: int
    covered: int
    ret_1d: float | None = None
    ret_5d: float | None = None
    breadth: float | None = None
    strong_count: int = 0
    volume_ratio: float | None = None
    above_ma20: float | None = None
    # 中期趋势与回调深度。低吸要的板块画像是「中期强 + 短期弱」，只有 1 日和 5 日
    # 读数答不了这个问题——5 日涨幅高说明的恰恰是**已经涨过了**。
    ret_20d: float | None = None
    pullback: float | None = None  # 板块中位的「距 20 日高回撤」，≤0
    pullback_atr: float | None = None  # 同上，除以各自日波幅后的中位数

    @property
    def coverage_ratio(self) -> float:
        return self.covered / self.constituents if self.constituents else 0.0


def daily_returns(metrics: dict[str, SectorMetrics]) -> dict[str, float]:
    """取有覆盖的板块日收益，统一预测台账与主题台账的基准口径。"""
    return {
        name: item.ret_1d
        for name, item in metrics.items()
        if item.ret_1d is not None and item.coverage_ratio > 0
    }


@dataclass
class SectorOutlook:
    """Ranked verdict for one sector."""

    name: str
    score: float
    metrics: SectorMetrics
    # 同花顺行业与概念分别排名；旧记录保留原分类标识。
    level: str = ""
    sentiment_polarity: float = 0.0
    sentiment_intensity: float = 0.0
    net_inflow: float | None = None
    # False when too few constituents had usable bars. An untrusted sector is
    # reported but must never be *selected*: scoring it at the neutral default
    # would let "no data" outrank a sector measured as weak.
    trusted: bool = True
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    headlines: list[str] = field(default_factory=list)
    news_doc_count: int = 0
    news_event_types: tuple[str, ...] = ()
    news_hits: tuple[str, ...] = ()
    news_risk_hits: tuple[str, ...] = ()

    @property
    def direction(self) -> str:
        if not self.trusted:
            return "数据不足"
        if self.score >= 65:
            return "偏多"
        if self.score >= 45:
            return "中性"
        return "偏空"


def reverse_index(definitions: Iterable[SectorDefinition]) -> dict[str, str]:
    """按定义顺序生成 symbol 到板块名的映射，重叠标的归入第一个板块。

    调用方按板块强弱传入定义，因此重叠标的继承最强板块的上下文。"""
    index: dict[str, str] = {}
    for definition in definitions:
        for symbol in definition.symbols:
            index.setdefault(symbol, definition.name)
    return index


class SectorTaxonomy(ABC):
    """Supplies sector definitions and their constituents."""

    name: str = "base"

    @abstractmethod
    def list_sectors(self) -> list[SectorDefinition]:
        """Return every sector with its constituent symbols."""
