"""计划和事件的时点、并发及追加失败恢复。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest

from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import append_jsonl, read_jsonl
from ashare_decision_support.state.workflow import (
    archive_plan,
    read_events,
    read_plans,
    record_event,
)

DAY = date(2026, 8, 3)
MORNING = datetime(2026, 8, 3, 9)
FACTS = {"trade_date": str(DAY), "strategy_version": "v2", "candidates": [{"symbol": "600519.SH"}]}


def test_plan_dedup_keeps_original_time_and_reverts_get_a_new_version(tmp_path):
    first = archive_plan(tmp_path, FACTS, now=MORNING)
    repeat = archive_plan(
        tmp_path, FACTS | {"data_notes": ["取数说明变化"]}, now=MORNING + timedelta(minutes=2)
    )
    assert first == repeat
    second = archive_plan(tmp_path, FACTS | {"candidates": []}, now=MORNING + timedelta(minutes=3))
    third = archive_plan(tmp_path, FACTS, now=MORNING + timedelta(minutes=4))
    assert len({first["plan_id"], second["plan_id"], third["plan_id"]}) == 3
    assert third["plan_revision"] == 3
    plans = read_plans(tmp_path, DAY, until=MORNING + timedelta(hours=1), notes=[])
    assert plans[0]["facts"]["candidates"] == FACTS["candidates"]
    assert plans[1]["supersedes"] == first["plan_id"]


def test_concurrent_plan_creation_deduplicates_inside_the_lock(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: archive_plan(tmp_path, FACTS, now=MORNING), range(12)))
    assert len({row["plan_id"] for row in records}) == 1
    assert len(read_plans(tmp_path, DAY, until=MORNING, notes=[])) == 1


def test_replay_excludes_future_plans_and_accepts_timezone_aware_cutoff(tmp_path):
    archive_plan(tmp_path, FACTS, now=MORNING)
    archive_plan(tmp_path, FACTS | {"candidates": []}, now=MORNING + timedelta(hours=1))
    notes = []
    rows = read_plans(tmp_path, DAY, until=datetime(2026, 8, 3, 1, 30, tzinfo=UTC), notes=notes)
    assert len(rows) == 1
    assert any("截止时点之后" in note for note in notes)


def test_damaged_and_wrong_shape_plan_rows_do_not_break_the_archive(tmp_path):
    first = archive_plan(tmp_path, FACTS, now=MORNING)
    path = tmp_path / "plans" / f"{DAY}.jsonl"
    append_jsonl(path, {"kind": "plan", "plan_id": [], "trade_date": str(DAY), "plan_revision": {}})
    with path.open("ab") as handle:
        handle.write(b'{"partial":')
    next_plan = archive_plan(
        tmp_path, FACTS | {"candidates": []}, now=MORNING + timedelta(minutes=1)
    )
    assert next_plan["plan_revision"] == 2
    notes = []
    rows = read_plans(tmp_path, DAY, until=MORNING + timedelta(hours=1), notes=notes)
    assert [row["plan_id"] for row in rows] == [first["plan_id"], next_plan["plan_id"]]
    assert len(notes) >= 2


def test_event_identity_cannot_be_overridden_by_payload_and_repeat_is_idempotent():
    payload = {"kind": "bad", "at": "wrong", "trade_date": "wrong", "event_id": "bad", "alerts": []}
    first = record_event(DAY, "stock_poll", payload, MORNING)
    assert record_event(DAY, "stock_poll", payload, MORNING) == first
    rows = read_events(DAY, until=MORNING, notes=[])
    assert len(rows) == 1
    assert rows[0]["kind"] == "stock_poll"
    assert rows[0]["trade_date"] == str(DAY)
    assert rows[0]["event_id"] != "bad"
    with pytest.raises(ValueError, match="不一致"):
        record_event(DAY, "stock_poll", {}, MORNING + timedelta(days=1))


@pytest.mark.parametrize("tail", [b'{"valid": 1}', b'{"damaged":', b"\xff"])
def test_append_isolates_old_tail_from_a_large_multibyte_record(tmp_path, tail):
    path = tmp_path / "rows.jsonl"
    path.write_bytes(tail)
    row = {"text": "中文消息" * 5000}
    append_jsonl(path, row)
    assert read_jsonl(path)[-1] == row


def test_append_sync_failure_rolls_back_and_retry_writes_once(tmp_path, monkeypatch):
    path = tmp_path / "rows.jsonl"
    append_jsonl(path, {"before": True})
    old = path.read_bytes()
    with monkeypatch.context() as patch:

        def fail(_):
            raise OSError("disk full")

        patch.setattr("ashare_decision_support.state.store.os.fsync", fail)
        with pytest.raises(OSError):
            append_jsonl(path, {"after": True})
    assert path.read_bytes() == old
    append_jsonl(path, {"after": True})
    assert read_jsonl(path) == [{"before": True}, {"after": True}]


def test_event_replay_skips_corrupt_records_and_future_times():
    record_event(DAY, "stock_poll", {}, MORNING)
    record_event(DAY, "stock_poll", {}, MORNING + timedelta(hours=1))
    append_jsonl(state_path("events", f"{DAY}.jsonl"), {"at": [], "event_id": "broken"})
    notes = []
    assert len(read_events(DAY, until=MORNING, notes=notes)) == 1
    assert any("无效" in note for note in notes)
