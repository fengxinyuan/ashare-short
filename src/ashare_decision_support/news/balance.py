"""Keep market-news sources visible instead of letting one busy feed crowd out the rest."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime
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
    """并发跑完每一路，按路记账，再交给 `retain_market_docs` 截断。

    返回 `(retained, notes, stats)`。`feeds` 里的每一项只要求有 `.name`（写进
    不可用 note）与 `.source`（记账用的键）。`unreachable_note` 用 `{names}` 占位。

    **一条都没采到时 `stats` 是空 dict，调用方要照着 `if stats:` 赋值。** 这不是
    风格问题：`last_market_stats` 是「上一次成功采集的构成」，全挂的那一轮把它
    覆盖成空，`agents/collectors.py` 那条覆盖率 note 就从「这一路挂了」变成
    「这一路不存在」——而前者才是真的，后者会让人以为源被摘掉了。

    **收口的理由和 `retain_market_docs` 是同一条，只是更靠前一步。** 那个函数收的
    是「留下多少」的字段名，这个收的是**「采到多少」那一行**——
    `raw_by_source[feed.source] = len(rows)`。`official_source` 与 `rss_source`
    此前各写一遍：谁漏了这一行，`agents/collectors.py` 拼出来的「市场舆情来源
    覆盖」里那一路就**静默消失**，而不是报错。一路取回 0 条和一路根本没跑，在
    文档里长得一模一样，只有取数方分得清——所以这一行必须在取数方，也就必须
    只有一份。

    串行取数等于把每一路的超时相加，而这几路都在盘前链路上，所以走 `gather`。
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
