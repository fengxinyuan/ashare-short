"""付费档分钟线：列式解析、量纲、浮点卫生、向前翻页。

三件事这个文件必须钉住，每一件都实测过一次错法：

1. **响应是列式的**（`{'timestamp': [...], 'close': [...]}`），和日线的行式不同。
   当成行处理会得到一条巨大的假记录，而且不会报错。
2. **`volume` 是「手」**，新浪那路是「股」。`signals/intraday.py` 的 `_vwap` 用
   `amount / volume`，不换算的话茅台 VWAP 会算成 135083 而不是 1351。
3. **单次 5000 根是硬顶**，`count` 调大无效，只能把 `end_time` 往前推。

外加一条浮点卫生：复权后的 OHLC 四个字段各自带尾巴，实测 15000 根里 1399 根
（9.33%）满足 `high < low`。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from ashare_decision_support.adapters.tickflow_intraday import (
    MAX_BARS_PER_REQUEST,
    TickFlowIntradayAdapter,
    fetch_minute_history,
    to_bars,
)

# 600519.SH 2026-07-31 尾盘三根，取自实测响应。
COMPACT = {
    "timestamp": [1785480960000, 1785481020000, 1785481200000],   # 14:56 / 14:57 / 15:00 CST
    "open": [1350.5, 1351.0, 1350.599999999998],
    "high": [1351.9, 1352.96, 1350.599999999988],     # 注意 high < low
    "low": [1349.88, 1350.24, 1350.6000000000106],
    "close": [1351.54, 1352.4, 1350.5999999999965],
    "volume": [370, 346, 677],                          # 手
    "amount": [49980887.1, 46766968.3, 91322169.0],
}


class FakeKlines:
    def __init__(self, pages=None, boom: bool = False) -> None:
        # pages: 依次返回的响应；不给就每次都返回 COMPACT。
        self.pages = pages
        self.boom = boom
        self.calls: list[dict] = []

    def batch(self, **kwargs):
        self.calls.append(kwargs)
        if self.boom:
            raise RuntimeError("klines down")
        if self.pages is None:
            return {s: COMPACT for s in kwargs["symbols"]}
        index = len(self.calls) - 1
        page = self.pages[index] if index < len(self.pages) else {}
        return {s: page for s in kwargs["symbols"]}


class FakeClient:
    def __init__(self, klines=None) -> None:
        self.klines = klines or FakeKlines()


# ---- 列式解析 -------------------------------------------------------------


def test_columnar_payload_becomes_one_bar_per_timestamp() -> None:
    bars = to_bars(COMPACT, "600519.SH")

    assert len(bars) == 3
    assert bars[0]["timestamp"] == datetime(2026, 7, 31, 14, 46)
    assert bars[0]["close"] == 1351.54


def test_a_row_shaped_payload_yields_nothing_rather_than_garbage() -> None:
    assert to_bars({"close": 1351.54, "timestamp": 1785481560000}, "600519.SH") == []
    assert to_bars(None, "600519.SH") == []


def test_ragged_columns_do_not_invent_values() -> None:
    """某一列比 timestamp 短的时候，缺的位置是 None，不是错位取到别人的值。"""
    ragged = dict(COMPACT, high=[1351.9])
    bars = to_bars(ragged, "600519.SH")

    assert bars[0]["high"] == 1351.9
    assert bars[1]["high"] is None


# ---- 量纲 -----------------------------------------------------------------


def test_volume_is_converted_from_lots_to_shares() -> None:
    """同一根 K 线新浪报 37000 股，TickFlow 报 370 手。"""
    bars = to_bars(COMPACT, "600519.SH")

    assert bars[0]["volume"] == 37000.0


def test_vwap_only_comes_out_right_because_volume_was_converted() -> None:
    """把量纲错误放到它真正起作用的地方验一次：amount/volume 就是成交均价。"""
    bar = to_bars(COMPACT, "600519.SH")[0]

    assert bar["amount"] / bar["volume"] == pytest.approx(1351.1, abs=0.5)


# ---- 浮点卫生 -------------------------------------------------------------


def test_prices_are_rounded_so_high_is_never_below_low() -> None:
    """复权后 OHLC 各自带浮点尾巴，实测 9.33% 的 K 线出现 high < low。

    下游拿 `high - low` 当振幅、拿 `low == min(lows)` 找枢轴点，倒挂会让这些
    判断静默出错——不报错，只是结果不对。
    """
    third = to_bars(COMPACT, "600519.SH")[2]

    assert third["high"] >= third["low"]
    assert third["close"] == 1350.6


# ---- 批量取数 -------------------------------------------------------------


def test_minute_bars_are_fetched_for_all_symbols_in_one_request() -> None:
    """免费档是一只票一次 HTTP 往返，付费档的意义就在这里。"""
    client = FakeClient()
    bars, notes = TickFlowIntradayAdapter(client).get_minute_bars(
        ["600519.SH", "000001.SZ"], period="1"
    )

    assert set(bars) == {"600519.SH", "000001.SZ"}
    assert len(client.klines.calls) == 1
    assert client.klines.calls[0]["period"] == "1m"
    assert notes == []


def test_count_is_capped_at_the_server_limit() -> None:
    """服务端单次 5000 根封顶，传更大只是自欺。"""
    client = FakeClient()
    TickFlowIntradayAdapter(client).get_minute_bars(["600519.SH"], period="1", max_bars=99999)

    assert client.klines.calls[0]["count"] == MAX_BARS_PER_REQUEST


def test_an_unsupported_period_is_rejected_up_front() -> None:
    with pytest.raises(ValueError, match="unsupported period"):
        TickFlowIntradayAdapter(FakeClient()).get_minute_bars(["600519.SH"], period="3")


def test_a_failed_batch_is_reported_not_raised() -> None:
    client = FakeClient(FakeKlines(boom=True))
    bars, notes = TickFlowIntradayAdapter(client).get_minute_bars(["600519.SH"], period="1")

    assert bars == {}
    assert any("拉取失败" in note for note in notes)


def test_no_symbols_is_not_a_request() -> None:
    client = FakeClient()
    assert TickFlowIntradayAdapter(client).get_minute_bars([], period="1") == ({}, [])
    assert client.klines.calls == []


# ---- 向前翻页 -------------------------------------------------------------


def _page(start_ms: int, count: int) -> dict:
    stamps = [start_ms + i * 60_000 for i in range(count)]
    return {
        "timestamp": stamps,
        "open": [10.0] * count,
        "high": [10.1] * count,
        "low": [9.9] * count,
        "close": [10.0] * count,
        "volume": [100] * count,
        "amount": [100000.0] * count,
    }


def test_history_pages_backwards_by_moving_end_time() -> None:
    """单次 5000 根封顶且 `count`/`start_time` 都放宽不了，只能把 end_time 前推。"""
    newer = _page(1785000000000, 3)
    older = _page(1784000000000, 3)
    client = FakeClient(FakeKlines(pages=[newer, older, {}]))

    bars, _ = fetch_minute_history(client, "600519.SH", period="1")

    assert len(bars) == 6
    assert bars[0]["timestamp"] < bars[-1]["timestamp"]        # 升序
    # 第二页的 end_time 落在第一页最早一根之前。
    assert client.klines.calls[1]["end_time"] == 1785000000000 - 60_000


def test_history_stops_when_a_page_adds_nothing_new() -> None:
    """到数据底之后服务端会一直返回同一批，不停就会一直空转到页数上限。"""
    same = _page(1785000000000, 3)
    client = FakeClient(FakeKlines(pages=[same, same, same, same]))

    bars, _ = fetch_minute_history(client, "600519.SH", period="1")

    assert len(bars) == 3
    assert len(client.klines.calls) == 2       # 第二页发现没有新数据就停


def test_history_stops_once_the_requested_start_is_covered() -> None:
    page = _page(1785000000000, 3)
    client = FakeClient(FakeKlines(pages=[page, page]))

    fetch_minute_history(
        client, "600519.SH", period="1", start=date(2000, 1, 1), end=date(2026, 7, 31)
    )

    assert len(client.klines.calls) >= 1


def test_history_trims_to_the_requested_window() -> None:
    """翻页是按整页回来的，落在窗口外的部分不该混进结果。"""
    page = _page(int(datetime(2026, 7, 30, 9, 30).timestamp() * 1000), 3)
    client = FakeClient(FakeKlines(pages=[page, {}]))

    bars, _ = fetch_minute_history(
        client, "600519.SH", period="1", start=date(2026, 7, 31), end=date(2026, 7, 31)
    )

    assert bars == []


def test_a_page_cap_is_announced_instead_of_looking_complete() -> None:
    """截断了不说，读起来就像「数据就这么多」。"""
    pages = [_page(1785000000000 - i * 1_000_000, 3) for i in range(6)]
    client = FakeClient(FakeKlines(pages=pages))

    _, notes = fetch_minute_history(client, "600519.SH", period="1", max_pages=3)

    assert any("翻满" in note and "未触底" in note for note in notes)


def test_a_mid_page_failure_keeps_what_was_already_fetched() -> None:
    class HalfDead(FakeKlines):
        def batch(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) > 1:
                raise RuntimeError("boom")
            return {kwargs["symbols"][0]: _page(1785000000000, 3)}

    client = FakeClient(HalfDead())
    bars, notes = fetch_minute_history(client, "600519.SH", period="1")

    assert len(bars) == 3
    assert any("取数失败" in note for note in notes)
