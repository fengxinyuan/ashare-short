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

from ashare_decision_support.news.base import NewsDoc, NewsSource


class CompositeNewsSource(NewsSource):
    """按「市场快讯并集、其余首个可用」合并多路源。"""

    name = "composite"

    def __init__(self, *sources: NewsSource) -> None:
        self.sources = [s for s in sources if s is not None]
        if not self.sources:
            raise ValueError("CompositeNewsSource 至少需要一路源")

    # ---- market-wide -----------------------------------------------------

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        docs: list[NewsDoc] = []
        notes: list[str] = []
        alive = 0
        for source in self.sources:
            try:
                got, source_notes = source.fetch_market(trade_date, limit=limit)
            except Exception as exc:  # noqa: BLE001 - 一路挂掉不能拖垮其余
                notes.append(f"舆情源 {source.name} 不可用（{type(exc).__name__}），本次跳过")
                continue
            notes.extend(source_notes)
            if got:
                alive += 1
                docs.extend(got)

        if not docs:
            notes.append("全部舆情源都没有返回市场快讯")
        elif alive < len(self.sources):
            # 少一路就是少一块覆盖面，不能让它读起来像「都取到了」。
            notes.append(f"市场快讯来自 {alive}/{len(self.sources)} 路源")
        # 不在这里截断到 limit：跨源去重要在 dedup 层做，先截会按源的先后顺序
        # 砍掉后面那路的全部内容，而不是砍掉最旧的。
        return docs, notes

    # ---- 其余：第一个给出非空结果的源说了算 --------------------------------

    def fetch_for_symbols(self, symbols: list[str], **kwargs):
        return self._first("fetch_for_symbols", {}, symbols, **kwargs)

    def fetch_announcements(self, trade_date: date):
        return self._first("fetch_announcements", [], trade_date)

    def fetch_dragon_tiger(self, start: date, end: date):
        return self._first("fetch_dragon_tiger", [], start, end)

    def _first(self, method: str, empty, *args, **kwargs):
        notes: list[str] = []
        for source in self.sources:
            fn = getattr(source, method, None)
            if not callable(fn):
                continue
            try:
                value, source_notes = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"舆情源 {source.name}.{method} 不可用（{type(exc).__name__}）")
                continue
            notes.extend(source_notes)
            if value:
                return value, notes
        return empty, notes
