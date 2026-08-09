"""把多路舆情源合成一路。

**为什么需要它。** 舆情的四种取数（市场快讯 / 个股新闻 / 公告 / 龙虎榜）来源并不
对称：`AkshareNewsSource` 四种全给，`WallstreetNewsSource` 只补市场快讯里的宏观段。
调用方（`jobs/morning.py`）不该为此分叉。

**合并规则只有一条：市场快讯取并集，其余取第一个能给出结果的源。** 前者是因为
不同源的覆盖面本来就互补（实测：东财+财联社覆盖地缘 66 次命中、宏观数据 0 次命中，
华尔街见闻正好相反）；后者是因为公告与龙虎榜是**同一份交易所数据**，两路取到的
只会是同一批，取并集除了制造重复没有别的作用。

跨源重复由 `news/dedup.py` 在下游按标题归并，这里不做——去重要和时效加权一起做，
拆开会让同一条消息在两个地方各算一次权重。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ashare_decision_support.fanout import gather
from ashare_decision_support.news.balance import retain_market_docs
from ashare_decision_support.news.base import NewsDoc, NewsSource


class CompositeNewsSource(NewsSource):
    """按「市场快讯并集、其余首个可用」合并多路源。"""

    name = "composite"

    def __init__(self, *sources: NewsSource) -> None:
        self.sources = [s for s in sources if s is not None]
        if not self.sources:
            raise ValueError("CompositeNewsSource 至少需要一路源")
        self.last_market_stats: dict[str, object] = {}

    # ---- market-wide -----------------------------------------------------

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        docs: list[NewsDoc] = []
        notes: list[str] = []
        alive = 0

        # 四路源各自内部已经并发，但**源与源之间**此前还是串行的——那等于把
        # akshare、华尔街见闻、国内官方、海外 RSS 四段等待首尾相接。这四路彼此
        # 毫无依赖，谁也不读谁的结果，所以整层一起放开。
        #
        # 保序展开：`retain_market_docs` 按来源发配额，同一来源内取列表前缀。
        # 完成顺序若能影响拼接顺序，同一个盘前跑两次就会得到两批不同的文档。
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
        balanced, self.last_market_stats = retain_market_docs(docs, limit)
        if raw_count > len(balanced):
            notes.append(
                f"市场快讯原始 {raw_count} 条，按来源均衡保留 {len(balanced)} 条"
            )
        return balanced, notes

    # ---- 其余：第一个给出非空结果的源说了算 --------------------------------

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
