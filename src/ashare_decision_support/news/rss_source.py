"""Small, dependency-free reader for curated official RSS/Atom feeds."""

from __future__ import annotations

import email.utils
import urllib.request
import xml.etree.ElementTree as element_tree
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ashare_decision_support.news.balance import collect_by_feed
from ashare_decision_support.news.base import NewsDoc, NewsSource
from ashare_decision_support.news.cache import NewsCache, cached_docs
from ashare_decision_support.news.health import probe_feeds
from ashare_decision_support.schedule.timezone import to_beijing_naive

USER_AGENT = "Mozilla/5.0"
TIMEOUT_SECONDS = 15
PER_FEED_LIMIT = 30
MAX_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class RssFeed:
    name: str
    url: str
    source: str


OFFICIAL_GLOBAL_FEEDS: tuple[RssFeed, ...] = (
    RssFeed("美联储", "https://www.federalreserve.gov/feeds/press_all.xml", "美联储"),
    # 保留在册但**本机 403**（CDN 层拦的，换 UA 无效，`api.bls.gov` 同样 503）。
    # 不删是因为「取不到」和「不存在」是两回事——换个部署环境它就回来了，这条
    # 分界线是 `DATA_SOURCES.md` §三 一直在守的。并发之后它只花 0.25s 就失败，
    # 不占墙钟时间，代价只有 note 里的一行。
    RssFeed("美国劳工统计局", "https://www.bls.gov/feed/bls_latest.rss", "美国劳工统计局"),
    RssFeed("美国能源信息署", "https://www.eia.gov/rss/todayinenergy.xml", "美国能源信息署"),
    # BLS 一挂，**就业与通胀**整块就没了，而这恰好是「宽松预期 → 风格切换」那条
    # 推理链的输入端。这两路补的不是同一个数，但补的是同一个判断：
    #
    # - 经济分析局：GDP、**个人收入与支出（PCE，美联储真正盯的通胀口径）**、贸易差额
    # - 人口普查局：零售、耐用品、批发库存、新屋销售
    #
    # 拿 PCE 代替 CPI 在这条链路上不是退而求其次——议息决策看的本来就是 PCE。
    # 非农仍然没有第二个官方来源，这一块只能靠华尔街见闻那一路的中文快讯兜。
    RssFeed("美国经济分析局", "https://apps.bea.gov/rss/rss.xml", "美国经济分析局"),
    RssFeed(
        "美国人口普查局", "https://www.census.gov/economic-indicators/indicator.xml", "美国人口普查局"
    ),
)


class RssNewsSource(NewsSource):
    """Curated feeds with the same partial-failure contract as other sources."""

    name = "official_global"

    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
        timeout: int = TIMEOUT_SECONDS,
        feeds: tuple[RssFeed, ...] = OFFICIAL_GLOBAL_FEEDS,
        cache: NewsCache | None = None,
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.feeds = feeds
        self.cache = cache
        self.last_market_stats: dict[str, object] = {}

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        # 每个 feed 一个独立往返，且都在盘前链路上——并发跑，慢的那个决定这一段。
        retained, notes, stats = collect_by_feed(
            self.feeds,
            lambda feed: self._feed_docs(trade_date, feed),
            limit=limit,
            unreachable_note="海外官方信息源 {names} 不可用，本次跳过",
            empty_note="海外官方宏观信息源未返回可用条目",
        )
        if stats:
            self.last_market_stats = stats
        return retained, notes

    def fetch_for_symbols(
        self, symbols: list[str], limit: int = 10
    ) -> tuple[dict[str, list[NewsDoc]], list[str]]:
        return {}, []

    def health_check(self) -> dict[str, Any]:
        return probe_feeds("official_global", self.feeds, lambda f: f.name, self._fetch_feed)

    def _feed_docs(self, trade_date: date, feed: RssFeed) -> list[NewsDoc]:
        return cached_docs(
            self.cache, trade_date, f"rss_{feed.source}", lambda: self._fetch_feed(feed)
        )

    def _fetch_feed(self, feed: RssFeed) -> list[NewsDoc]:
        request = urllib.request.Request(feed.url, headers={"User-Agent": USER_AGENT})
        with self._opener(request, timeout=self.timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES)
        # Curated, fixed HTTPS feeds and a strict size cap keep this stdlib-only
        # adapter bounded. The source is independently guarded at feed level.
        root = element_tree.fromstring(payload)  # noqa: S314
        return [
            NewsDoc(title=title, summary=summary, source=feed.source, published_at=published_at, url=url)
            for title, summary, published_at, url in _rss_items(root)[:PER_FEED_LIMIT]
        ]


def _rss_items(root: element_tree.Element) -> list[tuple[str, str, str, str]]:
    """Normalize RSS and Atom entries without depending on namespace prefixes."""
    rows: list[tuple[str, str, str, str]] = []
    for item in root.findall(".//item") + root.findall(".//{*}entry"):
        title = _child_text(item, "title")
        if not title:
            continue
        summary = _child_text(item, "description") or _child_text(item, "summary")
        stamp = _child_text(item, "pubDate") or _child_text(item, "published") or _child_text(item, "updated")
        link = _child_text(item, "link")
        if not link:
            for node in item.findall("{*}link"):
                link = (node.attrib.get("href") or "").strip()
                if link:
                    break
        rows.append((title, summary, _rss_stamp(stamp), link))
    return rows


def _child_text(item: element_tree.Element, name: str) -> str:
    node = item.find(name)
    if node is None:
        node = item.find(f"{{*}}{name}")
    return (node.text or "").strip() if node is not None else ""


def _rss_stamp(value: str) -> str:
    """Convert UTC feed timestamps to the exchange clock used by recency weighting."""
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value[:19]
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    return to_beijing_naive(parsed).strftime("%Y-%m-%d %H:%M:%S")
