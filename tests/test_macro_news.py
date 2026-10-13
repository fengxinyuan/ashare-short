"""宏观快讯源与多源合并。

**这一路补的缺口是实测出来的。** 2026-08-02 现有三路舆情（财联社 20 条、东财全球
快讯 180 条、新闻联播）在 200 条里的命中：伊朗 66、中东 26（地缘覆盖得很好），
但 **PMI 0、统计局 0、制造业 0、美债 0、降准 0**——宏观数据发布对系统完全不可见。
同一天 07-31 09:30 公布的「中国 7 月官方制造业 PMI 49.2」链路里一个字都没有。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from ashare_decision_support.news import CompositeNewsSource, WallstreetNewsSource
from ashare_decision_support.news.balance import balance_by_source
from ashare_decision_support.news.base import NewsDoc, NewsSource
from tests.fakes import FakeResponse

CN_TZ = timezone(timedelta(hours=8))


def _ts(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=CN_TZ).timestamp())


def _item(when: str, title: str = "", content: str = "") -> dict:
    return {"display_time": _ts(when), "title": title, "content_text": content}


class StubHttp:
    """按 cursor 逐页返回；每页 100 条以内。"""

    def __init__(self, pages_by_channel: dict[str, list[list[dict]]]) -> None:
        self.pages = pages_by_channel
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        channel = url.split("channel=")[1].split("&")[0]
        cursor = url.split("cursor=")[1] if "cursor=" in url else None
        self.calls.append((channel, cursor))
        pages = self.pages.get(channel, [])
        index = int(cursor) if cursor else 0
        items = pages[index] if index < len(pages) else []
        nxt = str(index + 1) if index + 1 < len(pages) else None
        body = json.dumps({"data": {"items": items, "next_cursor": nxt}}, ensure_ascii=False)
        return FakeResponse(body.encode("utf-8"))


PMI = _item("2026-07-31 09:30", content="中国7月官方制造业PMI 49.2，前值 50.3。")
RECENT = _item("2026-08-02 08:00", title="早间要闻")


# document mapping


def test_a_titleless_flash_uses_its_body_as_the_title() -> None:
    """数据发布几乎全是「只有正文没有标题」的形态。

    丢掉无标题条目 == 丢掉几乎全部的 PMI/CPI/社融，也就是这一路存在的理由。
    """
    http = StubHttp({"global-channel": [[PMI]], "a-stock-channel": [[]]})
    docs, _ = WallstreetNewsSource(opener=http).fetch_market(date(2026, 7, 31))

    assert len(docs) == 1
    assert "制造业PMI 49.2" in docs[0].title
    assert docs[0].source == "华尔街见闻"


def test_publish_time_is_converted_to_exchange_local_time() -> None:
    http = StubHttp({"global-channel": [[PMI]], "a-stock-channel": [[]]})
    docs, _ = WallstreetNewsSource(opener=http).fetch_market(date(2026, 7, 31))

    assert docs[0].published_at.startswith("2026-07-31 09:30")


def test_an_item_with_neither_title_nor_body_is_dropped() -> None:
    http = StubHttp({"global-channel": [[_item("2026-08-02 08:00")]], "a-stock-channel": [[]]})
    docs, _ = WallstreetNewsSource(opener=http).fetch_market(date(2026, 8, 2))

    assert docs == []


# time-window paging


def test_paging_is_driven_by_a_time_window_not_a_page_count() -> None:
    """两个频道流量差一个数量级，固定页数会让高流量频道只翻到几小时前——
    07-31 09:30 的 PMI 恰好落在那个盲区里。"""
    busy = [
        [_item(f"2026-08-02 0{h}:00", title=f"噪音{h}") for h in range(1, 10)],
        [_item(f"2026-08-01 1{h}:00", title=f"噪音b{h}") for h in range(9)],
        [PMI],
    ]
    http = StubHttp({"global-channel": busy, "a-stock-channel": [[]]})
    docs, notes = WallstreetNewsSource(opener=http, lookback_hours=72).fetch_market(
        date(2026, 8, 2), limit=1000
    )

    assert any("制造业PMI 49.2" in d.title for d in docs)
    assert any("回溯至 2026-07-31" in note for note in notes)


def test_paging_stops_once_the_window_is_covered() -> None:
    """翻够了就停，不把整个历史拖回来。"""
    pages = [
        [_item("2026-08-02 08:00", title="新")],
        [_item("2026-07-01 08:00", title="旧")],
        [_item("2026-06-01 08:00", title="更旧")],
    ]
    http = StubHttp({"global-channel": pages, "a-stock-channel": [[]]})
    docs, _ = WallstreetNewsSource(opener=http, lookback_hours=72).fetch_market(date(2026, 8, 2))

    assert {d.title for d in docs} == {"新", "旧"}  # 第三页没去翻
    assert len([c for c in http.calls if c[0] == "global-channel"]) == 2


def test_the_window_is_anchored_to_the_trade_date_not_the_wall_clock() -> None:
    """同一个交易日无论几点跑，取回的窗口都一样——盘前跑两次不该得到两份输入。"""
    http = StubHttp({"global-channel": [[PMI]], "a-stock-channel": [[]]})
    src = WallstreetNewsSource(opener=http, lookback_hours=72)

    first, _ = src.fetch_market(date(2026, 7, 31))
    second, _ = src.fetch_market(date(2026, 7, 31))

    assert [d.title for d in first] == [d.title for d in second]


def test_a_page_cap_stops_a_runaway_feed() -> None:
    endless = [[_item("2026-08-02 08:00", title=f"p{i}")] for i in range(50)]
    http = StubHttp({"global-channel": endless, "a-stock-channel": [[]]})
    WallstreetNewsSource(opener=http, lookback_hours=99999, max_pages=4).fetch_market(
        date(2026, 8, 2)
    )

    assert len([c for c in http.calls if c[0] == "global-channel"]) == 4


# degradation


def test_one_dead_channel_still_yields_the_other() -> None:
    class HalfDead(StubHttp):
        def __call__(self, request, timeout=None):
            if "a-stock-channel" in request.full_url:
                raise OSError("boom")
            return super().__call__(request, timeout=timeout)

    http = HalfDead({"global-channel": [[PMI]], "a-stock-channel": [[RECENT]]})
    docs, notes = WallstreetNewsSource(opener=http).fetch_market(date(2026, 7, 31))

    assert len(docs) == 1
    assert any("a-stock-channel 频道不可用" in note for note in notes)


def test_everything_down_says_the_macro_input_is_missing() -> None:
    class Dead(StubHttp):
        def __call__(self, request, timeout=None):
            raise OSError("boom")

    docs, notes = WallstreetNewsSource(opener=Dead({})).fetch_market(date(2026, 8, 2))

    assert docs == []
    assert any("无宏观数据发布输入" in note for note in notes)


def test_the_same_flash_in_both_channels_is_counted_once() -> None:
    http = StubHttp({"global-channel": [[PMI]], "a-stock-channel": [[PMI]]})
    docs, _ = WallstreetNewsSource(opener=http).fetch_market(date(2026, 7, 31))

    assert len(docs) == 1


# composite


class FakeSource(NewsSource):
    def __init__(self, name, market=(), announcements=(), boom=False) -> None:
        self.name = name
        self._market = list(market)
        self._announcements = list(announcements)
        self.boom = boom

    def fetch_market(self, trade_date, limit=200):
        if self.boom:
            raise ConnectionError("down")
        return list(self._market), [f"{self.name} ok"]

    def fetch_for_symbols(self, symbols, **kwargs):
        return {}, []

    def fetch_announcements(self, trade_date):
        return list(self._announcements), []

    def fetch_dragon_tiger(self, start, end):
        return [], []


def test_market_flashes_are_unioned_because_coverage_is_complementary() -> None:
    """东财+财联社覆盖地缘 66 次命中、宏观 0 次；华尔街见闻正好相反。"""
    a = FakeSource("akshare", market=[NewsDoc(title="伊朗局势升级")])
    b = FakeSource("wallstreetcn", market=[NewsDoc(title="中国7月官方制造业PMI 49.2")])

    docs, _ = CompositeNewsSource(a, b).fetch_market(date(2026, 8, 2))

    assert {d.title for d in docs} == {"伊朗局势升级", "中国7月官方制造业PMI 49.2"}


def test_announcements_take_the_first_source_that_answers() -> None:
    """公告是同一份交易所数据，取并集只会制造重复。"""
    a = FakeSource("akshare", announcements=[{"symbol": "000001"}])
    b = FakeSource("wallstreetcn", announcements=[{"symbol": "000002"}])

    rows, _ = CompositeNewsSource(a, b).fetch_announcements(date(2026, 8, 2))

    assert rows == [{"symbol": "000001"}]


def test_a_dead_source_does_not_take_down_the_others() -> None:
    dead = FakeSource("akshare", boom=True)
    alive = FakeSource("wallstreetcn", market=[NewsDoc(title="PMI 49.2")])

    docs, notes = CompositeNewsSource(dead, alive).fetch_market(date(2026, 8, 2))

    assert [d.title for d in docs] == ["PMI 49.2"]
    assert any("akshare 不可用" in note for note in notes)


def test_partial_coverage_is_stated_not_hidden() -> None:
    """少一路就是少一块覆盖面，不能读起来像「都取到了」。"""
    empty = FakeSource("akshare", market=[])
    alive = FakeSource("wallstreetcn", market=[NewsDoc(title="PMI 49.2")])

    _, notes = CompositeNewsSource(empty, alive).fetch_market(date(2026, 8, 2))

    assert any("1/2 路源" in note for note in notes)


def test_composite_needs_at_least_one_source() -> None:
    with pytest.raises(ValueError):
        CompositeNewsSource()


def test_composite_market_limit_is_shared_fairly_across_sources() -> None:
    busy = FakeSource(
        "busy",
        market=[NewsDoc(title=f"盘面{i}", source="忙源") for i in range(20)],
    )
    industry = FakeSource(
        "industry",
        market=[NewsDoc(title=f"纸浆消息{i}", source="行业源") for i in range(2)],
    )

    docs, _ = CompositeNewsSource(busy, industry).fetch_market(date(2026, 8, 2), limit=10)

    assert len(docs) == 10
    assert len([doc for doc in docs if doc.source == "行业源"]) == 2
    assert len([doc for doc in docs if doc.source == "忙源"]) == 8


def test_composite_diagnostics_include_expired_and_unretained_docs(monkeypatch) -> None:
    monkeypatch.setattr(
        "ashare_decision_support.news.composite.beijing_naive_now",
        lambda: datetime(2026, 9, 14, 10),
    )
    raw = [
        NewsDoc(title="过期政策", source=" policy ", published_at="2026-09-11 09:59:59"),
        NewsDoc(title="政策更新", source="policy", published_at="2026-09-14T01:00:00Z"),
        NewsDoc(title="", source="policy", published_at="2026-09-14T01:30:00Z"),
        NewsDoc(title="政策旧稿", source="policy", published_at="2026-09-12 10:00:00"),
        NewsDoc(title="无时间", source=""),
        NewsDoc(title="时间异常", source="unknown", published_at="bad-time"),
        NewsDoc(title="截止时刻", source="boundary", published_at="2026-09-11 10:00:00"),
        NewsDoc(title="有效临界稿", source="recent", published_at="2026-09-11 10:00:01"),
        NewsDoc(title="旧栏目", source="old-only", published_at="2026-09-01"),
    ]
    source = CompositeNewsSource(FakeSource("mixed", market=raw))

    docs, notes = source.fetch_market(date(2026, 9, 14), limit=2)

    assert docs == [raw[1], raw[7]]
    assert source.last_market_stats == {
        "raw_doc_count": 9,
        "retained_doc_count": 2,
        "raw_by_source": {"boundary": 1, "old-only": 1, "policy": 4, "recent": 1, "unknown": 2},
        "retained_by_source": {"policy": 1, "recent": 1},
        "effective_doc_count": 6,
        "effective_by_source": {"policy": 3, "recent": 1, "unknown": 2},
        "expired_before_budget": 3,
        "latest_by_source": {
            " policy ": "2026-09-11T09:59:59",
            "boundary": "2026-09-11T10:00:00",
            "old-only": "2026-09-01T00:00:00",
            "policy": "2026-09-14T09:30:00",
            "recent": "2026-09-11T10:00:01",
        },
    }
    assert notes == [
        "mixed ok",
        "超过 72 小时的快讯在分配来源预算前剔除 3 条",
        "市场快讯原始 9 条，按来源均衡保留 2 条",
    ]


def test_composite_historical_recency_uses_the_requested_day_end(monkeypatch) -> None:
    monkeypatch.setattr(
        "ashare_decision_support.news.composite.beijing_naive_now",
        lambda: datetime(2026, 9, 14, 10),
    )
    raw = [
        NewsDoc(title="已过期", source="archive", published_at="2026-09-10 23:59:59"),
        NewsDoc(title="历史有效稿", source="archive", published_at="2026-09-11 00:00:00"),
    ]
    source = CompositeNewsSource(FakeSource("archive", market=raw))

    docs, _ = source.fetch_market(date(2026, 9, 13))

    assert docs == [raw[1]]
    assert source.last_market_stats["expired_before_budget"] == 1


@pytest.mark.parametrize(
    ("limit", "indices", "counts"),
    [
        (-1, [], {}),
        (0, [], {}),
        (1, [1], {"a": 1}),
        (2, [1, 0], {"a": 1, "b": 1}),
        (3, [1, 0, 4], {"a": 1, "b": 1, "unknown": 1}),
        (4, [1, 2, 0, 4], {"a": 2, "b": 1, "unknown": 1}),
        (5, [1, 2, 0, 4, 5], {"a": 2, "b": 1, "unknown": 2}),
        (6, [1, 2, 0, 6, 4, 5], {"a": 3, "b": 1, "unknown": 2}),
        (20, [1, 2, 0, 6, 4, 5], {"a": 3, "b": 1, "unknown": 2}),
    ],
)
def test_market_balance_preserves_ties_and_redistributes_quota(limit, indices, counts) -> None:
    docs = [
        NewsDoc(title="b-first", source="b", published_at="2026-09-14T01:30:00Z"),
        NewsDoc(title="a-first", source=" a ", published_at="2026/09/14 09:30"),
        NewsDoc(title="a-second", source="a", published_at="20260914093000"),
        NewsDoc(title="", source="c", published_at="2026-09-15"),
        NewsDoc(title="missing", source=" "),
        NewsDoc(title="bad-date", source="unknown", published_at="invalid"),
        NewsDoc(title="oldest-valid", source="a", published_at="0001-01-01"),
    ]
    original = list(docs)

    selected, by_source = balance_by_source(docs, limit)

    assert selected == [original[index] for index in indices]
    assert by_source == counts
    assert docs == original
