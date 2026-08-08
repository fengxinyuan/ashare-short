"""Official policy / regulatory listing source."""

from __future__ import annotations

from datetime import date

from ashare_decision_support.news.official_source import OfficialListing, OfficialNewsSource


class Response:
    def __init__(self, body: str) -> None:
        self.body = body.encode("utf-8")

    def read(self) -> bytes:
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


PAGE = """
<ul>
  <li><a href="/policy/1.html"><span>国务院发布扩大内需行动方案</span></a><span>2026-08-07</span></li>
  <li><a href="/policy/1.html">国务院发布扩大内需行动方案</a> 2026/08/07</li>
  <li><a href="/nav.html">政策</a> 2026-08-07</li>
</ul>
"""


def test_listing_maps_dated_headlines_with_canonical_urls() -> None:
    listing = OfficialListing("国务院政策", "https://official.example/list", "国务院")
    source = OfficialNewsSource(opener=Http({listing.url: PAGE}), listings=(listing,))

    docs, notes = source.fetch_market(date(2026, 8, 8))

    assert notes == []
    assert len(docs) == 1
    assert docs[0].title == "国务院发布扩大内需行动方案"
    assert docs[0].published_at == "2026-08-07"
    assert docs[0].url == "https://official.example/policy/1.html"
    assert docs[0].source == "国务院"


def test_one_failed_official_listing_keeps_the_others() -> None:
    live = OfficialListing("国务院政策", "https://official.example/live", "国务院")
    dead = OfficialListing("中国证监会", "https://official.example/dead", "中国证监会")
    source = OfficialNewsSource(
        opener=Http({live.url: PAGE}, dead={dead.url}), listings=(live, dead)
    )

    docs, notes = source.fetch_market(date(2026, 8, 8))

    assert len(docs) == 1
    assert any("中国证监会" in note for note in notes)


def test_source_balances_each_official_publisher() -> None:
    pbc = OfficialListing("中国人民银行", "https://official.example/pbc", "中国人民银行")
    csrc = OfficialListing("中国证监会", "https://official.example/csrc", "中国证监会")
    many = "".join(
        f'<a href="/{i}.html">中国人民银行第{i}项公开市场操作公告</a>2026-08-07'
        for i in range(5)
    )
    one = '<a href="/a.html">证监会发布上市公司监管规则适用指引</a>2026-08-07'
    source = OfficialNewsSource(
        opener=Http({pbc.url: many, csrc.url: one}), listings=(pbc, csrc)
    )

    docs, _ = source.fetch_market(date(2026, 8, 8), limit=3)

    assert len(docs) == 3
    assert sum(doc.source == "中国证监会" for doc in docs) == 1
