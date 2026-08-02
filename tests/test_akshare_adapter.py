from datetime import date

from ashare_decision_support.adapters.akshare_adapter import AkshareAdapter


class FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient="records"):
        if orient != "records":
            raise ValueError("unsupported orient")
        return self._rows


class FakeAkshare:
    def stock_zh_a_spot_em(self):
        return FakeFrame(
            [
                {
                    "代码": "300001",
                    "名称": "示例一",
                    "最新价": 12.5,
                    "今开": 12.2,
                    "昨收": 12.0,
                    "成交量": 10.0,
                    "成交额": 125000.0,
                    "量比": 1.4,
                },
                {
                    "代码": "600001",
                    "名称": "示例二",
                    "最新价": 9.5,
                    "今开": 9.8,
                    "昨收": 10.0,
                    "成交量": 8.0,
                    "成交额": 76000.0,
                    "量比": 0.7,
                },
            ]
        )

    def stock_zh_a_hist(self, **kwargs):
        return FakeFrame([{"日期": "2026-07-30", "开盘": 12.0, "收盘": 12.5}])

    def stock_zh_a_hist_min_em(self, **kwargs):
        return FakeFrame(
            [
                {"时间": "2026-07-31 09:31:00", "收盘": 12.3, "均价": 12.25, "成交量": 4.0, "成交额": 49000.0},
                {"时间": "2026-07-31 09:33:00", "收盘": 12.5, "均价": 12.35, "成交量": 6.0, "成交额": 76000.0},
            ]
        )

    def tool_trade_date_hist_sina(self):
        return FakeFrame([{"trade_date": "2026-07-31"}])

    def stock_zt_pool_dtgc_em(self, **kwargs):
        return FakeFrame([])

    def stock_board_concept_name_em(self):
        return FakeFrame([])


def test_akshare_adapter_health_and_calendar() -> None:
    adapter = AkshareAdapter(ak_module=FakeAkshare())
    health = adapter.health_check()
    calendar = adapter.get_trading_calendar()

    assert health["capabilities"]["has_stock_zh_a_spot_em"] is True
    assert calendar == [date(2026, 7, 31)]


def test_akshare_adapter_builds_open_features() -> None:
    adapter = AkshareAdapter(ak_module=FakeAkshare())
    features = adapter.get_open_features(["300001.SZ"])
    item = features["300001.SZ"]

    assert item.minutes_since_open == 3
    assert round(item.open_gap_pct, 4) == round((12.2 / 12.0 - 1.0) * 100.0, 4)
    assert item.volume_ratio == 1.4
    assert item.price_vs_vwap_pct > 0


def test_akshare_adapter_degrades_auction_features() -> None:
    adapter = AkshareAdapter(ak_module=FakeAkshare())
    features = adapter.get_auction_features(["300001.SZ"])
    item = features["300001.SZ"]

    assert item.data_stale is True
    assert item.unmatched_qty == 0
    assert item.matched_amount == 125000.0
