"""Contracts for the sector (板块) layer.

The layer answers three questions in order:
  1. What are the sectors and who is in them?      -> SectorTaxonomy
  2. How did each sector actually trade?           -> SectorMetrics
  3. Which ones deserve attention tomorrow?        -> SectorOutlook

Keeping them separate matters because they degrade independently: the taxonomy
is a cached static file, the metrics need daily bars, and the outlook needs
news. Losing the third should still leave you with a usable momentum ranking.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field


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
    pullback: float | None = None       # 板块中位的「距 20 日高回撤」，≤0
    pullback_atr: float | None = None   # 同上，除以各自日波幅后的中位数

    @property
    def coverage_ratio(self) -> float:
        return self.covered / self.constituents if self.constituents else 0.0


@dataclass
class SectorOutlook:
    """Ranked verdict for one sector."""

    name: str
    score: float
    metrics: SectorMetrics
    # 这条读数出自哪一层分类（`SectorDefinition.level`：SW1/SW2/SW3 或 CONCEPT）。
    #
    # 行业与概念**并排跑、各自排名**，所以选择那一步必须能把两层分开。理由与
    # `sectors/intraday.py` 开头写的同一条：把一个 1215 只的概念和一个 88 只的
    # 行业放进同一张榜，名次本身就没有含义了。空串是老记录/单层调用方的默认值。
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
    """symbol → 板块名。**一只票落在多个板块时留下第一个。**

    这条并列规则不是实现细节，它决定了这只票在打分时继承谁的板块热度：
    `scoring/context.py:build_factor_contexts` 拿这张表给每只票挂上所属板块的
    热度、资金与排名。而调用方传进来的 `definitions` 是**按板块强弱排好序**的，
    所以「留第一个」= 归给它所属板块里最强的那个，「留最后一个」= 归给最弱的那个。
    同一只票，两种规则会得出不同的分数。

    收成一个函数是因为它曾经有两份、而且两份不一样：`SectorTaxonomy.sector_of`
    写了 `setdefault`（留第一个）并把理由写进了 docstring，`jobs/morning.py` 那份
    是字典生成式（**留最后一个**），两份都对着自己那半边。生成式那份是活的，
    带 docstring 的那份一个调用方都没有——所以真正在跑的是没写下理由的那一半，
    而它跑的正好是反的。

    概念板块接进排序之后这条会更要紧：一只票同时属于三四个概念是常态，不是边角。
    """
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
