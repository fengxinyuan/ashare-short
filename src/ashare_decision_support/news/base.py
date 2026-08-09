"""Contracts for the sentiment (舆情) layer.

The layer is deliberately split into *collection* (`NewsSource`) and *scoring*
(`SentimentAnalyzer`) so a failing feed degrades into "fewer documents" rather
than "no sentiment at all", and so the scoring stays unit-testable without
touching the network.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class NewsDoc:
    """One normalized document from any feed.

    `symbols` is only populated by per-stock feeds. Market-wide feeds leave it
    empty and are attributed to sectors by keyword matching instead.
    """

    title: str
    summary: str = ""
    source: str = ""
    published_at: str = ""
    url: str = ""
    symbols: tuple[str, ...] = ()
    # 时效权重，由 `news/dedup.py:weigh_docs` 打上，聚合时按它加权。默认 1.0，
    # 所以没经过时效层的调用方（测试、外部脚本）行为完全不变。
    weight: float = 1.0
    # Discovery-only metadata from an authorised social snapshot.  The
    # sentiment scorer deliberately ignores it.
    engagement: float | None = None
    heat_rank: int | None = None

    @property
    def text(self) -> str:
        return f"{self.title} {self.summary}".strip()


@dataclass(frozen=True)
class SentimentScore:
    """Lexicon verdict for one piece of text.

    `polarity` is bounded to [-1, 1]. `intensity` is the raw hit density and is
    used as a confidence weight: a headline with one matched word should not
    move a sector as much as one with six.
    """

    polarity: float
    intensity: float
    bullish_hits: tuple[str, ...] = ()
    bearish_hits: tuple[str, ...] = ()

    @property
    def is_neutral(self) -> bool:
        return not self.bullish_hits and not self.bearish_hits


@dataclass(frozen=True)
class SymbolSentiment:
    """One symbol's own news reading, separate from its sector's.

    Kept apart on purpose: a stock can carry a 减持公告 while its whole sector
    is being talked up, and averaging the two into one number is exactly how
    that gets lost. The sector reading answers "is this where attention is";
    this one answers "is there anything on this specific name".
    """

    symbol: str
    polarity: float
    intensity: float
    doc_count: int
    headlines: tuple[str, ...] = ()
    bullish_hits: tuple[str, ...] = ()
    bearish_hits: tuple[str, ...] = ()
    as_of: str | None = None

    @property
    def is_neutral(self) -> bool:
        return not self.doc_count or (not self.bullish_hits and not self.bearish_hits)

    def to_dict(self) -> dict:
        return {
            "polarity": round(self.polarity, 4),
            "intensity": round(self.intensity, 4),
            "doc_count": self.doc_count,
            "headlines": list(self.headlines),
            "bullish_hits": list(self.bullish_hits),
            "bearish_hits": list(self.bearish_hits),
            "as_of": self.as_of,
        }


class NewsSource(ABC):
    """Collects raw documents. Implementations must never raise on feed failure.

    A dead feed is an expected state, not an error: the caller gets whatever
    was reachable plus a note explaining what was missing, and decides for
    itself whether the remaining coverage is enough.
    """

    name: str = "base"

    @abstractmethod
    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        """Return (documents, notes) for market-wide feeds."""

    @abstractmethod
    def fetch_for_symbols(self, symbols: list[str], limit: int = 10) -> tuple[dict[str, list[NewsDoc]], list[str]]:
        """Return ({symbol: documents}, notes) for per-stock feeds."""


class SentimentAnalyzer(ABC):
    """Turns text into a bounded polarity score."""

    name: str = "base"

    @abstractmethod
    def score_text(self, text: str) -> SentimentScore:
        """Score a single string."""

    def score_docs(self, docs: list[NewsDoc]) -> SentimentScore:
        """Aggregate a document set, weighting each doc by its own intensity.

        Intensity weighting stops a pile of neutral filler headlines from
        diluting one strongly-worded item down to nothing. `NewsDoc.weight`
        multiplies on top of it, so a stale or duplicated document contributes
        proportionally less without needing a separate code path — documents
        that never went through `news/dedup.py` carry weight 1.0 and behave
        exactly as before.
        """
        pairs = [(self.score_text(doc.text), max(0.0, doc.weight)) for doc in docs]
        active = [(score, weight) for score, weight in pairs if not score.is_neutral and weight > 0]
        if not active:
            return SentimentScore(polarity=0.0, intensity=0.0)

        weight_total = sum(score.intensity * weight for score, weight in active)
        if weight_total <= 0:
            return SentimentScore(polarity=0.0, intensity=0.0)

        polarity = (
            sum(score.polarity * score.intensity * weight for score, weight in active) / weight_total
        )
        bullish = tuple(dict.fromkeys(hit for score, _ in active for hit in score.bullish_hits))
        bearish = tuple(dict.fromkeys(hit for score, _ in active for hit in score.bearish_hits))
        return SentimentScore(
            polarity=max(-1.0, min(1.0, polarity)),
            intensity=min(1.0, weight_total / max(len(docs), 1)),
            bullish_hits=bullish,
            bearish_hits=bearish,
        )

    def score_symbol(
        self,
        symbol: str,
        docs: list[NewsDoc],
        as_of: str | None = None,
        max_headlines: int = 3,
    ) -> SymbolSentiment:
        """Same aggregation, packaged as a per-symbol reading for the scorer."""
        score = self.score_docs(docs)
        return SymbolSentiment(
            symbol=symbol,
            polarity=score.polarity,
            intensity=score.intensity,
            doc_count=len(docs),
            headlines=tuple(doc.title for doc in docs[:max_headlines] if doc.title),
            bullish_hits=score.bullish_hits[:5],
            bearish_hits=score.bearish_hits[:5],
            as_of=as_of,
        )
