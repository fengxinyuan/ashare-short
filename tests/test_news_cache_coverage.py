"""缓存对四路源一视同仁：重跑读的是同一批文档，而不是同一段网络。

`news/cache.py` 开头那句承诺——「两次跑读到同一份文档」——此前只对 akshare
那一路成立。这些用例守的是另外三路，以及缓存**不该**生效的那两种情况：
挂掉的一路不能污染缓存，翻了一半的一路不能被记住。
"""

from __future__ import annotations

import json
from datetime import date

from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.news.cache import NewsCache
from ashare_decision_support.news.official_source import OfficialListing, OfficialNewsSource
from ashare_decision_support.news.rss_source import RssFeed, RssNewsSource
from ashare_decision_support.news.wallstreet_source import WallstreetNewsSource

TRADE_DATE = date(2026, 8, 8)

PAGE = """
<ul><li><a href="/policy/1.html">国务院发布扩大内需行动方案</a> 2026-08-07</li></ul>
"""
RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Federal Reserve issues statement</title><description>Policy decision.</description>
<link>https://fed.example/release</link><pubDate>Thu, 07 Aug 2026 18:00:00 GMT</pubDate></item>
</channel></rss>"""


class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, size: int = -1) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CountingHttp:
    """记下每个 URL 被打了几次；`dead` 里的直接抛。"""

    def __init__(self, pages: dict[str, str], dead: set[str] | None = None) -> None:
        self.pages = pages
        self.dead = dead or set()
        self.calls: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        if url in self.dead:
            raise OSError("down")
        return _Resp(self.pages[url].encode("utf-8"))


def test_official_listing_is_read_from_cache_on_the_second_run(tmp_path) -> None:
    listing = OfficialListing("国务院政策", "https://official.example/list", "国务院")
    http = CountingHttp({listing.url: PAGE})
    cache = NewsCache(tmp_path)

    def build() -> OfficialNewsSource:
        return OfficialNewsSource(opener=http, listings=(listing,), cache=cache)

    first, _ = build().fetch_market(TRADE_DATE)
    second, _ = build().fetch_market(TRADE_DATE)

    assert http.calls == [listing.url]
    assert [doc.title for doc in first] == [doc.title for doc in second]
    assert first == second


def test_rss_feed_is_read_from_cache_on_the_second_run(tmp_path) -> None:
    feed = RssFeed("美联储", "https://fed.example/feed.xml", "美联储")
    http = CountingHttp({feed.url: RSS})
    cache = NewsCache(tmp_path)

    first, _ = RssNewsSource(opener=http, feeds=(feed,), cache=cache).fetch_market(TRADE_DATE)
    second, _ = RssNewsSource(opener=http, feeds=(feed,), cache=cache).fetch_market(TRADE_DATE)

    assert http.calls == [feed.url]
    assert first == second


def test_a_dead_listing_does_not_stop_the_live_one_from_caching(tmp_path) -> None:
    """一路挂掉时另一路照常进缓存，下次重跑只重试挂掉的那一路。

    按栏目分键就是为了这个。整源一个键的话，任何一路超时都会让另外两路已经取回
    的内容一起失效——而官方栏目挂一路是常态，不是异常。
    """
    live = OfficialListing("国务院政策", "https://official.example/live", "国务院")
    dead = OfficialListing("中国证监会", "https://official.example/dead", "中国证监会")
    http = CountingHttp({live.url: PAGE}, dead={dead.url})
    cache = NewsCache(tmp_path)

    def run() -> tuple[list[NewsDoc], list[str]]:
        return OfficialNewsSource(
            opener=http, listings=(live, dead), cache=cache
        ).fetch_market(TRADE_DATE)

    first, first_notes = run()
    second, _ = run()

    assert any("中国证监会" in note and "OSError" in note for note in first_notes)
    # 活着的那一路只打了一次网络，挂掉的那一路两次都重试了。
    assert http.calls.count(live.url) == 1
    assert http.calls.count(dead.url) == 2
    assert first == second


class PagingHttp:
    """按 cursor 逐页返回；`fail_after` 页之后开始抛。"""

    def __init__(self, pages: list[list[dict]], fail_after: int | None = None) -> None:
        self.pages = pages
        self.fail_after = fail_after
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise OSError("down")
        url = request.full_url
        index = int(url.split("cursor=")[1]) if "cursor=" in url else 0
        items = self.pages[index] if index < len(self.pages) else []
        nxt = str(index + 1) if index + 1 < len(self.pages) else None
        body = json.dumps({"data": {"items": items, "next_cursor": nxt}}, ensure_ascii=False)
        return _Resp(body.encode("utf-8"))


def _item(title: str) -> dict:
    # 2026-08-08 09:00 CST，落在 72 小时回溯窗内，翻页不会被 cutoff 提前截断。
    return {"display_time": 1_786_150_800, "title": title, "content_text": ""}


def test_wallstreet_channel_is_cached_once_fully_paged(tmp_path) -> None:
    http = PagingHttp([[_item("第一页")], [_item("第二页")]])
    cache = NewsCache(tmp_path)

    def run():
        return WallstreetNewsSource(
            opener=http, channels=("global-channel",), cache=cache
        ).fetch_market(TRADE_DATE)

    first, _ = run()
    calls_after_first = http.calls
    second, _ = run()

    assert calls_after_first > 1  # 确实翻了页
    assert http.calls == calls_after_first  # 第二次一个请求都没发
    assert first == second


def test_a_half_paged_channel_is_not_remembered(tmp_path) -> None:
    """翻了一半就断的那次不进缓存——半截值得用，不值得记住。

    写进去的代价是后面每一次重跑都只看到半截宏观快讯，而且看不出来：
    简报上它和「今天就这么多」长得一模一样。
    """
    http = PagingHttp([[_item("第一页")], [_item("第二页")]], fail_after=1)
    cache = NewsCache(tmp_path)

    def run():
        return WallstreetNewsSource(
            opener=http, channels=("global-channel",), cache=cache
        ).fetch_market(TRADE_DATE)

    first, _ = run()
    assert [doc.title for doc in first] == ["第一页"]

    # 缓存里不该有东西，所以第二次必须重新打网络。
    before = http.calls
    run()
    assert http.calls > before


def test_cached_docs_round_trip_every_field(tmp_path) -> None:
    """`symbols` 存进 JSON 会变成 list，取回来必须还是 tuple。

    `NewsDoc` 是 frozen 且要可哈希的——还成 list 的话，下游按文档去重的地方
    只在**缓存命中时**才炸，也就是第二次跑才炸。
    """
    cache = NewsCache(tmp_path)
    doc = NewsDoc(
        title="标题",
        summary="摘要",
        source="财联社",
        published_at="2026-08-08 09:00",
        url="https://example/1",
        symbols=("600519.SH",),
        weight=0.75,
        engagement=12.0,
        heat_rank=3,
    )

    cache.write_docs(TRADE_DATE, "probe", [doc])
    restored = cache.read_docs(TRADE_DATE, "probe")

    assert restored == [doc]
    assert isinstance(restored[0].symbols, tuple)
    assert {restored[0]}  # 可哈希


def test_unreadable_cache_is_treated_as_no_cache(tmp_path) -> None:
    """字段对不上就当没缓存——重新打一次网络永远是安全的退路。"""
    cache = NewsCache(tmp_path)
    cache.write(TRADE_DATE, "probe", [{"title": "旧格式", "已删除的字段": 1}])

    assert cache.read_docs(TRADE_DATE, "probe") is None
