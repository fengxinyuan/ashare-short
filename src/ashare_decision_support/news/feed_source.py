"""分路取数的舆情源共用的那副骨架。

`official_source`（国务院/央行/证监会的列表页）与 `rss_source`（美联储/BEA 等
海外官方 feed）从取数机制上看是同一件事：**一组独立的 URL，每路各自可能挂，
并发取回、按路记账、合并截断**。两个文件此前各写了一遍这副骨架——构造函数、
`fetch_market`、`fetch_for_symbols`、`health_check`、缓存包装，五个方法逐字
对应，差别只有栏目/feed 这个词、缓存键前缀和两句 note 文案。

这轮之前已经收掉过两层：`balance.collect_by_feed` 收的是「采到多少」那一行，
`cache.cached_docs` 收的是「miss 才写」那个顺序，两处的模块注释都写着「此前
各写一遍」。**剩下的是把两遍调用本身也合成一遍**——理由和前两次同一条：这副
骨架里每多一处分叉，就多一条「只有一边有」的可能，而两边都不会因此报错。

子类只剩下真正不同的那一件事：**给定一路，取回它的 `NewsDoc`**。
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from datetime import date
from typing import Any

from ashare_decision_support.news.balance import collect_by_feed
from ashare_decision_support.news.base import NewsDoc, NewsSource
from ashare_decision_support.news.cache import NewsCache, cached_docs
from ashare_decision_support.news.health import probe_feeds

# 和 `adapters/http.py` 里那个同名同值，**这一份是有意保留的**。两处都只是「给个
# 浏览器头」的占位值，而 `news/` 与 `adapters/` 之间目前没有任何一条 import——
# 舆情层与行情层各自独立，为了共用一个 12 个字符的占位串去建这条依赖边，换来的
# 不是「少一处重复」，是「以后改行情站点的 UA 会连带影响舆情源」。
#
# 收口的判据一直是**「改一处而漏了另一处会不会静默出错」**：文件名、环境变量名、
# 端点地址都会（读写两端必须相等），这个不会——两边打的是完全不同的站点，各自
# 改各自的。
USER_AGENT = "Mozilla/5.0"
TIMEOUT_SECONDS = 15
PER_FEED_LIMIT = 30


class FeedNewsSource(NewsSource):
    """一组独立可降级的取数路。

    子类要给的四个类属性就是两边原来仅有的差别：`name` 进健康检查与覆盖统计，
    `cache_prefix` 让两个源的缓存键不撞，两句 note 是给人读的。
    """

    name = "feed"
    # 缓存键前缀。两个源可能取到同名机构，不加前缀会互相覆盖。
    cache_prefix = "feed"
    # `{names}` 是占位符，由 `collect_by_feed` 填进挂掉的那几路的名字。
    unreachable_note = "信息源 {names} 不可用，本次跳过"
    empty_note = "信息源未返回可用条目"
    per_feed_limit = PER_FEED_LIMIT

    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
        timeout: int = TIMEOUT_SECONDS,
        feeds: tuple[Any, ...] = (),
        cache: NewsCache | None = None,
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.feeds = feeds
        self.cache = cache
        # 「上一次成功采集的构成」。全挂的那一轮不覆盖它，理由见
        # `balance.collect_by_feed` 的 docstring。
        self.last_market_stats: dict[str, object] = {}

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        # 每一路是一个独立的网络往返，串行等于把每一路的超时相加。
        retained, notes, stats = collect_by_feed(
            self.feeds,
            lambda feed: self._cached_fetch(trade_date, feed),
            limit=limit,
            unreachable_note=self.unreachable_note,
            empty_note=self.empty_note,
        )
        if stats:
            self.last_market_stats = stats
        return retained, notes

    def fetch_for_symbols(
        self, symbols: list[str], limit: int = 10
    ) -> tuple[dict[str, list[NewsDoc]], list[str]]:
        """这一类源只有市场级内容，没有按标的的通道。"""
        return {}, []

    def health_check(self) -> dict[str, Any]:
        return probe_feeds(self.name, self.feeds, lambda feed: feed.name, self.fetch_one)

    def fetch_one(self, feed: Any) -> list[NewsDoc]:
        """取回一路的全部条目。**唯一需要子类实现的方法。**

        允许抛异常：抛出算这一路不可达，由 `collect_by_feed` 与 `probe_feeds`
        各自记账——这是这一层的降级契约，不是漏掉的 try。
        """
        raise NotImplementedError

    def open_bytes(self, url: str, max_bytes: int | None = None) -> bytes:
        """带浏览器头取回原始字节。

        交回字节而不是字符串：列表页按 utf-8 解码，RSS 要把整段交给 XML 解析器
        自己认编码声明。`max_bytes` 是 feed 那一路的响应上限。
        """
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with self._opener(request, timeout=self.timeout) as response:
            return response.read() if max_bytes is None else response.read(max_bytes)

    def _cached_fetch(self, trade_date: date, feed: Any) -> list[NewsDoc]:
        return cached_docs(
            self.cache,
            trade_date,
            f"{self.cache_prefix}_{feed.source}",
            lambda: self.fetch_one(feed),
        )
