"""Persistent, deduplicated ledger of intraday sector forecast confirmations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import read_json, write_json

SCHEMA = "sector-confirmation-ledger-v2"
RECORDABLE = {"已确认", "部分确认", "未确认", "已否决"}


@dataclass
class SectorConfirmationLedger:
    path: Path | None = None

    def __post_init__(self) -> None:
        if self.path is None:
            # v1 used optimistic first-snapshot/zero-delta confirmation rules. Keep its
            # file for audit, but never mix those records into the stricter v2 sample.
            self.path = state_path("market", "sector_confirmation_ledger_v2.json")

    def record(self, confirmation: dict[str, Any]) -> dict[str, Any]:
        payload = self._load()
        target = str(confirmation.get("target_session") or "")
        as_of = confirmation.get("as_of")
        existing = {
            (str(row.get("target_session")), str(row.get("name"))): row
            for row in payload["records"]
        }
        for item in confirmation.get("results") or []:
            status = str(item.get("status") or "")
            name = str(item.get("name") or "")
            if not target or not name or status not in RECORDABLE:
                continue
            key = (target, name)
            row = existing.get(key)
            if row is None:
                row = {
                    "target_session": target,
                    "name": name,
                    "first_as_of": as_of,
                    "transitions": [],
                }
                payload["records"].append(row)
                existing[key] = row
            if row.get("status") != status:
                row["transitions"].append({"as_of": as_of, "status": status})
            row.update(
                {
                    "last_as_of": as_of,
                    "status": status,
                    "scenario": item.get("scenario"),
                    "evidence_members": item.get("evidence_members") or [],
                    "breadth": item.get("breadth"),
                    "excess_pct": item.get("excess_pct"),
                    "amount_share": item.get("amount_share"),
                }
            )
        payload["records"].sort(
            key=lambda row: (str(row.get("target_session")), str(row.get("name")))
        )
        write_json(self.path, payload)
        return self.summary(payload)

    def summary(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or self._load()
        counts = dict.fromkeys(sorted(RECORDABLE), 0)
        sessions = set()
        for row in data["records"]:
            status = row.get("status")
            if status in counts:
                counts[status] += 1
            if row.get("target_session"):
                sessions.add(row["target_session"])
        return {
            "schema": SCHEMA,
            "sessions": len(sessions),
            "records": len(data["records"]),
            "by_latest_status": counts,
            "interpretation": "同一交易日同一主题只保留一条记录，transitions 保存盘中状态变化。",
        }

    def _load(self) -> dict[str, Any]:
        payload = read_json(self.path, default={}) or {}
        if payload.get("schema") != SCHEMA or not isinstance(payload.get("records"), list):
            return {"schema": SCHEMA, "records": []}
        return payload
