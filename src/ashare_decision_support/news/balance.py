"""Keep market-news sources visible instead of letting one busy feed crowd out the rest."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime
from operator import itemgetter
from typing import Any

from ashare_decision_support.fanout import gather
from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.news.dedup import parse_published


def source_counts(docs: list[NewsDoc]) -> dict[str, int]:
    """Return counts by normalized source name."""
    counts: dict[str, int] = {}
    for doc in docs:
        source = doc.source.strip() or "unknown"
        counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items()))


def collect_by_feed(
    feeds: Sequence[Any],
    fetch_one: Callable[[Any], list[NewsDoc]],
    *,
    limit: int,
    unreachable_note: str,
    empty_note: str,
) -> tuple[list[NewsDoc], list[str], dict[str, Any]]:
    """并发采集各路并均衡截断，返回 (retained, notes, stats)。

    feed.name 用于提示，feed.source 用于来源计数，unreachable_note 支持 {names} 占位符。
    全部无数据时 stats 为空；调用方只在非空时更新上次成功采集的统计。
    """
    docs: list[NewsDoc] = []
    notes: list[str] = []
    raw_by_source: dict[str, int] = {}

    fetched, unreachable = gather(feeds, fetch_one)
    for feed, rows in fetched:
        raw_by_source[feed.source] = len(rows)
        docs.extend(rows)

    if unreachable:
        names = "、".join(f"{item.name}（{type(exc).__name__}）" for item, exc in unreachable)
        notes.append(unreachable_note.format(names=names))
    if not docs:
        notes.append(empty_note)
        return [], notes, {}

    retained, stats = retain_market_docs(docs, limit, raw_by_source)
    return retained, notes, stats


def retain_market_docs(
    docs: list[NewsDoc],
    limit: int,
    raw_by_source: dict[str, int] | None = None,
) -> tuple[list[NewsDoc], dict[str, Any]]:
    """均衡截断并记录原始、保留的来源数量。

    raw_by_source 为 None 时按文档 source 计数；独立来源可显式记录零条，区分无内容与未采集。
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
    """按来源轮流分配配额，再按发布时间倒序返回。

    两次排序共用时间键；同时间文档按来源名称及输入顺序稳定排列。
    """
    if limit <= 0 or not docs:
        return [], {}

    grouped: dict[str, list[tuple[tuple[bool, datetime], NewsDoc]]] = defaultdict(list)
    for doc in docs:
        if doc.title:
            grouped[doc.source.strip() or "unknown"].append((_published_sort_key(doc), doc))

    for items in grouped.values():
        items.sort(key=itemgetter(0), reverse=True)
    sources = sorted(grouped)
    if not sources:
        return [], {}

    quotas = dict.fromkeys(sources, 0)
    remaining = min(limit, sum(len(items) for items in grouped.values()))
    while remaining:
        progressed = False
        for source in sources:
            if quotas[source] >= len(grouped[source]):
                continue
            quotas[source] += 1
            remaining -= 1
            progressed = True
            if not remaining:
                break
        if not progressed:
            break

    selected = [item for source in sources for item in grouped[source][: quotas[source]]]
    selected.sort(key=itemgetter(0), reverse=True)
    return [doc for _key, doc in selected], {
        source: count for source, count in quotas.items() if count
    }


def _published_sort_key(doc: NewsDoc) -> tuple[bool, datetime]:
    stamp = parse_published(doc.published_at)
    return stamp is not None, stamp or datetime.min
