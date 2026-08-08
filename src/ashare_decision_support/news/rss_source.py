"""Small, dependency-free reader for curated official RSS/Atom feeds."""

from __future__ import annotations

import email.utils
import urllib.request
import xml.etree.ElementTree as element_tree
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ashare_decision_support.news.balance import balance_by_source
from ashare_decision_support.news.base import NewsDoc, NewsSource

USER_AGENT = "Mozilla/5.0"
TIMEOUT_SECONDS = 15
PER_FEED_LIMIT = 30
MAX_RESPONSE_BYTES = 1_000_000
CN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class RssFeed:
    name: str
    url: str
    source: str


OFFICIAL_GLOBAL_FEEDS: tuple[RssFeed, ...] = (
    RssFeed("美联储", "https://www.federalreserve.gov/feeds/press_all.xml", "美联储"),
    RssFeed("美国劳工统计局", "https://www.bls.gov/feed/bls_latest.rss", "美国劳工统计局"),
    RssFeed("美国能源信息署", "https://www.eia.gov/rss/todayinenergy.xml", "美国能源信息署"),
)


class RssNewsSource(NewsSource):
    """Curated feeds with the same partial-failure contract as other sources."""

    name = "official_global"

    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
        timeout: int = TIMEOUT_SECONDS,
        feeds: tuple[RssFeed, ...] = OFFICIAL_GLOBAL_FEEDS,
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.feeds = feeds
        self.last_market_stats: dict[str, object] = {}

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        docs: list[NewsDoc] = []
        notes: list[str] = []
        raw_by_source: dict[str, int] = {}
        failed: list[str] = []
        for feed in self.feeds:
            try:
                rows = self._fetch_feed(feed)
            except Exception:
                failed.append(feed.name)
                continue
            raw_by_source[feed.source] = len(rows)
            docs.extend(rows)

        if failed:
            notes.append(f"海外官方信息源 {'、'.join(failed)} 不可用，本次跳过")
        if not docs:
            notes.append("海外官方宏观信息源未返回可用条目")
            return [], notes
        retained, retained_by_source = balance_by_source(docs, limit)
        self.last_market_stats = {
            "raw_doc_count": len(docs),
            "retained_doc_count": len(retained),
            "raw_by_source": dict(sorted(raw_by_source.items())),
            "retained_by_source": retained_by_source,
        }
        return retained, notes

    def fetch_for_symbols(
        self, symbols: list[str], limit: int = 10
    ) -> tuple[dict[str, list[NewsDoc]], list[str]]:
        return {}, []

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
    return parsed.astimezone(CN_TZ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
