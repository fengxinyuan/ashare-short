from __future__ import annotations

from datetime import date, datetime

import pytest

from ashare_decision_support.adapters.base import fetch_daily_bars
from ashare_decision_support.adapters.daily_cache import SCHEMA, DailyBarsCache
from ashare_decision_support.agents.collectors import _completed_day_for_metrics
from ashare_decision_support.jobs.post_close import run_post_close
from ashare_decision_support.journal import Journal, Recommendation, verify_pending
from ashare_decision_support.state.store import write_json


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


def test_completed_session_cache_is_reused_until_new_session_is_required(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))
    adapter = _Adapter([_row("2026-09-11")])
    symbols = ["600519.SH"]
    start, end = date(2026, 8, 1), date(2026, 9, 14)
    fetch_daily_bars(adapter, symbols, start, end)

    notes: list[str] = []
    reused = fetch_daily_bars(adapter, symbols, start, end, notes, reuse_through=date(2026, 9, 11))
    assert adapter.calls == 1
    assert reused["600519.SH"][0]["close"] == 10.0
    assert any("已复用完整收盘日日线缓存" in note for note in notes)

    fetch_daily_bars(adapter, symbols, start, end, reuse_through=date(2026, 9, 14))
    assert adapter.calls == 2


def test_cache_completion_day_skips_weekend_and_live_partial_session(monkeypatch) -> None:
    monkeypatch.setattr(
        "ashare_decision_support.agents.collectors.beijing_naive_now",
        lambda: datetime(2026, 9, 14, 9, 40),
    )
    monkeypatch.setattr("ashare_decision_support.agents.collectors.default_calendar", lambda: None)
    assert _completed_day_for_metrics(date(2026, 9, 13)) == date(2026, 9, 11)
    assert _completed_day_for_metrics(date(2026, 9, 14)) == date(2026, 9, 11)

    monkeypatch.setattr(
        "ashare_decision_support.agents.collectors.beijing_naive_now",
        lambda: datetime(2026, 9, 14, 15, 6),
    )
    assert _completed_day_for_metrics(date(2026, 9, 14)) == date(2026, 9, 14)


def test_failed_daily_fetch_uses_cache_and_reports_cutoff(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))
    live = _Adapter([_row("2026-08-25", 11.0)])
    fetch_daily_bars(live, ["600519.SH"], date(2026, 8, 1), date(2026, 8, 26))

    offline = _Adapter(error=OSError("network down"))
    notes: list[str] = []
    result = fetch_daily_bars(offline, ["600519.SH"], date(2026, 8, 1), date(2026, 8, 26), notes)

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
        [_row("2026-08-20", 10.0)] + [_row(f"2026-08-{day:02d}", 11.0) for day in range(21, 26)]
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


def test_same_session_with_different_timestamp_formats_is_replaced(tmp_path) -> None:
    cache = DailyBarsCache(root=tmp_path)
    cache.write("600519.SH", [_row("2026-08-24"), _row("2026-08-25")])

    cache.write("600519.SH", [{"date": "20260825", "close": 12.0}])

    rows = cache.read("600519.SH", date(2026, 8, 1), date(2026, 8, 26))
    assert [row["close"] for row in rows] == [10.0, 12.0]


def test_existing_duplicate_sessions_are_deduplicated_on_read(tmp_path) -> None:
    path = tmp_path / "default__600519.SH.json"
    write_json(
        path,
        {
            "schema": SCHEMA,
            "symbol": "600519.SH",
            "rows": [_row("2026-08-25"), {"date": "2026-08-25", "close": 12.0}],
        },
    )

    rows = DailyBarsCache(root=tmp_path).read("600519.SH", date(2026, 8, 25), date(2026, 8, 25))

    assert [row["close"] for row in rows] == [12.0]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"bad": "\xff"}',
        ('{"schema": "' + SCHEMA + '", "symbol": "600519.SH", "rows": 123}').encode(),
    ],
)
def test_damaged_cache_does_not_prevent_rebuilding(tmp_path, raw) -> None:
    path = tmp_path / "default__600519.SH.json"
    path.write_bytes(raw)
    cache = DailyBarsCache(root=tmp_path)

    assert cache.read("600519.SH", date.min, date.max) == []
    cache.write("600519.SH", [_row("2026-08-25")])
    assert cache.read("600519.SH", date.min, date.max)[0]["close"] == 10.0


def test_unwritable_cache_directory_does_not_block_live_data(monkeypatch) -> None:
    def fail_cache(adapter):
        raise OSError("read-only directory")

    monkeypatch.setattr("ashare_decision_support.adapters.base.cache_for", fail_cache)
    adapter = _Adapter([_row("2026-08-25")])
    notes = []

    rows = fetch_daily_bars(adapter, ["600519.SH"], date(2026, 8, 1), date(2026, 8, 26), notes)

    assert rows["600519.SH"][0]["close"] == 10.0
    assert adapter.calls == 1
    assert any("缓存目录不可用" in note for note in notes)


def test_failed_cache_write_keeps_live_result_and_old_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path))
    fetch_daily_bars(_Adapter([_row("2026-08-25")]), ["600519.SH"], date.min, date.max)
    notes = []

    live = fetch_daily_bars(
        _Adapter([_row("2026-08-25", float("nan"))]), ["600519.SH"], date.min, date.max, notes
    )
    fallback = fetch_daily_bars(
        _Adapter(error=OSError("offline")), ["600519.SH"], date.min, date.max
    )

    assert live["600519.SH"]
    assert fallback["600519.SH"][0]["close"] == 10.0
    assert any("缓存写入失败" in note for note in notes)


def test_post_close_reuses_cache_and_labels_the_actual_data_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))
    fetch_daily_bars(_Adapter([_row("2026-08-25")]), ["600519.SH"], date.min, date.max)

    summary = run_post_close(
        _Adapter(error=OSError("offline")),
        ["600519.SH"],
        tmp_path / "briefs",
        trade_date=date(2026, 8, 26),
        notifiers=[],
        use_limit_pool=False,
        use_trade_flow=False,
        use_lhb_seats=False,
    )

    assert summary["trade_date"] == "2026-08-25"
    assert "2026-08-25" in summary["title"]
    assert any("回退日线缓存" in note for note in summary["data_notes"])
    assert any("非当日数据" in note for note in summary["data_notes"])
