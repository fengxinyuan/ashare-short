"""同花顺行业指数：市场口径的第二套板块读数。

这一层存在的理由是申万成分股聚合答不了一个问题：**收盘落在当日区间的哪个分位**。
2026-07-31 实测现场——半导体 +4.23% 收在当日最低（收位 0%），同日白酒 +0.63% 收在
当日最高（收位 97%）。「涨了多少」两者都是正的，「谁在被卖」只有收位看得见。
"""

from __future__ import annotations

import json

import pytest

from ashare_decision_support.adapters.ths_sector_index import (
    ThsSectorIndexAdapter,
    _parse_line_js,
)
from tests.fakes import FakeResponse

# http double


def _by_name(snap) -> dict:
    """按板块名索引读数。断言要点名某个板块，`readings` 是个列表。"""
    return {r.name: r for r in snap.readings}


def _bars(rows: list[tuple[str, float, float, float, float, float]]) -> str:
    """(date, open, high, low, close, amount) -> 同花顺的分号分隔串。"""
    return ";".join(
        f"{d},{o},{h},{lo},{c},{int(amt / 10)},{amt:.3f},,,,0" for d, o, h, lo, c, amt in rows
    )


def _flat_series(n: int, close: float, amount: float = 1e9) -> list[tuple]:
    return [
        (f"2026{m:02d}{d:02d}", close, close, close, close, amount)
        for m in (5, 6)
        for d in range(1, n // 2 + 1)
    ][:n]


def _payload(code: str, name: str, rows: list[tuple]) -> bytes:
    body = json.dumps(
        {"name": name, "total": str(len(rows)), "data": _bars(rows)}, ensure_ascii=False
    )
    return f"quotebridge_v6_line_bk_{code}_01_last({body})".encode()


class StubHttp:
    """只有 `available` 里的代码有数据，其余返回空板块（同花顺的真实行为）。"""

    def __init__(self, available: dict[str, tuple[str, list[tuple]]]) -> None:
        self.available = available
        self.asked: list[str] = []

    def __call__(self, request, timeout=None):
        code = request.full_url.split("bk_")[1].split("/")[0]
        self.asked.append(code)
        if code not in self.available:
            return FakeResponse(b'quotebridge_v6_line_bk_x_01_last({"name":"","data":""})')
        name, rows = self.available[code]
        return FakeResponse(_payload(code, name, rows))


# 一根「高开冲高、收在当日最低」的日线，接在 26 根平盘之后。
FADE_ROWS = [*_flat_series(26, 100.0), ("20260731", 104.0, 106.0, 102.0, 102.0, 2000000000.0)]
# 一根「稳步走高、收在当日最高」的日线。
STRONG_ROWS = [*_flat_series(26, 100.0), ("20260731", 100.5, 101.0, 100.2, 101.0, 1200000000.0)]


def test_parses_the_jsonp_callback_wrapper() -> None:
    raw = _payload("881101", "半导体", FADE_ROWS).decode()
    name, bars = _parse_line_js(raw)

    assert name == "半导体"
    assert bars[-1]["close"] == 102.0
    assert bars[-1]["high"] == 106.0


def test_unparseable_body_returns_none_rather_than_a_wrong_reading() -> None:
    assert _parse_line_js("not a callback at all") is None
    assert _parse_line_js("cb({not json})") is None


def test_a_row_with_missing_cells_is_skipped_not_guessed() -> None:
    body = json.dumps({"name": "测试", "data": "20260731,1,2;20260730,1,2,3,4,5,6"})
    name, bars = _parse_line_js(f"cb({body})")

    assert name == "测试"
    assert [b["date"] for b in bars] == ["20260730"]


# measurement


def test_close_position_separates_faded_from_supported() -> None:
    """两个板块当天都是涨的，只有收位分得开它们。"""
    http = StubHttp({"881101": ("半导体", FADE_ROWS), "881102": ("白酒", STRONG_ROWS)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101", "881102"])

    by_name = _by_name(snap)
    assert by_name["半导体"].ret_1d > 0
    assert by_name["白酒"].ret_1d > 0
    assert by_name["半导体"].close_position == 0.0  # 收在当日最低
    assert by_name["白酒"].close_position == 100.0  # 收在当日最高


def test_shape_names_the_pattern_the_return_alone_hides() -> None:
    http = StubHttp({"881101": ("半导体", FADE_ROWS), "881102": ("白酒", STRONG_ROWS)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101", "881102"])

    by_name = _by_name(snap)
    assert "冲高回落" in by_name["半导体"].shape
    assert "有承接" in by_name["白酒"].shape


def test_describe_calls_out_sectors_that_rose_but_closed_at_the_low() -> None:
    http = StubHttp({"881101": ("半导体", FADE_ROWS), "881102": ("白酒", STRONG_ROWS)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101", "881102"])

    faded_line = next(line for line in snap.describe() if "冲高回落" in line)
    assert "半导体" in faded_line
    assert "白酒" not in faded_line


def test_volume_ratio_compares_the_recent_week_against_the_month() -> None:
    rows = [
        *_flat_series(26, 100.0, amount=1000000000.0),
        ("20260731", 100.0, 101.0, 99.0, 100.0, 1000000000.0),
    ]
    http = StubHttp({"881101": ("测试", rows)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101"])

    assert snap.readings[0].volume_ratio == pytest.approx(1.0, abs=1e-6)


def test_a_flat_bar_does_not_divide_by_zero() -> None:
    rows = [*_flat_series(26, 100.0), ("20260731", 100.0, 100.0, 100.0, 100.0, 1000000000.0)]
    http = StubHttp({"881101": ("测试", rows)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101"])

    assert snap.readings[0].close_position == 50.0


def test_too_short_a_history_is_dropped_instead_of_measured() -> None:
    http = StubHttp({"881101": ("新板块", _flat_series(6, 100.0))})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101"])

    assert snap.readings == []
    assert not snap.ok


# cross-sector consistency


def test_sectors_stuck_on_an_older_date_are_excluded_not_ranked() -> None:
    """不同交易日的读数排在一张榜上，名次就是假的。"""
    stale = [*_flat_series(26, 100.0), ("20260730", 100.0, 101.0, 99.0, 100.5, 1000000000.0)]
    http = StubHttp({"881101": ("半导体", FADE_ROWS), "881102": ("停牌板块", stale)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101", "881102"])

    assert [r.name for r in snap.readings] == ["半导体"]
    assert any("不是 20260731" in note for note in snap.notes)


def test_one_dead_sector_costs_only_itself() -> None:
    class HalfDead(StubHttp):
        def __call__(self, request, timeout=None):
            if "881102" in request.full_url:
                raise OSError("boom")
            return super().__call__(request, timeout=timeout)

    http = HalfDead({"881101": ("半导体", FADE_ROWS), "881102": ("白酒", STRONG_ROWS)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101", "881102"])

    assert snap.ok
    assert [r.name for r in snap.readings] == ["半导体"]
    assert any("1/2" in note for note in snap.notes)


def test_everything_down_reports_not_ok_rather_than_an_empty_ranking() -> None:
    class Dead(StubHttp):
        def __call__(self, request, timeout=None):
            raise OSError("boom")

    snap = ThsSectorIndexAdapter(opener=Dead({})).fetch(codes=["881101"])

    assert not snap.ok
    assert snap.readings == []


# code discovery cache


def test_discovery_scans_once_then_caches_the_live_codes(tmp_path) -> None:
    http = StubHttp({"881101": ("半导体", FADE_ROWS)})
    first = ThsSectorIndexAdapter(cache_dir=tmp_path, opener=http).fetch()

    assert [r.name for r in first.readings] == ["半导体"]
    assert len(http.asked) > 300  # 首次扫全区间
    assert any("首次探测" in note for note in first.notes)

    http.asked.clear()
    second = ThsSectorIndexAdapter(cache_dir=tmp_path, opener=http).fetch()

    assert http.asked == ["881101"]  # 之后只打活着的那个
    assert [r.name for r in second.readings] == ["半导体"]


def test_a_cache_without_the_schema_stamp_is_rescanned(tmp_path) -> None:
    (tmp_path / "ths_sector_codes.json").write_text(
        json.dumps({"codes": ["999999"]}), encoding="utf-8"
    )
    http = StubHttp({"881101": ("半导体", FADE_ROWS)})

    snap = ThsSectorIndexAdapter(cache_dir=tmp_path, opener=http).fetch()

    assert [r.name for r in snap.readings] == ["半导体"]
    assert len(http.asked) > 300


# describe 的两条独有读数

# 与 STRONG_ROWS 的价格形态完全相同（稳步走高、收在当日最高），只有成交额不同：
# 近端量能萎缩。两者的区别**只有 volume_ratio 分得开**，涨幅和收位都分不开。
DRY_ROWS = [
    *_flat_series(26, 100.0, amount=2000000000.0),
    ("20260731", 100.5, 101.0, 100.2, 101.0, 500000000.0),
]


def test_describe_reports_close_position_even_when_nothing_faded() -> None:
    """一个冲高回落都没有，是这一层能给的最强收盘读数——不能因此沉默。

    原来只在 faded 非空时才有一行，于是 2026-08-07 那种「90 个板块涨了 78 个、
    冲高回落 0 个」的日子，简报上一个字都没有。读的人会把「没写」读成「没测出
    什么」，而当天的事实是全场尾盘承接极强。
    """
    http = StubHttp({"881101": ("白酒", STRONG_ROWS), "881102": ("元件", STRONG_ROWS)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101", "881102"])

    line = next(line for line in snap.describe() if line.startswith("收盘位置"))
    assert "2/2" in line  # 分母跟着出，读的人才能自己判断
    assert "没有一个冲高回落" in line


def test_describe_still_names_the_faded_sectors() -> None:
    """分布总是报，但出现冲高回落时仍要点名——这是原来就有的行为，不能回退。"""
    http = StubHttp({"881101": ("半导体", FADE_ROWS), "881102": ("白酒", STRONG_ROWS)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101", "881102"])

    line = next(line for line in snap.describe() if line.startswith("收盘位置"))
    assert "冲高回落" in line
    assert "半导体" in line
    assert "白酒" not in line


def test_describe_splits_risers_by_volume_position() -> None:
    """`volume_ratio` 此前算了但没人读——涨幅一样的两个板块在简报上长得一模一样。

    2026-08-07 实测：同为大涨，小金属 1.29 是放量、半导体 0.85 是缩量。分不开这两
    类，「反弹有没有钱」这个问题在简报上就没有答案。
    """
    http = StubHttp({"881101": ("小金属", STRONG_ROWS), "881102": ("半导体", DRY_ROWS)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101", "881102"])

    by_name = _by_name(snap)
    # 两者涨幅与收位都相同，只有量能位置不同。
    assert by_name["小金属"].ret_1d == pytest.approx(by_name["半导体"].ret_1d)
    assert by_name["小金属"].close_position == by_name["半导体"].close_position
    assert by_name["小金属"].volume_ratio >= 1.0
    assert by_name["半导体"].volume_ratio < 1.0

    lines = snap.describe()
    volume_line = next(line for line in lines if line.startswith("上涨板块的量能位置"))
    assert "小金属" in volume_line
    dry_line = next(line for line in lines if "涨但缩量" in line)
    assert "半导体" in dry_line
    assert "小金属" not in dry_line


def test_describe_says_nothing_about_volume_when_nothing_rose() -> None:
    """全场下跌时不印「上涨板块的量能位置」——那一行的分母是 0。"""
    falling = [*_flat_series(26, 100.0), ("20260731", 99.5, 99.6, 98.0, 98.5, 1000000000.0)]
    http = StubHttp({"881101": ("半导体", falling)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101"])

    assert snap.readings[0].ret_1d < 0
    assert not any(line.startswith("上涨板块的量能位置") for line in snap.describe())


def test_volume_position_carries_the_scale_not_just_the_ratio() -> None:
    """比值必须配量级，否则会读出一个反过来的结论。

    2026-08-07 实测：教育量能 1.68 排放量榜首但日均只有 42 亿，半导体量能 0.85
    垫底、日均却有 3661 亿。只看比值会得出「教育是最大的流入方向」，而当天真正的
    资金事实是半导体那一侧在失血，且失的血比教育的全部成交额还多一个数量级。
    """
    http = StubHttp({"881101": ("小金属", STRONG_ROWS), "881102": ("半导体", DRY_ROWS)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101", "881102"])

    lines = snap.describe()
    expanding = next(line for line in lines if line.startswith("上涨板块的量能位置"))
    shrinking = next(line for line in lines if "涨但缩量" in line)
    # 两条线都要能看出「这个比值是多大一摊钱算出来的」。
    assert "日均" in expanding
    assert "日均" in shrinking
    # 缩量的那个基数更大——这正是只看比值会漏掉的事实。
    assert _by_name(snap)["半导体"].amount_5d > _by_name(snap)["小金属"].amount_5d
