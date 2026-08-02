from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

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


def test_tickflow_adapter_health_and_open_features() -> None:
    adapter = TickFlowAdapter(api_key="key", tickflow_module=FakeTickFlowModule())
    health = adapter.health_check()
    features = adapter.get_open_features(["300001.SZ"])

    assert health["mode"] == "full"
    assert health["capabilities"]["supports_intraday"] is True
    item = features["300001.SZ"]
    assert item.data_stale is False
    assert item.minutes_since_open == 5
    assert round(item.open_gap_pct, 4) == round((12.2 / 12.0 - 1.0) * 100.0, 4)
    assert item.volume_ratio > 0


def test_tickflow_adapter_auction_features_are_degraded_without_depth_precision() -> None:
    adapter = TickFlowAdapter(api_key="key", tickflow_module=FakeTickFlowModule())
    features = adapter.get_auction_features(["300001.SZ"])
    item = features["300001.SZ"]

    assert item.symbol == "300001.SZ"
    assert item.matched_qty == 100000
    assert item.matched_amount == 1250000
    assert item.unmatched_qty >= 0
    assert item.indicative_match_price == 12.5


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


def test_daily_proxy_lookback_survives_the_first_of_the_month() -> None:
    """The proxy window used to collapse to month-to-date, yielding 0-2 bars on the 1st."""
    adapter = TickFlowAdapter(api_key=None, tickflow_module=RecordingFreeModule())
    adapter.get_open_features(["603983.SH"])

    call = adapter.client.klines.calls[-1]
    span_days = (call["end_time"] - call["start_time"]) / 86_400_000
    # Must comfortably exceed the 21-bar baseline the volume-ratio helpers need,
    # on every calendar day including the 1st.
    assert span_days > 120
    assert call["adjust"] == "forward"


def test_free_mode_daily_proxy_returns_enough_bars_for_the_volume_baseline() -> None:
    adapter = TickFlowAdapter(api_key=None, tickflow_module=FakeFreeTickFlowModule())
    rows = adapter._daily_proxy_rows("603983.SH")

    assert rows, "degraded daily proxy must not come back empty"
    assert rows[-1]["close"] == 28.67


def test_tickflow_free_mode_daily_proxy_has_reasonable_vwap_and_gap() -> None:
    adapter = TickFlowAdapter(api_key=None, tickflow_module=FakeFreeTickFlowModule())
    feature = adapter.get_open_features(["603983.SH"])["603983.SH"]

    assert feature.data_stale is True
    assert feature.open_gap_pct > 0
    # The daily feed reports volume in lots and amount in yuan; deriving VWAP
    # without reconciling the two used to yield price_vs_vwap_pct = -99%.
    assert abs(feature.price_vs_vwap_pct) < 5


def test_tickflow_free_mode_publishes_no_auction_microstructure() -> None:
    """Free mode has no depth feed, so auction fields must be null, not daily totals."""
    adapter = TickFlowAdapter(api_key=None, tickflow_module=FakeFreeTickFlowModule())
    auction = adapter.get_auction_features(["603983.SH"])["603983.SH"]

    assert auction.data_stale is True
    assert auction.matched_amount is None
    assert auction.matched_qty == 0
    assert auction.unmatched_qty == 0
    assert auction.unmatched_side == "N"
    assert auction.seal_ratio is None
    # A real reference price is still available and still useful.
    assert auction.indicative_match_price > 0
