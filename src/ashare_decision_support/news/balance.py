"""Keep market-news sources visible instead of letting one busy feed crowd out the rest."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.news.dedup import parse_published


def source_counts(docs: list[NewsDoc]) -> dict[str, int]:
    """Return counts by normalized source name."""
    counts: dict[str, int] = {}
    for doc in docs:
        source = doc.source.strip() or "unknown"
        counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items()))


def retain_market_docs(
    docs: list[NewsDoc],
    limit: int,
    raw_by_source: dict[str, int] | None = None,
) -> tuple[list[NewsDoc], dict[str, Any]]:
    """均衡截断 + 「采到多少 / 留下多少」的记账，一次做完。

    这段此前在**五个**源里逐字重复（akshare / 华尔街见闻 / 官方 / RSS / composite）。
    值得收口的不是那四行，是 `last_market_stats` 的**字段名**：`agents/collectors.py`
    靠 `raw_by_source` / `retained_by_source` 拼那条「市场舆情来源覆盖」的 note，
    任何一个源把键名写岔，症状不是报错，是那一路在覆盖率里静默消失。

    `raw_by_source` 给 None 表示按文档自报的 `source` 字段数——这是聚合型源
    （composite、华尔街见闻的双频道）的口径；分路取数的源自己数，因为**一路取回
    0 条**和**一路根本没跑**在文档里长得一模一样，只有取数方分得清。
    """
    retained, retained_by_source = balance_by_source(docs, limit)
    counts = source_counts(docs) if raw_by_source is None else dict(sorted(raw_by_source.items()))
    stats: dict[str, Any] = {
        "raw_doc_count": len(docs),
        "retained_doc_count": len(retained),
        "raw_by_source": counts,
        "retained_by_source": retained_by_source,
    }
    return retained, stats


def balance_by_source(
    docs: list[NewsDoc],
    limit: int,
) -> tuple[list[NewsDoc], dict[str, int]]:
    """Retain a recent, source-balanced slice of market documents.

    A plain ``docs[:limit]`` makes source order a hidden weighting parameter:
    the first feed can consume the whole budget before policy or industry feeds
    are seen. Quotas are allocated round-robin and any unused quota is
    redistributed, then the retained documents are put back into time order.
    """
    if limit <= 0 or not docs:
        return [], {}

    grouped: dict[str, list[NewsDoc]] = defaultdict(list)
    for doc in docs:
        if doc.title:
            grouped[doc.source.strip() or "unknown"].append(doc)

    ordered_groups: dict[str, list[NewsDoc]] = {
        source: sorted(items, key=_published_sort_key, reverse=True)
        for source, items in grouped.items()
    }
    sources = sorted(ordered_groups)
    if not sources:
        return [], {}

    quotas = dict.fromkeys(sources, 0)
    remaining = min(limit, sum(len(items) for items in ordered_groups.values()))
    # Round-robin is intentionally simple and deterministic. It gives every
    # reachable source a chance before a large feed receives its second slot.
    while remaining:
        progressed = False
        for source in sources:
            if quotas[source] >= len(ordered_groups[source]):
                continue
            quotas[source] += 1
            remaining -= 1
            progressed = True
            if not remaining:
                break
        if not progressed:
            break

    selected = [
        doc
        for source in sources
        for doc in ordered_groups[source][: quotas[source]]
    ]
    selected.sort(key=_published_sort_key, reverse=True)
    return selected, source_counts(selected)


def _published_sort_key(doc: NewsDoc) -> tuple[bool, datetime]:
    stamp = parse_published(doc.published_at)
    return stamp is not None, stamp or datetime.min
