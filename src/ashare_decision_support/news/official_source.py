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

from ashare_decision_support.fanout import gather
from ashare_decision_support.news.balance import retain_market_docs
from ashare_decision_support.news.base import NewsDoc, NewsSource
from ashare_decision_support.news.cache import NewsCache
from ashare_decision_support.news.health import probe_feeds

USER_AGENT = "Mozilla/5.0"
TIMEOUT_SECONDS = 15
PER_LISTING_LIMIT = 30

# 标题长度上限。这三个栏目里**真实**标题实测 12–63 字（最长的一条是证监会那种
# 「就《…》答记者问」的长标题），而漏进来的站点导航与页脚是 117–1121 字。中间
# 这段空当足够宽，所以这里不是在调参，是在两类东西之间划线。
#
# 为什么需要这条而不是只靠去脚本：`<script>` 清掉之后，剩下的还有「首页 | 简 |
# 繁 | EN | 登录…」这类**纯文本**导航条和版权页脚。它们带日期、带链接，长得和
# 一条公告没有任何结构差别，只有长度分得开。
MAX_TITLE_CHARS = 90

# 导航条的三个特征，按实测反例定，不是按想象定。触发任意一条就丢弃：
#
# - `更多>>`／`>>`：栏目跳转标记。反例「更多>> 国务院公报 政策解读 新华时评：…」
#   ——它把三个栏目名和一条真标题粘成了一条，长度只有 35 字，长度上限拦不住。
# - 内嵌 `http`：锚文本里出现整条 URL，只有站点导航会这样。
# - **三个以上**竖线：导航条是「首页 | 简 | 繁 | EN | 登录…」。**不能见竖线就丢**
#   ——真标题里有竖线，例如「国务院常务会议解读 | 住房公积金将更好满足居民多样化
#   住房消费需求」。一条是分隔真标题的破折号用法，四条以上才是导航。
_NAV_MARKERS = (">>", "http")
MAX_NAV_PIPES = 2


@dataclass(frozen=True)
class OfficialListing:
    name: str
    url: str
    source: str


DEFAULT_LISTINGS: tuple[OfficialListing, ...] = (
    # `zhengce/zuixin.htm` 已经变成一个 meta-refresh 跳转壳子：**HTTP 200、没有
    # 异常、正文里一条带日期的链接都没有**。也就是说这一路会被报成「可达」而
    # 一条不出——`health_check` 加上去当天就是这么发现的。跳转目标 `zuixin/`
    # 只有 12 条且混着站点导航，`zhengce/` 才是今天真正在更新的那一版。
    OfficialListing("国务院政策", "https://www.gov.cn/zhengce/", "国务院"),
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
# 脚本与样式要**连内容一起**去掉，不能只去标签。`_TAGS` 删的是 `<script>` 和
# `</script>` 这两个尖括号，中间那段 JS 是纯文本，会原样留下来——实测央行页面
# 因此产出过一条 1121 字的「标题」，内容是 `var wa='RSS订阅'; document.write(wa)`。
# 那种东西一旦进语料，词典打分会照常在里面找多空词。
_SCRIPTS = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
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
        cache: NewsCache | None = None,
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.listings = listings
        self.cache = cache
        self.last_market_stats: dict[str, object] = {}

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        docs: list[NewsDoc] = []
        notes: list[str] = []
        raw_by_source: dict[str, int] = {}

        # 三个栏目是三个独立的网络往返，串行等于把三次超时相加。
        fetched, unreachable = gather(
            self.listings, lambda listing: self._listing_docs(trade_date, listing)
        )
        for listing, rows in fetched:
            raw_by_source[listing.source] = len(rows)
            docs.extend(rows)

        if unreachable:
            notes.append(
                f"官方信息源 {'、'.join(f'{item.name}（{type(exc).__name__}）' for item, exc in unreachable)}"
                " 不可用，本次跳过"
            )
        if not docs:
            notes.append("官方政策与监管信息源未返回可用条目")
            return [], notes

        retained, self.last_market_stats = retain_market_docs(docs, limit, raw_by_source)
        return retained, notes

    def fetch_for_symbols(
        self, symbols: list[str], limit: int = 10
    ) -> tuple[dict[str, list[NewsDoc]], list[str]]:
        return {}, []

    def health_check(self) -> dict[str, Any]:
        return probe_feeds(
            "official_cn", self.listings, lambda listing: listing.name, self._fetch_listing
        )

    def _listing_docs(self, trade_date: date, listing: OfficialListing) -> list[NewsDoc]:
        """一个栏目一个缓存键，取不到才打网络。

        按栏目而不是按整源缓存：三个栏目分属三个机构、各自会挂。整源一个键的话，
        一路超时就会让另外两路已经取回的内容也一起失效，下次重跑再全打一遍。
        """
        key = f"official_{listing.source}"
        if self.cache is not None:
            cached = self.cache.read_docs(trade_date, key)
            if cached is not None:
                return cached
        docs = self._fetch_listing(listing)
        if self.cache is not None:
            self.cache.write_docs(trade_date, key, docs)
        return docs

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
    page = _SCRIPTS.sub(" ", page)
    for match in _LINK_WITH_DATE.finditer(page):
        title = _plain(match.group("title"))
        published_at = match.group("date").replace("/", "-").replace(".", "-")
        url = urljoin(base_url, html.unescape(match.group("href")).strip())
        # Navigation labels and breadcrumbs have no analytical value. A dated
        # headline is long enough to be a real publication in these listings —
        # and short enough not to be the site's nav bar or copyright footer,
        # both of which also carry a date and a link. See `MAX_TITLE_CHARS`.
        if not 8 <= len(title) <= MAX_TITLE_CHARS:
            continue
        if not url.startswith(("http://", "https://")):
            continue
        if _is_navigation(title):
            continue
        key = (title, url)
        if key not in seen:
            seen.add(key)
            result.append((title, published_at, url))
    return result


def _is_navigation(title: str) -> bool:
    """锚文本吞掉了同级导航标签时为真。判据与反例见 `_NAV_MARKERS`。"""
    if any(marker in title for marker in _NAV_MARKERS):
        return True
    return title.count("|") + title.count("｜") > MAX_NAV_PIPES


def _plain(value: str) -> str:
    return _SPACE.sub(" ", html.unescape(_TAGS.sub("", value))).strip()
