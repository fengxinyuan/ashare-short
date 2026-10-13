"""2026-09-13 证监会公开响应的精简样本，防止重新接回 2021 年静态旧页。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ashare_decision_support.news.cache import NewsCache
from ashare_decision_support.news.official_source import (
    DEFAULT_LISTINGS,
    OfficialListing,
    OfficialNewsSource,
)
from tests.fakes import PageOpener

CSRC_URL = (
    "https://www.csrc.gov.cn/searchList/a1a078ee0bc54721ab6b148884c784a8"
    "?_isAgg=true&_isJson=true&_pageSize=30&_template=index"
    "&_rangeTimeGte=&_channelName=&page=1"
)
FIXTURE = Path(__file__).parent / "fixtures" / "csrc_news_20260913.json"
DAY = date(2026, 9, 13)


def test_default_csrc_source_reads_current_releases_with_full_timestamps(tmp_path):
    feed = next(item for item in DEFAULT_LISTINGS if item.source == "中国证监会")
    http = PageOpener({CSRC_URL: FIXTURE.read_text(encoding="utf-8")})
    source = OfficialNewsSource(opener=http, listings=(feed,), cache=NewsCache(tmp_path))

    first, notes = source.fetch_market(DAY)
    second, _ = source.fetch_market(DAY)

    assert notes == []
    assert len(first) == 2
    assert first[0].title == "中国证监会发布《期货公司监督管理办法》及配套实施公告"
    assert first[0].published_at == "2026-09-11 18:03:38"
    assert first[0].url == "https://www.csrc.gov.cn/csrc/c100028/c7658236/content.shtml"
    assert first == second
    assert http.calls == [CSRC_URL]


@pytest.mark.parametrize("payload", [None, {}, {"data": {"results": "schema changed"}}])
def test_invalid_csrc_json_preserves_other_official_sources_and_can_retry(tmp_path, payload):
    csrc = next(item for item in DEFAULT_LISTINGS if item.source == "中国证监会")
    gov = OfficialListing("国务院", "https://official.example/list", "国务院")
    http = PageOpener(
        {
            CSRC_URL: json.dumps(payload),
            gov.url: '<a href="/release">国务院发布扩大内需行动方案</a> 2026-09-11',
        }
    )
    source = OfficialNewsSource(opener=http, listings=(gov, csrc), cache=NewsCache(tmp_path))

    first, notes = source.fetch_market(DAY)
    assert [doc.source for doc in first] == ["国务院"]
    assert any("中国证监会" in note and "不可用" in note for note in notes)

    http.pages[CSRC_URL] = FIXTURE.read_text(encoding="utf-8")
    second, _ = source.fetch_market(DAY)
    assert {doc.source for doc in second} == {"国务院", "中国证监会"}
    assert http.calls.count(CSRC_URL) == 2
    assert http.calls.count(gov.url) == 1
