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
