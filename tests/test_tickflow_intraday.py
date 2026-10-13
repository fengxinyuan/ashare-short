"""付费档分钟线：列式解析、成交量换算、价格精度、批量上限与免费回退。"""

from __future__ import annotations

from datetime import datetime

import pytest

from ashare_decision_support.adapters.tickflow_intraday import (
    MAX_BARS_PER_REQUEST,
    TickFlowIntradayAdapter,
    build_intraday_adapter,
    to_bars,
)

# 600519.SH 2026-07-31 尾盘三根，取自实测响应。
COMPACT = {
    "timestamp": [1785480960000, 1785481020000, 1785481200000],  # 14:56 / 14:57 / 15:00 CST
    "open": [1350.5, 1351.0, 1350.599999999998],
    "high": [1351.9, 1352.96, 1350.599999999988],  # 注意 high < low
    "low": [1349.88, 1350.24, 1350.6000000000106],
    "close": [1351.54, 1352.4, 1350.5999999999965],
    "volume": [370, 346, 677],  # 手
    "amount": [49980887.1, 46766968.3, 91322169.0],
}


class FakeKlines:
    def __init__(self, boom: bool = False) -> None:
        self.boom = boom
        self.calls: list[dict] = []

    def batch(self, **kwargs):
        self.calls.append(kwargs)
        if self.boom:
            raise RuntimeError("klines down")
        return dict.fromkeys(kwargs["symbols"], COMPACT)


class FakeClient:
    def __init__(self, klines=None) -> None:
        self.klines = klines or FakeKlines()


# 列式解析


def test_columnar_payload_becomes_one_bar_per_timestamp() -> None:
    bars = to_bars(COMPACT, "600519.SH")

    assert len(bars) == 3
    assert bars[0]["timestamp"] == datetime(2026, 7, 31, 14, 56)
    assert bars[-1]["timestamp"] == datetime(2026, 7, 31, 15, 0)
    assert bars[0]["close"] == 1351.54


def test_timestamps_are_exchange_local_time() -> None:
    """毫秒时间戳按 UTC 解会把 15:00 收盘读成 07:00，整条日内形态跟着错位。"""
    bars = to_bars(COMPACT, "600519.SH")

    assert bars[-1]["timestamp"].hour == 15
    assert bars[-1]["timestamp"].tzinfo is None  # 与新浪那路同为 naive


def test_a_row_shaped_payload_yields_nothing_rather_than_garbage() -> None:
    assert to_bars({"close": 1351.54, "timestamp": 1785481560000}, "600519.SH") == []
    assert to_bars(None, "600519.SH") == []


def test_ragged_columns_do_not_invent_values() -> None:
    """某一列比 timestamp 短的时候，缺的位置是 None，不是错位取到别人的值。"""
    ragged = dict(COMPACT, high=[1351.9])
    bars = to_bars(ragged, "600519.SH")

    assert bars[0]["high"] == 1351.9
    assert bars[1]["high"] is None


# 量纲


def test_volume_is_converted_from_lots_to_shares() -> None:
    """同一根 K 线新浪报 37000 股，TickFlow 报 370 手。"""
    bars = to_bars(COMPACT, "600519.SH")

    assert bars[0]["volume"] == 37000.0


def test_vwap_only_comes_out_right_because_volume_was_converted() -> None:
    """把量纲错误放到它真正起作用的地方验一次：amount/volume 就是成交均价。"""
    bar = to_bars(COMPACT, "600519.SH")[0]

    assert bar["amount"] / bar["volume"] == pytest.approx(1351.1, abs=0.5)


# 浮点卫生


def test_prices_are_rounded_so_high_is_never_below_low() -> None:
    """复权后 OHLC 各自带浮点尾巴，实测 9.33% 的 K 线出现 high < low。

    下游拿 `high - low` 当振幅、拿 `low == min(lows)` 找枢轴点，倒挂会让这些
    判断静默出错——不报错，只是结果不对。
    """
    third = to_bars(COMPACT, "600519.SH")[2]

    assert third["high"] >= third["low"]
    assert third["close"] == 1350.6


# 批量取数


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


def test_minute_bars_are_unadjusted_because_they_are_quoted_as_order_prices() -> None:
    """SDK 默认是比例前复权，不是不复权——必须显式指定。

    实测五粮液 21 天 1 分钟线窗口内有除权，最早一根前复权 71.1339、不复权
    73.6200，差 3.4%。而免费路径（新浪 `adjust=""`）给的是原始价：两条路对同一
    件事给不同的数，是这类系统里最难查的一种错。简报里那句「价格为不复权实际
    成交价」也得是真的。
    """
    client = FakeClient()
    TickFlowIntradayAdapter(client).get_minute_bars(["600519.SH"], period="1")

    assert client.klines.calls[0]["adjust"] == "none"


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


# 选路


class PaidAdapter:
    supports_intraday = True

    def __init__(self) -> None:
        self.client = FakeClient()


class FreeAdapter:
    supports_intraday = False
    client = None


def test_a_paid_adapter_routes_to_the_batch_endpoint() -> None:
    assert isinstance(build_intraday_adapter(PaidAdapter()), TickFlowIntradayAdapter)


@pytest.mark.parametrize("adapter", [FreeAdapter(), None])
def test_a_free_adapter_does_not_route_to_the_paid_endpoint(adapter, monkeypatch) -> None:
    """档位是服务端说了算的，猜不得——`supports_intraday` 为假就必须退回免费路径。"""
    fallback = object()
    monkeypatch.setattr(
        "ashare_decision_support.adapters.sina_intraday.SinaIntradayAdapter", lambda: fallback
    )
    assert build_intraday_adapter(adapter) is fallback


def test_no_intraday_backend_degrades_to_none(monkeypatch) -> None:
    """分钟线缺了链路仍要能跑（退化成只出日线结构判断），所以这里不许抛。"""

    def missing():
        raise RuntimeError("optional intraday dependency is not installed")

    monkeypatch.setattr(
        "ashare_decision_support.adapters.sina_intraday.SinaIntradayAdapter", missing
    )
    assert build_intraday_adapter(None) is None


def test_minute_bars_drop_non_finite_readings() -> None:
    """NaN 不能进分钟线——`float(nan)` 不抛异常，`try/float` 挡不住它。

    进去之后不会报错：`_px` 的 `round(nan, 4)` 还是 NaN，而
    `signals/intraday.py` 里 `float(bar.get("volume") or 0.0)` 也收得下——NaN 是
    真值，连那个 `or 0.0` 兜底都不触发，当日累计成交量整个变成 NaN，随后所有
    量比比较静默变成 False。
    """
    from ashare_decision_support.adapters.tickflow_intraday import _at

    nan = float("nan")
    assert _at([1.0, nan, 3.0], 1) is None
    assert _at([nan], 0) is None
    assert _at([float("inf")], 0) is None
    assert _at([], 0) is None
    assert _at([None, ""], 0) is None
    assert _at([None, ""], 1) is None
    # 正常读数照常通过，字符串数字也仍然收得下。
    assert _at([1.5], 0) == 1.5
    assert _at(["2.5"], 0) == 2.5
