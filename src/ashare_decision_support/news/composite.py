"""组合舆情源：市场快讯并集，其余数据取首个非空结果。

市场快讯按时效筛选后分配来源配额；跨源去重与权重处理由 news/dedup.py 完成。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from ashare_decision_support.fanout import gather
from ashare_decision_support.news.balance import retain_market_docs, source_counts
from ashare_decision_support.news.base import NewsDoc, NewsSource
from ashare_decision_support.news.dedup import parse_published, recency_weight
from ashare_decision_support.schedule.timezone import beijing_naive_now


class CompositeNewsSource(NewsSource):
    """按「市场快讯并集、其余首个可用」合并多路源。"""

    name = "composite"

    def __init__(self, *sources: NewsSource) -> None:
        self.sources = [s for s in sources if s is not None]
        if not self.sources:
            raise ValueError("CompositeNewsSource 至少需要一路源")
        self.last_market_stats: dict[str, object] = {}

    # market-wide

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        docs: list[NewsDoc] = []
        notes: list[str] = []
        alive = 0

        # 并发采集后按来源顺序展开，保证相同时间的文档排序稳定。
        fetched, unreachable = gather(
            self.sources,
            lambda source: source.fetch_market(trade_date, limit=limit),
        )
        for source, exc in unreachable:
            notes.append(f"舆情源 {source.name} 不可用（{type(exc).__name__}），本次跳过")
        for _source, (got, source_notes) in fetched:
            notes.extend(source_notes)
            if got:
                alive += 1
                docs.extend(got)

        if not docs:
            notes.append("全部舆情源都没有返回市场快讯")
        elif alive < len(self.sources):
            # 少一路就是少一块覆盖面，不能让它读起来像「都取到了」。
            notes.append(f"市场快讯来自 {alive}/{len(self.sources)} 路源")
        raw_count = len(docs)
        # 先剔除过期稿件再分配配额；最新发布时间仍覆盖所有原始稿件。
        observed_at = beijing_naive_now()
        moment = (
            observed_at
            if trade_date == observed_at.date()
            else datetime.combine(trade_date, time.max)
        )
        current: list[NewsDoc] = []
        latest_by_source: dict[str, datetime] = {}
        for doc in docs:
            stamp = parse_published(doc.published_at)
            if recency_weight(stamp, moment) > 0:
                current.append(doc)
            if stamp is not None:
                previous = latest_by_source.get(doc.source)
                if previous is None or stamp > previous:
                    latest_by_source[doc.source] = stamp

        balanced, stats = retain_market_docs(current, limit)
        self.last_market_stats = {
            **stats,
            "raw_doc_count": raw_count,
            "raw_by_source": source_counts(docs),
            "effective_doc_count": len(current),
            "effective_by_source": stats["raw_by_source"],
            "expired_before_budget": raw_count - len(current),
            "latest_by_source": {
                source: stamp.isoformat(timespec="seconds")
                for source, stamp in sorted(latest_by_source.items())
            },
        }
        if raw_count > len(current):
            notes.append(f"超过 72 小时的快讯在分配来源预算前剔除 {raw_count - len(current)} 条")
        if raw_count > len(balanced):
            notes.append(f"市场快讯原始 {raw_count} 条，按来源均衡保留 {len(balanced)} 条")
        return balanced, notes

    # 其余：第一个给出非空结果的源说了算

    def fetch_for_symbols(self, symbols: list[str], **kwargs):
        return self._first("fetch_for_symbols", {}, symbols, **kwargs)

    def fetch_announcements(self, trade_date: date):
        return self._first("fetch_announcements", [], trade_date)

    def fetch_dragon_tiger(self, start: date, end: date):
        return self._first("fetch_dragon_tiger", [], start, end)

    def health_check(self) -> dict[str, Any]:
        """把四路源各自的探针结果并到一份里。

        整层的 `reachable` 是「至少还有一路」，但真正要看的是 `unreachable_feeds`：
        这四路覆盖面互补，少哪一路决定的是简报少哪一块，不是少多少条。没实现
        `health_check` 的源列进 `unprobed`，不假装它健康。
        """
        probed = [s for s in self.sources if callable(getattr(s, "health_check", None))]
        results, failed = gather(probed, lambda source: source.health_check())

        by_source: dict[str, Any] = {}
        unreachable: list[str] = []
        for source, report in results:
            by_source[source.name] = report
            unreachable.extend(
                f"{source.name}/{feed}" for feed in report.get("unreachable_feeds", ())
            )
        for source, exc in failed:
            by_source[source.name] = {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}
            unreachable.append(source.name)

        return {
            "source": self.name,
            "reachable": any(item.get("reachable") for item in by_source.values()),
            "unreachable_feeds": sorted(unreachable),
            "unprobed": sorted(s.name for s in self.sources if s not in set(probed)),
            "sources": dict(sorted(by_source.items())),
        }

    def _first(self, method: str, empty, *args, **kwargs):
        notes: list[str] = []
        for source in self.sources:
            fn = getattr(source, method, None)
            if not callable(fn):
                continue
            try:
                value, source_notes = fn(*args, **kwargs)
            except Exception as exc:
                notes.append(f"舆情源 {source.name}.{method} 不可用（{type(exc).__name__}）")
                continue
            notes.extend(source_notes)
            if value:
                return value, notes
        return empty, notes
