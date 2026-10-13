"""新闻缓存故障、时区和分页缺口不得静默改变盘前输入。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.news.cache import NewsCache
from ashare_decision_support.news.dedup import parse_published, weigh_docs
from ashare_decision_support.news.official_source import OfficialListing, OfficialNewsSource
from ashare_decision_support.news.wallstreet_source import WallstreetNewsSource
from ashare_decision_support.schedule.timezone import BEIJING_TZ
from tests.fakes import FakeResponse, PageOpener
from tests.test_news_cache_coverage import PagingHttp

DAY = date(2026, 8, 8)


@pytest.mark.parametrize("payload", [None, [], 1, "not an object"])
def test_news_cache_wrong_top_level_is_a_cache_miss(tmp_path, payload):
    cache = NewsCache(tmp_path)
    path = cache.path_for(DAY, "feed")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cache.read(DAY, "feed") is None


@pytest.mark.parametrize(
    "stamp",
    ["2026-08-08T08:00:00", "not a timestamp", "2999-08-08T08:00:00+00:00"],
)
def test_news_cache_invalid_or_future_clock_is_a_cache_miss(tmp_path, stamp):
    cache = NewsCache(tmp_path)
    path = cache.path_for(DAY, "feed")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"cached_at": stamp, "data": ["old"]}), encoding="utf-8")

    assert cache.read(DAY, "feed") is None


def test_news_cache_invalid_encoding_is_a_cache_miss(tmp_path):
    cache = NewsCache(tmp_path)
    path = cache.path_for(DAY, "feed")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe")

    assert cache.read_docs(DAY, "feed") is None


def test_official_feed_refetches_after_cache_corruption(tmp_path):
    listing = OfficialListing("国务院", "https://official.example/list", "国务院")
    http = PageOpener({listing.url: '<a href="/release">国务院发布扩大内需行动方案</a> 2026-08-08'})
    cache = NewsCache(tmp_path)
    path = cache.path_for(DAY, f"{OfficialNewsSource.cache_prefix}_国务院")
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")
    source = OfficialNewsSource(opener=http, listings=(listing,), cache=cache)

    docs, notes = source.fetch_market(DAY)

    assert [doc.title for doc in docs] == ["国务院发布扩大内需行动方案"]
    assert http.calls == [listing.url]
    assert not any("不可用" in note for note in notes)
    assert cache.read_docs(DAY, f"{OfficialNewsSource.cache_prefix}_国务院") == docs


def test_news_cache_write_failure_keeps_last_complete_snapshot(tmp_path, monkeypatch):
    cache = NewsCache(tmp_path)
    assert cache.write(DAY, "feed", [{"title": "last complete snapshot"}])
    path = cache.path_for(DAY, "feed")
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk full during atomic replacement")

    monkeypatch.setattr("ashare_decision_support.state.store.os.replace", fail)

    assert cache.write(DAY, "feed", [{"title": "new snapshot"}]) is False
    assert path.read_bytes() == before


def test_news_cache_rejects_nonfinite_data_without_destroying_previous_file(tmp_path):
    cache = NewsCache(tmp_path)
    assert cache.write(DAY, "feed", [{"title": "good"}])

    assert cache.write(DAY, "feed", [{"weight": float("nan")}]) is False
    assert cache.read(DAY, "feed") == [{"title": "good"}]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-08T00:30:00Z", datetime(2026, 8, 8, 8, 30)),
        ("2026-08-08T00:30:00-04:00", datetime(2026, 8, 8, 12, 30)),
        ("2026-08-07T20:30:00+00:00", datetime(2026, 8, 8, 4, 30)),
        ("2026-08-08T08:30:00.123+08:00", datetime(2026, 8, 8, 8, 30)),
    ],
)
def test_news_timestamps_use_the_exchange_clock(raw, expected):
    assert parse_published(raw) == expected


def test_same_instant_has_same_news_weight_across_timezones():
    docs = [
        NewsDoc(title="海外宏观数据发布", published_at="2026-08-08T00:30:00Z"),
        NewsDoc(title="国内政策数据发布", published_at="2026-08-08 08:30:00"),
    ]
    weighted, _ = weigh_docs(docs, datetime(2026, 8, 8, 20, 30))

    assert [doc.weight for doc in weighted] == [0.5, 0.5]


def test_news_weight_accepts_an_aware_observation_time():
    docs = [NewsDoc(title="新闻标题", published_at="2026-08-08 08:30:00")]
    weighted, _ = weigh_docs(docs, datetime(2026, 8, 8, 12, 30, tzinfo=UTC))

    assert weighted[0].weight == 0.5


def _flash(title, timestamp=1_786_150_800):
    return {"title": title, "display_time": timestamp, "content_text": ""}


def test_page_cap_keeps_partial_news_but_does_not_cache_it_as_complete(tmp_path):
    http = PagingHttp([[_flash("第一页")], [_flash("第二页")]])
    cache = NewsCache(tmp_path)
    first, notes = WallstreetNewsSource(
        opener=http, channels=("global-channel",), cache=cache, max_pages=1
    ).fetch_market(DAY)
    second, _ = WallstreetNewsSource(
        opener=http, channels=("global-channel",), cache=cache, max_pages=3
    ).fetch_market(DAY)

    assert [doc.title for doc in first] == ["第一页"]
    assert any("不完整" in note for note in notes)
    assert {doc.title for doc in second} == {"第一页", "第二页"}


def test_mid_page_failure_keeps_news_and_reports_the_coverage_gap(tmp_path):
    http = PagingHttp([[_flash("第一页")], [_flash("第二页")]], fail_after=1)
    docs, notes = WallstreetNewsSource(
        opener=http, channels=("global-channel",), cache=NewsCache(tmp_path)
    ).fetch_market(DAY)

    assert [doc.title for doc in docs] == ["第一页"]
    assert any("不完整" in note for note in notes)


@pytest.mark.parametrize("timestamp", ["invalid", 10**100])
def test_one_bad_flash_timestamp_does_not_drop_a_whole_channel(timestamp):
    http = PagingHttp([[_flash("有效快讯"), _flash("时间缺失的快讯", timestamp)]])
    docs, _ = WallstreetNewsSource(opener=http, channels=("global-channel",)).fetch_market(DAY)

    assert {doc.title for doc in docs} == {"有效快讯", "时间缺失的快讯"}
    assert next(doc for doc in docs if doc.title == "时间缺失的快讯").published_at == ""


@pytest.mark.parametrize("payload", [{"code": 503}, {"data": {"items": "bad schema"}}])
def test_invalid_feed_response_is_retried_instead_of_caching_an_empty_success(tmp_path, payload):
    replies = iter([payload, {"data": {"items": [_flash("恢复后的快讯")]}}])

    def opener(request, timeout=None):
        return FakeResponse(json.dumps(next(replies)).encode())

    source = WallstreetNewsSource(
        opener=opener, channels=("global-channel",), cache=NewsCache(tmp_path)
    )
    first, notes = source.fetch_market(DAY)
    second, _ = source.fetch_market(DAY)

    assert first == []
    assert any("不可用" in note for note in notes)
    assert [doc.title for doc in second] == ["恢复后的快讯"]


def test_cache_accepts_a_valid_timezone_offset(tmp_path):
    cache = NewsCache(tmp_path)
    path = cache.path_for(DAY, "feed")
    path.parent.mkdir(parents=True)
    stamp = datetime.now(UTC) - timedelta(seconds=10)
    path.write_text(json.dumps({"cached_at": stamp.isoformat(), "data": ["valid"]}))

    assert cache.read(DAY, "feed") == ["valid"]


def test_monday_morning_news_window_includes_friday_market_open(monkeypatch):
    monday = datetime(2026, 8, 3, 8, 30, tzinfo=BEIJING_TZ)
    monkeypatch.setattr(
        "ashare_decision_support.news.wallstreet_source.beijing_now", lambda: monday
    )
    weekend = int(datetime(2026, 8, 1, 8, 0, tzinfo=BEIJING_TZ).timestamp())
    friday = int(datetime(2026, 7, 31, 9, 30, tzinfo=BEIJING_TZ).timestamp())
    http = PagingHttp([[_flash("周末新闻", weekend)], [_flash("周五早盘宏观数据", friday)]])

    docs, _ = WallstreetNewsSource(
        opener=http, channels=("global-channel",), lookback_hours=72
    ).fetch_market(monday.date())

    assert {doc.title for doc in docs} == {"周末新闻", "周五早盘宏观数据"}


def test_repeated_pagination_cursor_stops_and_reports_partial_results():
    calls = []

    def opener(request, timeout=None):
        calls.append(request.full_url)
        return FakeResponse(
            json.dumps({"data": {"items": [_flash("重复页面")], "next_cursor": "same"}})
        )

    docs, notes = WallstreetNewsSource(
        opener=opener, channels=("global-channel",), max_pages=16
    ).fetch_market(DAY)

    assert len(docs) == 1
    assert len(calls) == 2
    assert any("不完整" in note for note in notes)
