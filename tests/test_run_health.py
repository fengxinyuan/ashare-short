"""事实包里的时点判定：这份数据是哪天的、哪几块根本没有。

钉住的是**周一早上那个错**：上周五跑的盘前简报，在周一读起来和当天跑出来的一模
一样。按自然日判会说「3 天前」并误报陈旧，按交易日判才知道它就是上一个交易日。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from ashare_decision_support.ask.context import assemble
from ashare_decision_support.runhealth import (
    STATUS_FAILED,
    STATUS_FUTURE,
    STATUS_LATEST,
    STATUS_MISSING,
    STATUS_PREVIOUS,
    STATUS_STALE,
    STATUS_TODAY,
    STATUS_UNKNOWN,
    HealthLedger,
)


class _Calendar:
    """节假日表的桩。周末不开市，其余按给定的休市日。"""

    configured = True

    def __init__(self, holidays: set[str] | None = None) -> None:
        self.holidays = holidays or set()

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and str(day) not in self.holidays


def _ledger(today: date, calendar: object | None = None) -> HealthLedger:
    return HealthLedger(today=today, calendar=calendar if calendar is not None else _Calendar())


def test_the_same_day_is_not_behind_at_all() -> None:
    read = _ledger(date(2026, 8, 12)).stamped("盘前主链路", "2026-08-12")
    assert (read.status, read.sessions_behind) == (STATUS_TODAY, 0)
    assert not read.degraded


def test_fridays_data_read_on_monday_is_the_previous_session_not_three_days_old() -> None:
    """这一条是整个模块存在的理由。按自然日算是 3 天，按交易日算是 1 个。"""
    read = _ledger(date(2026, 8, 17)).stamped("盘前主链路", "2026-08-14")  # 周五 → 周一
    assert (read.status, read.sessions_behind) == (STATUS_PREVIOUS, 1)
    assert not read.degraded


def test_skipping_a_whole_session_is_stale() -> None:
    read = _ledger(date(2026, 8, 18)).stamped("盘前主链路", "2026-08-14")  # 周五 → 周二
    assert (read.status, read.sessions_behind) == (STATUS_STALE, 2)
    assert read.degraded
    assert "已落后 2 个交易日" in read.describe()


def test_a_holiday_in_between_does_not_count_as_a_session() -> None:
    calendar = _Calendar(holidays={"2026-08-17"})
    read = _ledger(date(2026, 8, 18), calendar).stamped("盘前主链路", "2026-08-14")
    assert (read.status, read.sessions_behind) == (STATUS_PREVIOUS, 1)


def test_without_a_holiday_table_the_reading_says_so() -> None:
    """说不准就说不准：长假会被算多，那句话必须跟着读数走。"""
    read = HealthLedger(today=date(2026, 8, 18)).stamped("盘前主链路", "2026-08-14")
    assert "未经节假日校验" in read.detail


def test_eight_digit_stamps_parse_and_garbage_does_not() -> None:
    ledger = _ledger(date(2026, 8, 18))
    assert ledger.stamped("板块指数", "20260818").status == STATUS_TODAY
    # 认不出来的日期不猜：猜错一位就是把当日数据判成陈旧，或者反过来。
    assert ledger.stamped("坏日期", "banana").sessions_behind is None


def test_missing_blocks_are_listed_as_needing_attention() -> None:
    ledger = _ledger(date(2026, 8, 18))
    ledger.stamped("盘前主链路", "2026-08-18")
    ledger.missing("参数化策略扫描", "尚无落盘结果")
    payload = ledger.to_dict()

    assert payload["判定"] == "降级"
    assert [item["名称"] for item in payload["块"]] == ["盘前主链路", "参数化策略扫描"]
    assert any(STATUS_MISSING in line or "缺失" in line for line in payload["需要注意"])


def test_an_empty_ledger_is_not_reported_as_healthy() -> None:
    """全部新鲜和「根本没装配任何落盘产出」不是一回事。"""
    assert _ledger(date(2026, 8, 18)).to_dict()["判定"] == "未记录"


def test_the_facts_bundle_carries_the_reading(tmp_path) -> None:
    payload = {"trade_date": "2026-08-10", "position_factor": 0.3, "candidates": []}
    (tmp_path / "morning_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    ctx = assemble(datetime(2026, 8, 14, 8, 30), output_dir=tmp_path)
    health = ctx.blocks["数据健康"]

    assert health["判定"] == "降级"
    morning = next(item for item in health["块"] if item["名称"] == "盘前主链路")
    assert morning["数据日期"] == "2026-08-10"
    assert morning["落后交易日"] >= 2
    # 陈旧也要出现在散文里：`render()` 是顺着读的，读的人要在看那份排序之前知道。
    assert any("已落后" in note for note in ctx.notes)


@pytest.mark.parametrize("stamp", [None, "", "banana", "2026-08-18 invalid"])
def test_missing_or_invalid_timestamp_is_degraded_instead_of_live(stamp) -> None:
    ledger = _ledger(date(2026, 8, 18))

    reading = ledger.stamped("盘前主链路", stamp)

    assert reading.status == STATUS_UNKNOWN
    assert reading.degraded
    assert ledger.to_dict()["判定"] == "降级"
    assert "无法确认时效" in reading.describe()


def test_future_data_is_not_reported_as_current() -> None:
    ledger = _ledger(date(2026, 8, 18))

    reading = ledger.stamped("次日执行卡", "2026-08-19")

    assert reading.status == STATUS_FUTURE
    assert reading.sessions_behind is None
    assert reading.degraded
    assert "2026-08-19" in reading.describe()
    assert ledger.to_dict()["判定"] == "降级"


def test_weekend_data_retains_its_actual_session_label() -> None:
    reading = _ledger(date(2026, 8, 16)).stamped("次日执行卡", "2026-08-14")

    assert reading.status == STATUS_LATEST
    assert reading.sessions_behind == 0
    assert not reading.degraded


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-08-17T16:30:00+00:00",
        datetime.fromisoformat("2026-08-17T16:30:00+00:00"),
    ],
)
def test_timezone_aware_stamps_use_the_beijing_date(stamp) -> None:
    reading = _ledger(date(2026, 8, 18)).stamped("板块数据", stamp)

    assert reading.trade_date == "2026-08-18"
    assert reading.status == STATUS_TODAY


def test_ancient_data_counts_recent_calendar_sessions() -> None:
    class RecentCalendar(_Calendar):
        def is_trading_day(self, day: date) -> bool:
            return day.year >= 2026 and super().is_trading_day(day)

    reading = _ledger(date(2026, 8, 18), RecentCalendar()).stamped("旧数据", "2000-01-01")

    assert reading.status == STATUS_STALE
    assert reading.sessions_behind >= 2


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '{"trade_date": "2026-08-18", "sectors": 123}',
        '{"trade_date": "2026-08-18", "position_factor": NaN}',
    ],
)
def test_invalid_payload_does_not_crash_or_enter_the_facts_bundle(tmp_path, raw) -> None:
    (tmp_path / "morning_latest.json").write_text(raw, encoding="utf-8")
    (tmp_path / "strategy_screen_latest.json").write_text(
        '{"trade_date": "2026-08-18", "profiles": {}}', encoding="utf-8"
    )

    ctx = assemble(datetime(2026, 8, 18, 8, 30), output_dir=tmp_path)

    assert "盘前主链路" not in ctx.blocks
    assert "参数化策略扫描" in ctx.blocks
    health = next(row for row in ctx.blocks["数据健康"]["块"] if row["名称"] == "盘前主链路")
    assert health["判定"] == STATUS_FAILED


@pytest.mark.parametrize("stamp", ["20260914", "bad-date"])
def test_morning_does_not_use_future_or_undated_rotation(monkeypatch, stamp) -> None:
    from ashare_decision_support.jobs import morning_fuse

    reading = SimpleNamespace(
        ok=True, trade_date=stamp, notes=[], observations=["不应输出的轮动结论"]
    )
    monkeypatch.setattr(morning_fuse, "load_all_series", lambda: {"sector": []})
    for name in ("read_rotation", "read_turnover", "read_seesaw"):
        monkeypatch.setattr(morning_fuse, name, lambda series: reading)
    notes = []

    result = morning_fuse._rotation_from_series(date(2026, 9, 12), notes)

    assert result == (None, None, None)
    assert any("本次不使用" in note for note in notes)
    assert "不应输出的轮动结论" not in notes
