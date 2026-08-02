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

    @property
    def coverage_ratio(self) -> float:
        return self.covered / self.constituents if self.constituents else 0.0


@dataclass
class SectorOutlook:
    """Ranked verdict for one sector."""

    name: str
    score: float
    metrics: SectorMetrics
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

    @property
    def direction(self) -> str:
        if not self.trusted:
            return "数据不足"
        if self.score >= 65:
            return "偏多"
        if self.score >= 45:
            return "中性"
        return "偏空"


class SectorTaxonomy(ABC):
    """Supplies sector definitions and their constituents."""

    name: str = "base"

    @abstractmethod
    def list_sectors(self) -> list[SectorDefinition]:
        """Return every sector with its constituent symbols."""

    def sector_of(self) -> dict[str, str]:
        """Reverse index: symbol -> sector name.

        A symbol in two sectors keeps the first, which is deterministic because
        `list_sectors` returns a stable order.
        """
        index: dict[str, str] = {}
        for definition in self.list_sectors():
            for symbol in definition.symbols:
                index.setdefault(symbol, definition.name)
        return index
