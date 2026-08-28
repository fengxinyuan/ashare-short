"""Find sectors that deserve a second, targeted news pass.

The first market-news pass is intentionally broad. This module closes the
opposite gap: a sector can have clear price/volume strength while its headlines
use product, raw-material, or supply-chain language rather than the sector name.
Those sectors are selected for a wider follow-up fetch and their terms are used
as a recall mechanism, not as a standalone trading signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.news.events import detect_events
from ashare_decision_support.sectors.base import SectorDefinition, SectorMetrics
from ashare_decision_support.sectors.keywords import sector_terms
from ashare_decision_support.symbols import bare_code


@dataclass(frozen=True)
class SectorNewsTarget:
    name: str
    score: float
    matched_docs: int
    reasons: tuple[str, ...] = ()

    @property
    def terms(self) -> tuple[str, ...]:
        return sector_terms(self.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 2),
            "matched_docs": self.matched_docs,
            "reasons": list(self.reasons),
            "terms": list(self.terms[:12]),
        }


def rank_news_targets(
    definitions: list[SectorDefinition],
    metrics: dict[str, SectorMetrics],
    docs: list[NewsDoc],
    net_inflow_by_symbol: dict[str, float] | None = None,
    max_targets: int = 8,
) -> list[SectorNewsTarget]:
    """Rank strong sectors whose current news coverage is thin.

    The score is only a collection priority. It combines persistence, breadth,
    volume and money confirmation, while adding priority when few current
    documents already match the sector vocabulary.
    """
    if max_targets <= 0:
        return []

    existing = sector_doc_counts(definitions, docs)
    inflow = _sector_inflows(definitions, net_inflow_by_symbol or {})
    inflow_scale = max((abs(value) for value in inflow.values()), default=0.0)
    candidates: list[SectorNewsTarget] = []

    for definition in definitions:
        metric = metrics.get(definition.name)
        if metric is None or metric.coverage_ratio <= 0:
            continue
        ret_20d = metric.ret_20d or 0.0
        ret_5d = metric.ret_5d or 0.0
        breadth = metric.breadth or 0.0
        volume_ratio = metric.volume_ratio or 0.0
        money_ratio = (
            inflow.get(definition.name, 0.0) / inflow_scale if inflow_scale else 0.0
        )

        score = 0.0
        reasons: list[str] = []
        if ret_20d >= 0.02:
            points = min(4.0, ret_20d * 40.0)
            score += points
            reasons.append(f"20日趋势 {ret_20d:+.2%}")
        if ret_5d >= 0.015:
            points = min(3.0, ret_5d * 35.0)
            score += points
            reasons.append(f"5日强度 {ret_5d:+.2%}")
        if breadth >= 0.55:
            score += min(2.0, (breadth - 0.5) * 8.0)
            reasons.append(f"上涨广度 {breadth:.0%}")
        if volume_ratio >= 1.05:
            score += min(2.0, (volume_ratio - 1.0) * 3.0)
            reasons.append(f"量比 {volume_ratio:.2f}")
        if money_ratio > 0:
            score += min(2.0, money_ratio * 2.0)
            reasons.append("龙虎榜净流入")

        matched = existing.get(definition.name, 0)
        if matched <= 1:
            score += 2.5
            reasons.append(f"当前匹配舆情仅 {matched} 条")
        elif matched <= 3:
            score += 1.0
            reasons.append(f"当前匹配舆情 {matched} 条")

        # Only sectors with an observable positive trend enter the supplement
        # pass. A news vacuum by itself is not evidence of an opportunity.
        if score >= 3.0 and (ret_20d >= 0.02 or ret_5d >= 0.015 or breadth >= 0.62):
            candidates.append(
                SectorNewsTarget(
                    name=definition.name,
                    score=score,
                    matched_docs=matched,
                    reasons=tuple(reasons),
                )
            )

    return sorted(candidates, key=lambda item: (item.score, -item.matched_docs), reverse=True)[
        :max_targets
    ]


def sector_doc_counts(
    definitions: list[SectorDefinition],
    docs: list[NewsDoc],
) -> dict[str, int]:
    """Count current documents matching each sector's recall vocabulary."""
    counts = {definition.name: 0 for definition in definitions}
    terms_by_sector = {
        definition.name: sector_terms(definition.name) for definition in definitions
    }
    for doc in docs:
        text = doc.text
        if not text:
            continue
        for name, terms in terms_by_sector.items():
            if any(term in text for term in terms):
                counts[name] += 1
    return counts


def sector_coverage_snapshot(
    definitions: list[SectorDefinition],
    metrics: dict[str, SectorMetrics],
    docs: list[NewsDoc],
) -> dict[str, dict[str, Any]]:
    """Diagnose whether price strength and news coverage agree."""
    terms_by_sector = {
        definition.name: sector_terms(definition.name) for definition in definitions
    }
    snapshot: dict[str, dict[str, Any]] = {}
    for definition in definitions:
        matched = [
            doc
            for doc in docs
            if doc.text
            and any(term in doc.text for term in terms_by_sector[definition.name])
        ]
        metric = metrics.get(definition.name)
        ret_5d = metric.ret_5d if metric is not None else None
        ret_20d = metric.ret_20d if metric is not None else None
        breadth = metric.breadth if metric is not None else None
        strong = bool(
            (ret_20d is not None and ret_20d >= 0.02)
            or (ret_5d is not None and ret_5d >= 0.015)
            or (breadth is not None and breadth >= 0.62)
        )
        if strong and len(matched) <= 1:
            status = "行情强但新闻不足"
        elif len(matched) >= 3 and not strong:
            status = "新闻较多但行情未确认"
        elif strong and matched:
            status = "行情与新闻同向"
        elif matched:
            status = "有新闻但行情偏弱"
        else:
            status = "无匹配新闻"
        evidence = detect_events(doc.text for doc in matched)
        snapshot[definition.name] = {
            "doc_count": len(matched),
            "status": status,
            "strong_price_signal": strong,
            "ret_5d": ret_5d,
            "ret_20d": ret_20d,
            "breadth": breadth,
            "event_types": list(evidence.event_types),
            "hits": list(evidence.hits[:8]),
            "risk_hits": list(evidence.risk_hits[:8]),
        }
    return snapshot


def matching_docs(
    docs: list[NewsDoc],
    targets: list[SectorNewsTarget],
) -> list[NewsDoc]:
    """Keep documents relevant to at least one targeted sector."""
    if not docs or not targets:
        return []
    terms = tuple(term for target in targets for term in target.terms)
    return [doc for doc in docs if doc.text and any(term in doc.text for term in terms)]


def _sector_inflows(
    definitions: list[SectorDefinition],
    by_symbol: dict[str, float],
) -> dict[str, float]:
    if not by_symbol:
        return {}
    normalized = {bare_code(symbol): value for symbol, value in by_symbol.items()}
    return {
        definition.name: sum(
            normalized.get(bare_code(symbol), 0.0) for symbol in definition.symbols
        )
        for definition in definitions
    }
