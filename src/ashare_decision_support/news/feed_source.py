"""官方列表页与 RSS 源共用的并发采集、缓存和健康检查。

子类配置来源名称、缓存前缀及提示文本，并实现 fetch_one；单路失败独立降级。
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

# 新闻站点使用独立的浏览器标识，避免与行情请求配置耦合。
USER_AGENT = "Mozilla/5.0"
TIMEOUT_SECONDS = 15
PER_FEED_LIMIT = 30


class FeedNewsSource(NewsSource):
    """独立信息源的采集基类；子类通过类属性配置名称、缓存键与提示文本。"""

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
        # 全路失败时保留上次成功采集的来源统计。
        self.last_market_stats: dict[str, object] = {}

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
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
        """读取一路原始文档；异常由采集与健康检查调用方分别记录。"""
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
