from __future__ import annotations

from datetime import date, datetime

import pytest

from ashare_decision_support.adapters.base import fetch_daily_bars
from ashare_decision_support.journal import Journal, Recommendation, verify_pending


def _row(day: str, close: float = 10.0) -> dict:
    return {
        "symbol": "600519.SH",
        "timestamp": datetime.fromisoformat(f"{day} 15:00:00"),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
    }


class _Adapter:
    adjust = "forward"

    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.calls = 0

    def get_historical_bars(self, symbols, start, end):
        self.calls += 1
        if self.error:
            raise self.error
        return {symbol: [dict(row, symbol=symbol) for row in self.rows] for symbol in symbols}


def test_successful_daily_fetch_is_cached(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))
    adapter = _Adapter([_row("2026-08-25")])
    notes: list[str] = []

    result = fetch_daily_bars(adapter, ["600519.SH"], date(2026, 8, 1), date(2026, 8, 26), notes)

    assert result["600519.SH"][0]["close"] == 10.0
    assert adapter.calls == 1
    assert notes == []
    assert list((tmp_path / "state" / "cache" / "daily_bars").glob("*.json"))


def test_failed_daily_fetch_uses_cache_and_reports_cutoff(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))
    live = _Adapter([_row("2026-08-25", 11.0)])
    fetch_daily_bars(live, ["600519.SH"], date(2026, 8, 1), date(2026, 8, 26))

    offline = _Adapter(error=OSError("network down"))
    notes: list[str] = []
    result = fetch_daily_bars(
        offline, ["600519.SH"], date(2026, 8, 1), date(2026, 8, 26), notes
    )

    assert result["600519.SH"][0]["close"] == 11.0
    assert any("回退日线缓存" in note for note in notes)
    assert any("2026-08-25" in note for note in notes)
    assert any("不可视为实时" in note for note in notes)


def test_failed_daily_fetch_without_cache_still_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))
    with pytest.raises(OSError, match="network down"):
        fetch_daily_bars(
            _Adapter(error=OSError("network down")),
            ["600519.SH"],
            date(2026, 8, 1),
            date(2026, 8, 26),
        )


def test_verification_reports_when_it_uses_cached_daily_bars(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))
    live = _Adapter(
        [_row("2026-08-20", 10.0)]
        + [_row(f"2026-08-{day:02d}", 11.0) for day in range(21, 26)]
    )
    fetch_daily_bars(live, ["600519.SH"], date(2026, 8, 1), date(2026, 8, 26))

    journal = Journal(root=tmp_path / "journal")
    journal.record(
        Recommendation(
            symbol="600519.SH",
            trade_date="2026-08-20",
            source="morning",
            reference_price=10.0,
            entry_high_pct=0.0,
            stop_pct=-0.05,
            target_pcts=[0.08],
        )
    )

    result = verify_pending(
        journal,
        _Adapter(error=OSError("network down")),
        today=date(2026, 8, 26),
    )

    assert result["resolved"] == 1
    assert any("回退日线缓存" in note for note in result["notes"])
    assert any("不可视为实时" in note for note in result["notes"])
