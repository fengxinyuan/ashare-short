from datetime import date

from ashare_decision_support.adapters.qmt_xtdata import QmtXtDataAdapter


class FakeXtData:
    def connect(self):
        return True

    def subscribe_quote(self, *args, **kwargs):
        return 1

    def subscribe_whole_quote(self, *args, **kwargs):
        return 2

    def get_full_tick(self, symbols):
        return {
            "300001.SZ": {
                "lastPrice": 12.5,
                "volume": 1000,
                "amount": 12500,
                "preClose": 12.0,
                "open": 12.2,
                "time": "2026-07-31T09:33:00",
            }
        }

    def get_l2_quote(self, symbol):
        return {
            "UnmatchedQty": 100,
            "UnmatchedSide": "B",
            "bidVol1": 300,
        }

    def get_instrument_detail(self, symbol):
        return {"symbol": symbol, "exchange": symbol.split(".")[-1]}

    def get_trading_calendar(self, market, start_time="", end_time=""):
        return ["20260731"]

    def get_market_data(self, **kwargs):
        return {
            "open": {"300001.SZ": [12.0]},
            "high": {"300001.SZ": [12.8]},
            "low": {"300001.SZ": [11.9]},
            "close": {"300001.SZ": [12.5]},
            "volume": {"300001.SZ": [1000]},
            "amount": {"300001.SZ": [12500]},
        }


class FakeXtDataNoL2(FakeXtData):
    def get_l2_quote(self, symbol):
        return {}


def test_qmt_adapter_health_check_and_calendar() -> None:
    adapter = QmtXtDataAdapter(xtdata_module=FakeXtData())
    health = adapter.health_check()
    calendar = adapter.get_trading_calendar()

    assert health["connected"] is True
    assert health["capabilities"]["has_get_full_tick"] is True
    assert calendar == [date(2026, 7, 31)]


def test_qmt_adapter_builds_auction_and_open_features() -> None:
    adapter = QmtXtDataAdapter(xtdata_module=FakeXtData())
    auction = adapter.get_auction_features(["300001.SZ"])["300001.SZ"]
    open_feature = adapter.get_open_features(["300001.SZ"])["300001.SZ"]

    assert auction.matched_qty == 1000
    assert auction.unmatched_qty == 100
    assert auction.data_stale is False
    assert open_feature.minutes_since_open == 3
    assert open_feature.open_gap_pct == 1.6666666666666607
    assert open_feature.volume_ratio > 1.0


def test_qmt_adapter_marks_missing_l2_auction_data_as_stale() -> None:
    adapter = QmtXtDataAdapter(xtdata_module=FakeXtDataNoL2())
    auction = adapter.get_auction_features(["300001.SZ"])["300001.SZ"]
    dump_payload = adapter.dump_snapshot(["300001.SZ"])

    assert auction.unmatched_qty == 0
    assert auction.data_stale is True
    assert "full_tick" in dump_payload


def test_qmt_health_reports_not_ready_instead_of_raising() -> None:
    """xtquant 未安装是预期状态；诊断命令必须给出结构化答案而不是抛栈。"""
    import importlib.util

    from ashare_decision_support.cli import _qmt_health

    payload = _qmt_health()

    if importlib.util.find_spec("xtquant") is None:
        assert payload["ready"] is False
        assert payload["connected"] is False
        assert "xtquant" in payload["reason"]
    else:
        assert payload["ready"] is True


def test_source_timestamp_rejects_non_numbers_instead_of_crashing() -> None:
    """`isinstance(raw, (int, float))` 在这里有两个漏法，后果不同。

    - `True` 通过后 `fromtimestamp(1.0)` 是 1970 年——一个看不出错的错时间戳，
      而 `source_ts` 决定简报上那句「数据时点」。
    - NaN 通过后 `fromtimestamp(nan)` 直接抛 ValueError，把一次取数变成一次崩溃。

    两种都该退回「没有时间戳」，由调用方按缺失处理。
    """
    from datetime import datetime

    adapter = QmtXtDataAdapter(xtdata_module=FakeXtData(), auto_connect=False)
    before = datetime.now()
    for bad in (float("nan"), float("inf"), True, "not-a-time"):
        stamp = adapter._extract_source_dt({"sourceTs": bad})
        # 退回 `datetime.now()`，而不是 1970 年、也不是抛异常。
        assert stamp >= before.replace(microsecond=0)
