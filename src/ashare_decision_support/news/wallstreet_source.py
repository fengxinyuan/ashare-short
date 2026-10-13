"""华尔街见闻宏观、政策与海外流动性快讯。

并发采集 global-channel 和 a-stock-channel，按回溯时间窗翻页。
单页最多 100 条；部分分页失败时保留已取得文档，并报告覆盖不完整。
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from ashare_decision_support.fanout import gather
from ashare_decision_support.news.balance import retain_market_docs
from ashare_decision_support.news.base import NewsDoc, NewsSource
from ashare_decision_support.news.cache import NewsCache
from ashare_decision_support.news.feed_source import USER_AGENT
from ashare_decision_support.news.health import probe_feeds
from ashare_decision_support.schedule.timezone import BEIJING_TZ, beijing_now

BASE_URL = "https://api-one.wallstcn.com/apiv1/content/lives?channel={channel}&limit={limit}"
CURSOR_SUFFIX = "&cursor={cursor}"

CHANNEL_GLOBAL = "global-channel"
CHANNEL_A_SHARE = "a-stock-channel"

PAGE_LIMIT = 100  # 服务端硬上限

# 按时间窗而非页数对齐频道覆盖；72 小时可覆盖周一盘前至上周五早盘。
DEFAULT_LOOKBACK_HOURS = 72
MAX_PAGES = 16  # 失控保护：单频道最多翻 1600 条
TIMEOUT_SECONDS = 15


class WallstreetNewsSource(NewsSource):
    """宏观 / 政策 / 海外流动性快讯。"""

    name = "wallstreetcn"

    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
        timeout: int = TIMEOUT_SECONDS,
        channels: tuple[str, ...] = (CHANNEL_GLOBAL, CHANNEL_A_SHARE),
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
        max_pages: int = MAX_PAGES,
        cache: NewsCache | None = None,
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.channels = channels
        self.lookback_hours = lookback_hours
        self.max_pages = max_pages
        self.cache = cache
        self.last_market_stats: dict[str, object] = {}

    # market-wide

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        docs: list[NewsDoc] = []
        notes: list[str] = []
        # 当日按实际采集时刻回溯，周一 08:30 的 72 小时窗口才能覆盖周五早盘。
        # 历史日期仍锚定该日结束；同日重跑由 TTL 缓存复用已采集的文档。
        moment = beijing_now()
        anchor = (
            moment
            if moment.date() == trade_date
            else datetime.combine(trade_date, datetime.min.time(), tzinfo=BEIJING_TZ)
            + timedelta(days=1)
        )
        cutoff = int((anchor - timedelta(hours=self.lookback_hours)).timestamp())

        fetched, unreachable = gather(
            self.channels, lambda channel: self._channel_docs(trade_date, channel, cutoff)
        )
        # `_channel` 自己不抛异常，翻第一页就失败时返回 ok=False；两种失败都要
        # 进同一条 note，否则「频道挂了」会读起来像「今天这个频道没消息」。
        partial = [channel for channel, (_, ok, complete) in fetched if ok and not complete]
        empty = {channel for channel, (_, ok, _) in fetched if not ok}
        dead = {channel for channel, _exc in unreachable}
        failed = [channel for channel in self.channels if channel in empty or channel in dead]
        for _, (got, ok, _) in fetched:
            if ok:
                docs.extend(got)

        if failed:
            notes.append(f"华尔街见闻 {'/'.join(failed)} 频道不可用，本次宏观快讯不完整")
        if partial:
            notes.append(
                f"华尔街见闻 {'/'.join(partial)} 频道仅取到部分分页，"
                "本次宏观快讯不完整，未写入完整缓存"
            )
        if not docs:
            notes.append("华尔街见闻快讯全部不可用，本次无宏观数据发布输入")
            return [], notes

        # 同一条快讯可能同时出现在两个频道，按标题去重（跨源去重由 news/dedup.py 做）。
        unique: dict[str, NewsDoc] = {}
        for doc in docs:
            unique.setdefault(doc.title, doc)
        result = sorted(unique.values(), key=lambda d: d.published_at or "", reverse=True)
        oldest = result[-1].published_at[:16] if result and result[-1].published_at else "?"
        notes.append(
            f"华尔街见闻快讯 {len(result)} 条（宏观数据发布与海外流动性），回溯至 {oldest}"
        )
        # 频道合并去重后，按文档 source 字段分配来源配额。
        result, self.last_market_stats = retain_market_docs(result, limit)
        return result, notes

    def fetch_for_symbols(
        self, symbols: list[str], limit: int = 10
    ) -> tuple[dict[str, list[NewsDoc]], list[str]]:
        """个股舆情不走这一路——这是宏观源，按标的检索没有意义。"""
        return {}, []

    def fetch_announcements(self, trade_date: date) -> tuple[list[dict], list[str]]:
        return [], []

    def fetch_dragon_tiger(self, start: date, end: date) -> tuple[list[dict], list[str]]:
        return [], []

    def health_check(self) -> dict[str, Any]:
        # 只探第一页：这里问的是「频道还通不通」，翻满 16 页是取数的事。
        return probe_feeds(
            "wallstreetcn",
            self.channels,
            lambda channel: channel,
            lambda channel: (self._page(channel, None) or {}).get("items") or [],
        )

    def _channel_docs(
        self, trade_date: date, channel: str, cutoff: int | None
    ) -> tuple[list[NewsDoc], bool, bool]:
        """保留部分结果与完整性标识；只缓存已完成窗口的采集。

        版本键隔离旧版误写的完整缓存，不同回溯长度也不能共用覆盖结论。
        """
        key = f"wallstreet_v2_{self.lookback_hours}_{channel}"
        if self.cache is not None:
            cached = self.cache.read_docs(trade_date, key)
            if cached is not None:
                return cached, True, True
        docs, ok, complete = self._channel(channel, cutoff)
        if ok and complete and self.cache is not None:
            self.cache.write_docs(trade_date, key, docs)
        return docs, ok, complete

    def _channel(self, channel: str, cutoff: int | None) -> tuple[list[NewsDoc], bool, bool]:
        """返回 (文档, 这一路算不算取到, 是否翻完整)。

        完整性同时用于缓存与报告。到达页数上限或重复游标时仍返回已采集的部分。
        """
        docs: list[NewsDoc] = []
        cursor: str | None = None
        cursors: set[str] = set()
        cutoff_stamp = _stamp(cutoff) if cutoff is not None else ""
        for _ in range(max(self.max_pages, 1)):
            try:
                payload = self._page(channel, cursor)
            except Exception:
                return docs, bool(docs), False
            items = payload["items"]
            if not items:
                return docs, True, True
            page_docs = [doc for doc in (_to_doc(item) for item in items) if doc is not None]
            docs.extend(page_docs)

            oldest = min((doc.published_at for doc in page_docs if doc.published_at), default="")
            if cutoff_stamp and oldest and oldest <= cutoff_stamp:
                return docs, True, True
            next_cursor = payload.get("next_cursor")
            if not next_cursor:
                return docs, True, True
            cursor = str(next_cursor)
            if cursor in cursors:
                return docs, bool(docs), False
            cursors.add(cursor)
        return docs, bool(docs), False

    def _page(self, channel: str, cursor: str | None) -> dict:
        url = BASE_URL.format(channel=channel, limit=PAGE_LIMIT)
        if cursor:
            url += CURSOR_SUFFIX.format(cursor=cursor)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with self._opener(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "ignore"))
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("华尔街见闻响应缺少有效的 data.items 列表")
        return data


def _to_doc(item: Any) -> NewsDoc | None:
    if not isinstance(item, dict):
        return None
    # 快讯常常只有正文没有标题，此时正文就是标题——丢掉这些等于丢掉大部分数据发布，
    # 因为「中国7月官方制造业PMI 49.2，前值 50.3」正是以正文形式发出来的。
    title = str(item.get("title") or "").strip()
    text = str(item.get("content_text") or "").strip()
    if not title:
        title = text
    if not title:
        return None
    return NewsDoc(
        title=title[:200],
        summary=text[:500] if text and text != title else "",
        source="华尔街见闻",
        published_at=_stamp(item.get("display_time")),
        url=str(item.get("uri") or ""),
    )


def _stamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
