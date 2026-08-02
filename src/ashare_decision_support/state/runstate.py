"""Which scheduled tasks have already run today.

Two jobs this does. Idempotency: the daemon checks its timetable on a short
tick, so without a record it would re-run the 08:20 job on every tick until
08:21. Restart recovery: a daemon restarted at 13:00 must not re-send the
morning brief, and must still run the afternoon tasks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import read_json, write_json

STATE_FILE = "daemon_state.json"

STATUS_OK = "ok"
STATUS_FAILED = "failed"


@dataclass
class DaemonState:
    day: date
    path: Path
    tasks: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, day: date, path: Path | None = None) -> "DaemonState":
        resolved = Path(path) if path is not None else state_path(STATE_FILE)
        payload = read_json(resolved, default={}) or {}
        # A state file from a previous day describes a previous day. Starting
        # clean is the only correct reading of it.
        if payload.get("day") != str(day):
            return cls(day=day, path=resolved, tasks={})
        return cls(day=day, path=resolved, tasks=payload.get("tasks") or {})

    def is_done(self, task: str) -> bool:
        """Only a success counts as done — a failed task is retried next tick."""
        return self.tasks.get(task, {}).get("status") == STATUS_OK

    def attempts(self, task: str) -> int:
        return int(self.tasks.get(task, {}).get("attempts", 0))

    def record(
        self,
        task: str,
        status: str,
        now: datetime,
        summary: Any = None,
        error: str | None = None,
    ) -> None:
        previous = self.tasks.get(task, {})
        self.tasks[task] = {
            "status": status,
            "at": now.isoformat(timespec="seconds"),
            "attempts": int(previous.get("attempts", 0)) + 1,
            "summary": summary,
            "error": error,
        }
        self.save()

    def save(self) -> None:
        write_json(self.path, {"day": str(self.day), "tasks": self.tasks})
