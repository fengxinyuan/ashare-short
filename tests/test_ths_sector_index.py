"""同花顺行业指数：市场口径的第二套板块读数。

这一层存在的理由是申万成分股聚合答不了一个问题：**收盘落在当日区间的哪个分位**。
2026-07-31 实测现场——半导体 +4.23% 收在当日最低（收位 0%），同日白酒 +0.63% 收在
当日最高（收位 97%）。「涨了多少」两者都是正的，「谁在被卖」只有收位看得见。
"""

from __future__ import annotations

import json

import pytest

from ashare_decision_support.adapters.ths_sector_index import (
    SectorIndexReading,
    ThsSectorIndexAdapter,
    _parse_line_js,
)


# ---- http double ---------------------------------------------------------


def _bars(rows: list[tuple[str, float, float, float, float, float]]) -> str:
    """(date, open, high, low, close, amount) -> 同花顺的分号分隔串。"""
    return ";".join(
        f"{d},{o},{h},{lo},{c},{int(amt / 10)},{amt:.3f},,,,0" for d, o, h, lo, c, amt in rows
    )


def _flat_series(n: int, close: float, amount: float = 1e9) -> list[tuple]:
    return [(f"2026{m:02d}{d:02d}", close, close, close, close, amount)
            for m in (5, 6) for d in range(1, n // 2 + 1)][:n]


def _payload(code: str, name: str, rows: list[tuple]) -> bytes:
    body = json.dumps({"name": name, "total": str(len(rows)), "data": _bars(rows)},
                      ensure_ascii=False)
    return f"quotebridge_v6_line_bk_{code}_01_last({body})".encode("utf-8")


class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StubHttp:
    """只有 `available` 里的代码有数据，其余返回空板块（同花顺的真实行为）。"""

    def __init__(self, available: dict[str, tuple[str, list[tuple]]]) -> None:
        self.available = available
        self.asked: list[str] = []

    def __call__(self, request, timeout=None):
        code = request.full_url.split("bk_")[1].split("/")[0]
        self.asked.append(code)
        if code not in self.available:
            return _Resp(b'quotebridge_v6_line_bk_x_01_last({"name":"","data":""})')
        name, rows = self.available[code]
        return _Resp(_payload(code, name, rows))


# 一根「高开冲高、收在当日最低」的日线，接在 26 根平盘之后。
FADE_ROWS = _flat_series(26, 100.0) + [("20260731", 104.0, 106.0, 102.0, 102.0, 2e9)]
# 一根「稳步走高、收在当日最高」的日线。
STRONG_ROWS = _flat_series(26, 100.0) + [("20260731", 100.5, 101.0, 100.2, 101.0, 1.2e9)]


# ---- parsing -------------------------------------------------------------


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


# ---- measurement ---------------------------------------------------------


def test_close_position_separates_faded_from_supported() -> None:
    """两个板块当天都是涨的，只有收位分得开它们。"""
    http = StubHttp({"881101": ("半导体", FADE_ROWS), "881102": ("白酒", STRONG_ROWS)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101", "881102"])

    by_name = snap.by_name()
    assert by_name["半导体"].ret_1d > 0
    assert by_name["白酒"].ret_1d > 0
    assert by_name["半导体"].close_position == 0.0     # 收在当日最低
    assert by_name["白酒"].close_position == 100.0     # 收在当日最高


def test_shape_names_the_pattern_the_return_alone_hides() -> None:
    http = StubHttp({"881101": ("半导体", FADE_ROWS), "881102": ("白酒", STRONG_ROWS)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101", "881102"])

    by_name = snap.by_name()
    assert "冲高回落" in by_name["半导体"].shape
    assert "有承接" in by_name["白酒"].shape


def test_describe_calls_out_sectors_that_rose_but_closed_at_the_low() -> None:
    http = StubHttp({"881101": ("半导体", FADE_ROWS), "881102": ("白酒", STRONG_ROWS)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101", "881102"])

    faded_line = next(line for line in snap.describe() if "冲高回落" in line)
    assert "半导体" in faded_line
    assert "白酒" not in faded_line


def test_volume_ratio_compares_the_recent_week_against_the_month() -> None:
    rows = _flat_series(26, 100.0, amount=1e9) + [("20260731", 100.0, 101.0, 99.0, 100.0, 1e9)]
    http = StubHttp({"881101": ("测试", rows)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101"])

    assert snap.readings[0].volume_ratio == pytest.approx(1.0, abs=1e-6)


def test_a_flat_bar_does_not_divide_by_zero() -> None:
    rows = _flat_series(26, 100.0) + [("20260731", 100.0, 100.0, 100.0, 100.0, 1e9)]
    http = StubHttp({"881101": ("测试", rows)})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101"])

    assert snap.readings[0].close_position == 50.0


def test_too_short_a_history_is_dropped_instead_of_measured() -> None:
    http = StubHttp({"881101": ("新板块", _flat_series(6, 100.0))})
    snap = ThsSectorIndexAdapter(opener=http).fetch(codes=["881101"])

    assert snap.readings == []
    assert not snap.ok


# ---- cross-sector consistency -------------------------------------------


def test_sectors_stuck_on_an_older_date_are_excluded_not_ranked() -> None:
    """不同交易日的读数排在一张榜上，名次就是假的。"""
    stale = _flat_series(26, 100.0) + [("20260730", 100.0, 101.0, 99.0, 100.5, 1e9)]
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


# ---- code discovery cache ------------------------------------------------


def test_discovery_scans_once_then_caches_the_live_codes(tmp_path) -> None:
    http = StubHttp({"881101": ("半导体", FADE_ROWS)})
    first = ThsSectorIndexAdapter(cache_dir=tmp_path, opener=http).fetch()

    assert [r.name for r in first.readings] == ["半导体"]
    assert len(http.asked) > 300          # 首次扫全区间
    assert any("首次探测" in note for note in first.notes)

    http.asked.clear()
    second = ThsSectorIndexAdapter(cache_dir=tmp_path, opener=http).fetch()

    assert http.asked == ["881101"]        # 之后只打活着的那个
    assert [r.name for r in second.readings] == ["半导体"]


def test_a_cache_without_the_schema_stamp_is_rescanned(tmp_path) -> None:
    (tmp_path / "ths_sector_codes.json").write_text(
        json.dumps({"codes": ["999999"]}), encoding="utf-8"
    )
    http = StubHttp({"881101": ("半导体", FADE_ROWS)})

    snap = ThsSectorIndexAdapter(cache_dir=tmp_path, opener=http).fetch()

    assert [r.name for r in snap.readings] == ["半导体"]
    assert len(http.asked) > 300
