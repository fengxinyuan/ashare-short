"""Deterministic event evidence extracted from industry and company headlines.

词表本身不在这里，在 `news/lexicon.py`——打分层（`scoring/context.py`）读的是
同一份。这两层曾经各存一份 `RISK_TERMS`，交集只有 4 个词，「退市 / 风险警示 /
财务造假」只在本层有，于是同一条公告在简报里是风险、在打分里是 0 分。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ashare_decision_support.news.lexicon import EVENT_TERMS, RISK_TERMS

__all__ = ["EVENT_TERMS", "RISK_TERMS", "EventEvidence", "detect_events"]


@dataclass(frozen=True)
class EventEvidence:
    event_types: tuple[str, ...] = ()
    hits: tuple[str, ...] = ()
    risk_hits: tuple[str, ...] = ()
    risk_types: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "event_types": list(self.event_types),
            "hits": list(self.hits),
            "risk_hits": list(self.risk_hits),
            "risk_types": list(self.risk_types),
        }


def detect_events(texts: Iterable[str] | None) -> EventEvidence:
    """Extract bounded, explainable event tags from a group of texts."""
    text = " ".join(str(item) for item in (texts or []) if item)
    if not text:
        return EventEvidence()

    matched = _match(EVENT_TERMS, text)
    risks = _match(RISK_TERMS, text)
    return EventEvidence(
        event_types=tuple(dict.fromkeys(event_type for event_type, _ in matched)),
        hits=tuple(dict.fromkeys(term for _, term in matched)),
        risk_hits=tuple(dict.fromkeys(term for _, term in risks)),
        risk_types=tuple(dict.fromkeys(risk_type for risk_type, _ in risks)),
    )


def _match(table: dict[str, tuple[str, ...]], text: str) -> list[tuple[str, str]]:
    return [
        (category, term) for category, terms in table.items() for term in terms if term in text
    ]
