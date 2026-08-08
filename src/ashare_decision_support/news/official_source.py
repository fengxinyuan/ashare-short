"""Official Chinese macro and regulatory releases.

Commercial flashes are useful for speed, but a policy or statistical release
must retain an official, independently verifiable origin. This source reads
the public listing pages of the State Council, PBOC, and CSRC. It deliberately
collects only headlines and canonical links: interpretation remains in the
existing classifier / daily-read layers.
"""

from __future__ import annotations

import html
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urljoin

from ashare_decision_support.news.balance import balance_by_source
from ashare_decision_support.news.base import NewsDoc, NewsSource

USER_AGENT = "Mozilla/5.0"
TIMEOUT_SECONDS = 15
PER_LISTING_LIMIT = 30


@dataclass(frozen=True)
class OfficialListing:
    name: str
    url: str
    source: str


DEFAULT_LISTINGS: tuple[OfficialListing, ...] = (
    OfficialListing("国务院政策", "https://www.gov.cn/zhengce/zuixin.htm", "国务院"),
    OfficialListing(
        "中国人民银行", "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html", "中国人民银行"
    ),
    OfficialListing(
        "中国证监会", "http://www.csrc.gov.cn/csrc/c100028/common_list.shtml", "中国证监会"
    ),
)

# Listing pages use several different templates. Match an anchor followed by
# a nearby date, then normalize the markup instead of binding to page classes.
_LINK_WITH_DATE = re.compile(
    r'<a\b[^>]*\bhref=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<title>.*?)</a>'
    r'.{0,500}?(?P<date>20\d{2}[./-]\d{1,2}[./-]\d{1,2})',
    re.IGNORECASE | re.DOTALL,
)
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


class OfficialNewsSource(NewsSource):
    """Public policy and regulatory listings, each independently degradable."""

    name = "official_cn"

    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
        timeout: int = TIMEOUT_SECONDS,
        listings: tuple[OfficialListing, ...] = DEFAULT_LISTINGS,
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.listings = listings
        self.last_market_stats: dict[str, object] = {}

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        docs: list[NewsDoc] = []
        notes: list[str] = []
        raw_by_source: dict[str, int] = {}
        failed: list[str] = []

        for listing in self.listings:
            try:
                rows = self._fetch_listing(listing)
            except Exception:
                failed.append(listing.name)
                continue
            raw_by_source[listing.source] = len(rows)
            docs.extend(rows)

        if failed:
            notes.append(f"官方信息源 {'、'.join(failed)} 不可用，本次跳过")
        if not docs:
            notes.append("官方政策与监管信息源未返回可用条目")
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

    def _fetch_listing(self, listing: OfficialListing) -> list[NewsDoc]:
        request = urllib.request.Request(listing.url, headers={"User-Agent": USER_AGENT})
        with self._opener(request, timeout=self.timeout) as response:
            page = response.read().decode("utf-8", "ignore")
        return [
            NewsDoc(title=title, source=listing.source, published_at=published_at, url=url)
            for title, published_at, url in _listing_items(page, listing.url)[:PER_LISTING_LIMIT]
        ]


def _listing_items(page: str, base_url: str) -> list[tuple[str, str, str]]:
    """Return unique, dated headline links from an official HTML listing."""
    result: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in _LINK_WITH_DATE.finditer(page):
        title = _plain(match.group("title"))
        published_at = match.group("date").replace("/", "-").replace(".", "-")
        url = urljoin(base_url, html.unescape(match.group("href")).strip())
        # Navigation labels and breadcrumbs have no analytical value. A dated
        # headline is long enough to be a real publication in these listings.
        if len(title) < 8 or not url.startswith(("http://", "https://")):
            continue
        key = (title, url)
        if key not in seen:
            seen.add(key)
            result.append((title, published_at, url))
    return result


def _plain(value: str) -> str:
    return _SPACE.sub(" ", html.unescape(_TAGS.sub("", value))).strip()
