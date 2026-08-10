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
