from __future__ import annotations

import math
from datetime import date, datetime

from ashare_decision_support.adapters.tickflow_adapter import TickFlowAdapter


class FakeDepth:
    def get(self, *args, **kwargs):
        symbol = args[0] if args else kwargs.get("symbol")
        return {
            "data": [
                {
                    "symbol": symbol,
                    "bidVol1": 1200,
                    "bidVol2": 800,
                    "askVol1": 500,
                    "askVol2": 300,
                }
            ]
        }


class FakeQuotes:
    def get(self, *args, **kwargs):
        symbol = args[0] if args else kwargs.get("symbol") or (kwargs.get("symbols") or [None])[0]
        return {
            "data": [
                {
                    "symbol": symbol,
                    "last_price": 12.5,
                    "open": 12.2,
                    "prev_close": 12.0,
                    "volume": 100000,
                    "amount": 1250000,
                    "timestamp": datetime(2026, 7, 31, 9, 33),
                }
            ]
        }


class FakeKlines:
    def get(self, *args, **kwargs):
        symbol = args[0] if args else kwargs.get("symbol")
        period = kwargs.get("period")
        if period == "1d":
            return {
                "data": {
                    "timestamp": [datetime(2026, 7, 30, 15, 0), datetime(2026, 7, 31, 15, 0)],
                    "open": [12.0, 12.2],
                    "high": [12.5, 12.7],
                    "low": [11.8, 12.0],
                    "close": [12.2, 12.5],
                    "volume": [100000, 120000],
                    "amount": [1220000, 1500000],
                    "symbol": symbol,
                }
            }
        return {
            "data": {
                "timestamp": [
                    datetime(2026, 7, 31, 9, 31),
                    datetime(2026, 7, 31, 9, 33),
                    datetime(2026, 7, 31, 9, 35),
                ],
                "open": [12.2, 12.3, 12.4],
                "high": [12.3, 12.5, 12.6],
                "low": [12.1, 12.2, 12.3],
                "close": [12.25, 12.45, 12.55],
                "volume": [30000, 25000, 26000],
                "amount": [367500, 311250, 326000],
                "symbol": symbol,
            }
        }


class FakeInstruments:
    def batch(self, *args, **kwargs):
        symbols = args[0] if args else kwargs.get("symbols", [])
        return {
            "data": [{"symbol": symbol, "name": f"Name-{symbol}"} for symbol in symbols]
        }


class FakeClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key
        self.quotes = FakeQuotes()
        self.klines = FakeKlines()
        self.instruments = FakeInstruments()
        self.depth = FakeDepth()

    def connect(self):
        return True


class FakeTickFlowModule:
    TickFlow = FakeClient


class FakeCnLotKlines:
    def get(self, *args, **kwargs):
        symbol = args[0] if args else kwargs.get("symbol")
        return {
            "data": {
                "timestamp": [datetime(2026, 7, 30, 15, 0), datetime(2026, 7, 31, 15, 0)],
                "open": [27.80, 27.90],
                "high": [28.20, 28.90],
                "low": [27.50, 27.70],
                "close": [27.60, 28.67],
                "volume": [85000, 93930],
                "amount": [236000000, 271445068],
                "symbol": symbol,
            }
        }


class FakeFreeClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key
        self.klines = FakeCnLotKlines()
        self.instruments = FakeInstruments()

    def connect(self):
        return True


class FakeFreeTickFlowModule:
    TickFlow = FakeFreeClient


def test_tickflow_adapter_historical_bars() -> None:
    adapter = TickFlowAdapter(api_key="key", tickflow_module=FakeTickFlowModule())
    bars = adapter.get_historical_bars(["300001.SZ"], date(2026, 7, 30), date(2026, 7, 31))

    assert len(bars["300001.SZ"]) == 2
    assert bars["300001.SZ"][-1]["close"] == 12.5


def test_tickflow_adapter_normalizes_a_share_symbol_suffix() -> None:
    adapter = TickFlowAdapter(api_key="key", tickflow_module=FakeTickFlowModule())

    assert adapter._normalize_symbol("603983") == "603983.SH"
    assert adapter._normalize_symbol("300001") == "300001.SZ"
    assert adapter._normalize_symbol("830001") == "830001.BJ"


class RecordingKlines(FakeCnLotKlines):
    """Records the window each call asks for, so we can assert on the lookback."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get(self, *args, **kwargs):
        self.calls.append(dict(kwargs))
        return super().get(*args, **kwargs)


class RecordingFreeClient(FakeFreeClient):
    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(api_key)
        self.klines = RecordingKlines()


class RecordingFreeModule:
    TickFlow = RecordingFreeClient


def test_free_mode_tests_are_not_silently_upgraded_by_an_exported_key() -> None:
    """A guard on the guard: `..._free_mode_...` tests must really run free mode.

    `TickFlowAdapter.__init__` does `api_key or os.getenv("TICKFLOW_API_KEY")`,
    which the CLI relies on. The side effect is that `api_key=None` in a test
    means "free tier" only while no key is exported. Once the Pro tier was
    activated, every free-mode assertion below started running against full mode
    — three of them broke, and had the fixtures been a little more permissive
    they would have kept passing while testing the wrong path entirely.

    `tests/conftest.py:no_ambient_credentials` scrubs the variable. This test
    fails if that scrubbing is ever dropped.
    """
    adapter = TickFlowAdapter(api_key=None, tickflow_module=FakeFreeTickFlowModule())

    assert adapter.mode == "free"
    assert adapter.supports_realtime is False
    assert adapter.supports_intraday is False
    assert adapter.supports_depth is False


# ---- 批次大小护栏 ---------------------------------------------------------
#
# 直觉上「一次要更多只」应该更快，实测正相反。2026-08-02 付费档全 A 5534 只、
# 120 天日线：chunk=100 拿到 5534/5534 用 27.8s；chunk=500 只剩 4434 只、53.6s；
# chunk=1000 剩 4034 只、51.2s；chunk=2000 **一只都没有**、36.2s。
#
# 丢掉的标的返回空列表，与「这只票确实没有日线」无法区分——又慢又静默丢数据。
# 小样本（2000 只）测出来的结论是反的，所以这里用调用记录钉死批次上限。


class ChunkRecordingKlines:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def batch(self, symbols, **kwargs):
        self.batch_sizes.append(len(symbols))
        return {
            symbol: {
                "timestamp": [datetime(2026, 7, 31, 15, 0)],
                "open": [12.0],
                "high": [12.5],
                "low": [11.8],
                "close": [12.2],
                "volume": [100000],
                "amount": [1220000],
            }
            for symbol in symbols
        }


class ChunkRecordingClient(FakeClient):
    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(api_key)
        self.klines = ChunkRecordingKlines()


class ChunkRecordingModule:
    TickFlow = ChunkRecordingClient


def _batch_sizes(chunk_size: int, symbol_count: int = 250) -> list[int]:
    adapter = TickFlowAdapter(api_key="k", tickflow_module=ChunkRecordingModule())
    symbols = [f"{600000 + index}.SH" for index in range(symbol_count)]
    adapter.get_historical_bars_batch(symbols, date(2026, 4, 1), date(2026, 7, 31), chunk_size=chunk_size)
    return adapter.client.klines.batch_sizes


def test_an_oversized_chunk_is_clamped_instead_of_silently_losing_symbols() -> None:
    assert max(_batch_sizes(2000)) <= 100


def test_the_default_chunk_is_the_measured_safe_size() -> None:
    assert _batch_sizes(100) == [100, 100, 50]


def test_a_zero_or_negative_chunk_does_not_hang() -> None:
    """`range(0, n, 0)` 会抛 ValueError，夹到 1 至少还能跑完。"""
    assert _batch_sizes(0, symbol_count=3) == [1, 1, 1]


def test_every_symbol_comes_back_when_the_chunking_is_right() -> None:
    adapter = TickFlowAdapter(api_key="k", tickflow_module=ChunkRecordingModule())
    symbols = [f"{600000 + index}.SH" for index in range(250)]
    bars = adapter.get_historical_bars_batch(symbols, date(2026, 4, 1), date(2026, 7, 31))

    assert len(bars) == 250
    assert all(rows for rows in bars.values())


# ---- 列式载荷：这一支此前是全仓唯一不过 `finite` 的取数路径 ----------------


class _ColumnarKlines:
    """TickFlow 返回**列式**载荷时的样子：每个字段一个数组。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def get(self, *args, **kwargs):
        return self.payload


def _columnar_adapter(payload: dict) -> TickFlowAdapter:
    module = type(
        "Mod",
        (),
        {"TickFlow": staticmethod(lambda **kw: type("C", (), {"klines": _ColumnarKlines(payload)})())},
    )
    return TickFlowAdapter(
        api_key="k", tickflow_module=module, auto_connect=False, rate_limit=None
    )


def test_columnar_bars_drop_non_finite_readings() -> None:
    """NaN 不是 None，`close is not None` 那道过滤挡不住它。

    挡不住的后果不是报错：`dipbuy` 的 `float(high)` 收得下 NaN，真实波幅变成
    NaN，ATR 跟着变成 NaN，而 NaN 的比较永远是 False——「跌破止损」于是静默
    变成「没跌破」。这条钉住的是取数那一层，不是下游每一处比较。
    """
    nan = float("nan")
    adapter = _columnar_adapter(
        {
            "timestamp": [1_785_000_000_000, 1_785_086_400_000, 1_785_172_800_000],
            "open": [10.0, nan, 10.2],
            "high": [10.5, 10.6, float("inf")],
            "low": [9.8, 9.9, 10.0],
            "close": [10.2, 10.4, nan],
            "volume": [1000, nan, 1200],
            "amount": [10200, 10400, 12000],
        }
    )

    bars = adapter.get_historical_bars(["600519.SH"], date(2026, 8, 1), date(2026, 8, 3))["600519.SH"]

    # close 为 NaN 的那一根整根丢掉——和 close 缺失是同一种情况。
    assert len(bars) == 2
    assert [bar["close"] for bar in bars] == [10.2, 10.4]
    # 单格的 NaN 只让那一格缺失，不带走整根。
    assert bars[1]["open"] is None
    assert bars[1]["volume"] is None
    assert all(
        value is None or math.isfinite(value)
        for bar in bars
        for value in (bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"])
    )


def test_columnar_bars_tolerate_short_columns() -> None:
    """各列不保证等长，短的那一列越界只该让那一格缺失。"""
    adapter = _columnar_adapter(
        {
            "timestamp": [1_785_000_000_000, 1_785_086_400_000],
            "close": [10.2, 10.4],
            "amount": [10200],  # 比 timestamp 短
        }
    )

    bars = adapter.get_historical_bars(["600519.SH"], date(2026, 8, 1), date(2026, 8, 2))["600519.SH"]

    assert len(bars) == 2
    assert bars[1]["amount"] is None
