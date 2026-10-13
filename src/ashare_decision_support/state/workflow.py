"""不可变的每日计划版本与盘中过程记录，供盘后复盘读取。"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.journal.records import DEFAULT_STRATEGY_VERSION
from ashare_decision_support.numeric import as_int
from ashare_decision_support.schedule.timezone import beijing_naive_now, to_beijing_naive
from ashare_decision_support.signals.series import parse_bar_date, parse_bar_datetime
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import append_jsonl, file_lock, read_jsonl


def archive_plan(
    output_dir: str | Path,
    facts: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """相邻内容一致时复用版本，变更和改回旧方案都追加新版本。"""
    day = parse_bar_date(facts.get("trade_date"))
    if day is None:
        raise ValueError("盘前计划缺少有效交易日")
    path = Path(output_dir) / "plans" / f"{day}.jsonl"
    volatile = {"plan_id", "plan_revision", "generated_at", "data_notes"}
    content = {key: value for key, value in facts.items() if key not in volatile}
    content["strategy_version"] = facts.get("strategy_version") or DEFAULT_STRATEGY_VERSION
    digest = _digest(content)
    with file_lock(path):
        rows = [row for row in read_jsonl(path) if _valid_plan(row, day)]
        previous = rows[-1] if rows else {}
        if previous.get("content_hash") == digest:
            return _plan_metadata(previous)
        stamp = to_beijing_naive(now or beijing_naive_now()).isoformat(timespec="microseconds")
        plan_id = _digest([str(day), previous.get("plan_id"), digest])
        revision = max((as_int(row.get("plan_revision")) for row in rows), default=0) + 1
        row = {
            "kind": "plan",
            "trade_date": str(day),
            "plan_id": plan_id,
            "plan_revision": revision,
            "generated_at": stamp,
            "strategy_version": facts.get("strategy_version") or DEFAULT_STRATEGY_VERSION,
            "supersedes": previous.get("plan_id"),
            "content_hash": digest,
        }
        row["facts"] = {**facts, **_plan_metadata(row)}
        append_jsonl(path, row)
    return _plan_metadata(row)


def read_plans(
    output_dir: str | Path,
    day: date,
    *,
    until: datetime,
    notes: list[str],
) -> list[dict]:
    path = Path(output_dir) / "plans" / f"{day}.jsonl"
    until = to_beijing_naive(until)
    result = []
    for row in read_jsonl(path, notes):
        stamp = parse_bar_datetime(row.get("generated_at"))
        if not _valid_plan(row, day):
            notes.append("计划档案中有日期、时点、编号或事实无效的版本，已跳过")
            continue
        if stamp > until:
            notes.append("存在复盘截止时点之后生成的计划，未用作当时判断")
            continue
        result.append({**row, "source_ref": f"{path.resolve()}#{row['plan_id']}"})
    return sorted(
        result,
        key=lambda row: (parse_bar_datetime(row["generated_at"]), as_int(row.get("plan_revision"))),
    )


def record_event(
    day: date,
    kind: str,
    payload: dict[str, Any],
    at: datetime,
) -> str:
    """按事件内容去重；日志写入失败由调用方保留业务结果并报告。"""
    moment = to_beijing_naive(at)
    if moment.date() != day:
        raise ValueError("过程记录时点与交易日不一致")
    row = {
        **payload,
        "kind": kind,
        "trade_date": str(day),
        "at": moment.isoformat(timespec="microseconds"),
    }
    row.pop("event_id", None)
    event_id = _digest(row)
    row["event_id"] = event_id
    path = state_path("events", f"{day}.jsonl")
    with file_lock(path):
        if not any(item.get("event_id") == event_id for item in read_jsonl(path)):
            append_jsonl(path, row)
    return event_id


def read_events(day: date, *, until: datetime, notes: list[str]) -> list[dict]:
    path = state_path("events", f"{day}.jsonl")
    until = to_beijing_naive(until)
    result: dict[str, dict] = {}
    for row in read_jsonl(path, notes):
        stamp = parse_bar_datetime(row.get("at"))
        event_id = row.get("event_id")
        if stamp is None or stamp.date() != day or parse_bar_date(row.get("trade_date")) != day:
            notes.append("过程日志中有日期或时点无效的记录，已跳过")
            continue
        if stamp > until:
            continue
        if not isinstance(event_id, str) or not event_id:
            notes.append("过程日志中有缺少编号的记录，已跳过")
            continue
        result[event_id] = {**row, "source_ref": f"{path.resolve()}#{event_id}"}
    return sorted(result.values(), key=lambda row: (parse_bar_datetime(row["at"]), row["event_id"]))


def _valid_plan(row: dict, day: date) -> bool:
    facts = row.get("facts")
    return (
        row.get("kind") == "plan"
        and parse_bar_date(row.get("trade_date")) == day
        and parse_bar_datetime(row.get("generated_at")) is not None
        and isinstance(row.get("plan_id"), str)
        and bool(row["plan_id"])
        and isinstance(facts, dict)
        and parse_bar_date(facts.get("trade_date")) == day
        and as_int(row.get("plan_revision")) > 0
    )


def _plan_metadata(row: dict) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in ("plan_id", "plan_revision", "generated_at", "strategy_version")
    }


def _digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:24]
