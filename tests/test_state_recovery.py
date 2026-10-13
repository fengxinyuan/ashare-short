"""坏状态和落盘失败不能阻断正常任务或丢掉已经生成的风险告警。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from ashare_decision_support.schedule import TradingCalendar, build_timetable, run_daemon
from ashare_decision_support.state import AlertState, DaemonState, Positions
from ashare_decision_support.state.store import write_json

SESSION = date(2026, 8, 3)
MOMENT = datetime(2026, 8, 3, 10, 0)


@pytest.mark.parametrize("payload", [[1], "bad", 123, None])
def test_wrong_top_level_state_shape_is_recoverable(tmp_path, payload) -> None:
    path = tmp_path / "state.json"
    write_json(path, payload)

    assert AlertState.load(SESSION, path).fired_summary() == []
    assert DaemonState.load(SESSION, path).tasks == {}
    assert TradingCalendar(path).configured is False


def test_alert_load_keeps_valid_records_when_neighboring_records_are_damaged(tmp_path) -> None:
    path = tmp_path / "alerts.json"
    write_json(
        path,
        {
            "session": str(SESSION),
            "fired": {
                "600519.SH|entry_zone": {"at": "2026-08-03T02:00:00Z", "count": "bad"},
                "000001.SZ|entry_zone": [1],
            },
            "observed": {"600519.SH": {"vs_vwap": "above"}, "000001.SZ": [1]},
            "pinned": {"600519.SH": {"stop": "99.5", "targets": ["105", "bad", True]}},
            "peaks": {"600519.SH": "110", "000001.SZ": True},
        },
    )

    state = AlertState.load(SESSION, path)

    assert state.last_fired("600519", "entry_zone") == MOMENT
    assert state.fire_count("600519", "entry_zone") == 1
    assert state.fire_count("000001", "entry_zone") == 0
    assert state.state_of("600519") == {"vs_vwap": "above"}
    assert state.state_of("000001") == {}
    assert state.pinned_of("600519")["stop"] == 99.5
    assert state.pinned_of("600519")["targets"] == [105.0]
    assert state.peak_of("600519") == 110
    assert state.peak_of("000001") is None
    assert len(state.fired_summary()) == 1
    state.mark_fired("600519", "entry_zone", MOMENT)
    state.save()
    assert AlertState.load(SESSION, path).fire_count("600519", "entry_zone") == 2


def test_alerts_from_another_session_are_not_reused(tmp_path) -> None:
    path = tmp_path / "alerts.json"
    state = AlertState.load(SESSION, path)
    state.mark_fired("600519", "entry_zone", MOMENT)
    state.observe_peak("600519", 110)
    state.save()

    reloaded = AlertState.load(date(2026, 8, 4), path)

    assert reloaded.fired_summary() == []
    assert reloaded.peak_of("600519") is None


@pytest.mark.parametrize("price", [float("inf"), float("nan"), True, "bad", -1])
def test_invalid_peak_cannot_poison_persisted_alerts(tmp_path, price) -> None:
    path = tmp_path / "alerts.json"
    state = AlertState.load(SESSION, path)
    state.observe_peak("600519", 110)

    assert state.observe_peak("600519", price) == 110
    state.save()
    assert AlertState.load(SESSION, path).peak_of("600519") == 110


def test_daemon_recovers_individual_bad_tasks_without_repeating_completed_tasks(tmp_path) -> None:
    path = tmp_path / "daemon.json"
    write_json(
        path,
        {
            "day": str(SESSION),
            "tasks": {
                "news": {"status": "ok", "attempts": "bad"},
                "morning": {"status": "failed", "attempts": []},
                "review": [1],
            },
        },
    )
    calls = []
    tasks = build_timetable(
        {key: lambda key=key: calls.append(key) for key in ("news", "morning", "review")}
    )

    result = run_daemon(
        tasks,
        day=SESSION,
        state=DaemonState.load(SESSION, path),
        now_fn=lambda: datetime(2026, 8, 3, 16),
        max_ticks=10,
    )

    assert calls == ["morning", "review"]
    assert result.ran == ["morning", "review"]
    assert DaemonState.load(SESSION, path).attempts("morning") == 1


def test_daemon_state_write_failure_preserves_progress_and_task_failure_isolation(
    tmp_path,
    monkeypatch,
) -> None:
    state = DaemonState.load(SESSION, tmp_path / "daemon.json")
    calls = []

    def task_failed():
        calls.append("news")
        raise RuntimeError("source down")

    def disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("ashare_decision_support.state.runstate.write_json", disk_full)
    tasks = build_timetable(
        {
            "news": task_failed,
            "morning": lambda: calls.append("morning") or {"ok": True},
            "review": lambda: calls.append("review"),
        }
    )

    report = run_daemon(
        tasks,
        day=SESSION,
        state=state,
        now_fn=lambda: datetime(2026, 8, 3, 16),
        max_ticks=10,
    )

    assert calls == ["news", "news", "news", "morning", "review"]
    assert report.ran == ["morning", "review"]
    assert state.is_done("morning") is True
    assert state.attempts("news") == 3
    assert any("落盘失败" in note and "重启" in note for note in report.notes)


def test_daemon_omits_non_finite_summary_values_without_losing_completion(tmp_path) -> None:
    path = tmp_path / "daemon.json"
    tasks = build_timetable(
        {
            "morning": lambda: {
                "ok": True,
                "count": 3,
                "score": float("nan"),
                "rate": float("inf"),
            },
        }
    )

    report = run_daemon(
        tasks,
        day=SESSION,
        state=DaemonState.load(SESSION, path),
        now_fn=lambda: datetime(2026, 8, 3, 16),
        max_ticks=5,
    )

    reloaded = DaemonState.load(SESSION, path)
    assert report.ran == ["morning"]
    assert reloaded.is_done("morning") is True
    assert reloaded.tasks["morning"]["summary"] == {"ok": True, "count": 3}


def test_bad_nested_calendar_entries_do_not_hide_valid_holidays(tmp_path) -> None:
    path = tmp_path / "calendar.json"
    write_json(path, {"holidays": ["2026-10-01", {}, "bad", True], "workdays": 123})

    calendar = TradingCalendar(path)

    assert calendar.holidays == {"2026-10-01"}
    assert calendar.workdays == set()
    assert calendar.is_trading_day(date(2026, 10, 1)) is False


def test_calendar_write_failure_keeps_the_in_memory_calendar(tmp_path, monkeypatch) -> None:
    calendar = TradingCalendar(tmp_path / "calendar.json")
    calendar.save(["2026-10-01"])
    before = calendar.path.read_bytes()

    def disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("ashare_decision_support.schedule.calendar.write_json", disk_full)
    with pytest.raises(OSError):
        calendar.save(["2026-10-02"], ["2026-08-01"])

    assert calendar.holidays == {"2026-10-01"}
    assert calendar.workdays == set()
    assert calendar.path.read_bytes() == before


def test_monitor_emits_stop_alert_even_when_state_cannot_be_saved(tmp_path, monkeypatch) -> None:
    from ashare_decision_support.monitor import MonitorSession
    from ashare_decision_support.monitor.loop import run_monitor_loop
    from tests.test_automation import (
        RISE,
        RecordingNotifier,
        ScriptedIntraday,
        StubDaily,
        minute_bars,
    )

    bars = minute_bars(RISE)
    state = AlertState.load(SESSION, tmp_path / "alerts.json")
    feed = ScriptedIntraday([bars, minute_bars([*RISE, RISE[-1]])])
    session = MonitorSession(StubDaily(), feed, SESSION, alert_state=state)
    book = Positions(tmp_path / "positions.json")
    book.open("600519", 100, plan_stop=103)
    session.prime(book.symbols())
    session.load_positions(book)

    def disk_full():
        raise OSError("disk full")

    monkeypatch.setattr(state, "save", disk_full)
    channel = RecordingNotifier()
    moments = iter([datetime(2026, 8, 3, 10, 10), datetime(2026, 8, 3, 10, 11)])

    report = run_monitor_loop(
        session,
        book.symbols,
        [channel],
        now_fn=lambda: next(moments),
        sleep=lambda seconds: None,
        max_passes=2,
    )

    assert report.bad_passes == 0
    assert report.alerts >= 1
    assert state.fire_count("600519", "stop_break") == 1
    assert report.suspensions == 0
    assert len(channel.messages) == 1
    assert any("止损" in str(message) for message in channel.messages)
    assert len([note for note in report.notes if "落盘失败" in note]) == 1


def test_unusable_feed_does_not_rewrite_unchanged_alert_state(tmp_path, monkeypatch) -> None:
    from ashare_decision_support.monitor import MonitorSession
    from tests.test_automation import ScriptedIntraday, StubDaily

    state = AlertState.load(SESSION, tmp_path / "alerts.json")
    session = MonitorSession(StubDaily(), ScriptedIntraday([[]]), SESSION, alert_state=state)

    def unexpected_save():
        pytest.fail("无新告警或观测状态时不应写盘")

    monkeypatch.setattr(state, "save", unexpected_save)

    result = session.poll(["600519.SH"], MOMENT)

    assert result.health.usable is False
    assert result.alerts == []
