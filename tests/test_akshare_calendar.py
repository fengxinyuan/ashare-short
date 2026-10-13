from datetime import date
from types import SimpleNamespace

import pytest

from ashare_decision_support.adapters.akshare_calendar import AkshareCalendarAdapter
from ashare_decision_support.schedule.calendar import TradingCalendar


class FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, *, orient):
        assert orient == "records"
        return self._rows


def test_calendar_refresh_uses_only_the_calendar_endpoint(tmp_path) -> None:
    adapter = AkshareCalendarAdapter(
        ak_module=SimpleNamespace(
            tool_trade_date_hist_sina=lambda: FakeFrame(
                [
                    {"trade_date": date(2025, 12, 31)},
                    {"交易日": "20260102"},
                    {"date": "2026-01-05T00:00:00+08:00"},
                ]
            ),
        ),
    )
    calendar = TradingCalendar(tmp_path / "holidays.json")

    assert calendar.refresh_from(adapter, 2026) == 2
    assert calendar.is_trading_day(date(2026, 1, 1)) is False
    assert calendar.is_trading_day(date(2026, 1, 2)) is True
    assert calendar.is_trading_day(date(2026, 1, 6)) is True


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "invalid",
        [{"trade_date": "2026-01-02"}, {"trade_date": "broken"}],
        [{"trade_date": "2026-01-02"}, {}],
        [{"trade_date": "2026-01-02"}, None],
    ],
)
def test_unusable_calendar_response_preserves_saved_holidays(tmp_path, payload) -> None:
    adapter = AkshareCalendarAdapter(
        ak_module=SimpleNamespace(tool_trade_date_hist_sina=lambda: payload),
    )
    calendar = TradingCalendar(tmp_path / "holidays.json")
    calendar.save(["2026-01-01", "2026-01-05"])
    before = calendar.path.read_bytes()

    assert calendar.refresh_from(adapter, 2026) == 0
    assert calendar.path.read_bytes() == before


def test_unavailable_calendar_endpoint_preserves_saved_holidays(tmp_path) -> None:
    adapter = AkshareCalendarAdapter(ak_module=SimpleNamespace())
    calendar = TradingCalendar(tmp_path / "holidays.json")
    calendar.save(["2026-01-01"])
    before = calendar.path.read_bytes()

    assert calendar.refresh_from(adapter, 2026) == 0
    assert calendar.path.read_bytes() == before
