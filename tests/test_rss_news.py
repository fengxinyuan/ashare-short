"""Curated overseas official RSS feeds."""

from __future__ import annotations

from datetime import date

from ashare_decision_support.news.rss_source import RssFeed, RssNewsSource

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Federal Reserve issues statement</title><description>Policy decision.</description>
<link>https://fed.example/release</link><pubDate>Thu, 07 Aug 2026 18:00:00 GMT</pubDate></item>
</channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>EIA weekly petroleum report</title><summary>Crude inventories.</summary>
<link href="https://eia.example/report"/><updated>2026-08-07T15:30:00Z</updated></entry>
</feed>"""


class Response:
    def __init__(self, body: str) -> None:
        self.body = body.encode("utf-8")

    def read(self, size: int = -1) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Http:
    def __init__(self, pages: dict[str, str], dead: set[str] | None = None) -> None:
        self.pages = pages
        self.dead = dead or set()

    def __call__(self, request, timeout=None):
        url = request.full_url
        if url in self.dead:
            raise OSError("down")
        return Response(self.pages[url])


def test_rss_and_atom_are_normalized_to_news_docs() -> None:
    fed = RssFeed("美联储", "https://fed.example/rss", "美联储")
    eia = RssFeed("EIA", "https://eia.example/rss", "美国能源信息署")
    source = RssNewsSource(opener=Http({fed.url: RSS, eia.url: ATOM}), feeds=(fed, eia))

    docs, notes = source.fetch_market(date(2026, 8, 8))

    assert notes == []
    assert {doc.source for doc in docs} == {"美联储", "美国能源信息署"}
    assert any(doc.published_at == "2026-08-08 02:00:00" for doc in docs)
    assert any(doc.url == "https://eia.example/report" for doc in docs)


def test_a_dead_feed_does_not_drop_live_foreign_sources() -> None:
    fed = RssFeed("美联储", "https://fed.example/rss", "美联储")
    bls = RssFeed("BLS", "https://bls.example/rss", "美国劳工统计局")
    source = RssNewsSource(opener=Http({fed.url: RSS}, {bls.url}), feeds=(fed, bls))

    docs, notes = source.fetch_market(date(2026, 8, 8))

    assert len(docs) == 1
    assert any("BLS" in note for note in notes)


def test_default_feeds_cover_inflation_after_bls_is_unreachable() -> None:
    """BLS 本机 403，就业与通胀整块会跟着没有——除非有别的官方口径顶上。

    这条钉的不是「有几个 feed」，是**那条推理链的输入端还在不在**：PMI/通胀
    破位直接对应「宽松预期升温 → 红利/消费占优、顺周期走弱」，而议息真正盯的
    通胀口径是 PCE，它在经济分析局那一路（Personal Income and Outlays），不在
    BLS。有人删掉 BEA 时，症状不是报错，是风格切换的判断悄悄没了依据。
    """
    from ashare_decision_support.news.rss_source import OFFICIAL_GLOBAL_FEEDS

    sources = {feed.source for feed in OFFICIAL_GLOBAL_FEEDS}
    assert "美国经济分析局" in sources     # GDP / PCE / 贸易差额
    assert "美国人口普查局" in sources     # 零售 / 耐用品 / 库存
    assert "美联储" in sources             # 议息与声明
    # 取不到不等于不存在：换个部署环境 BLS 就回来了，不能因为本机 403 就删掉。
    assert "美国劳工统计局" in sources


def test_one_dead_feed_costs_only_its_own_documents() -> None:
    """BLS 那一路今天就是这个状态：它挂掉不能带走另外四路。"""
    live = RssFeed("美国经济分析局", "https://bea.example/rss", "美国经济分析局")
    dead = RssFeed("美国劳工统计局", "https://bls.example/rss", "美国劳工统计局")
    source = RssNewsSource(
        opener=Http({live.url: RSS}, dead={dead.url}), feeds=(live, dead)
    )

    docs, notes = source.fetch_market(date(2026, 8, 8))

    assert len(docs) == 1
    assert any("美国劳工统计局" in note for note in notes)
