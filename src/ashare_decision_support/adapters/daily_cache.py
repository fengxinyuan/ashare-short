"""Small disk cache for historical daily bars.

The cache is deliberately a fallback, not a second data source: live data is
always attempted first, and cached rows are only used after the adapter fails.
Every fallback is reported by the caller so stale data cannot look current.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.state.paths import state_path

SCHEMA = "ashare-daily-bars-cache-v1"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _stamp(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text[:10], text[:8]):
        try:
            if len(candidate) == 8:
                return datetime.strptime(candidate, "%Y%m%d").date().isoformat()
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            continue
    return None


def row_date(row: dict[str, Any]) -> date | None:
    for key in ("timestamp", "date", "datetime", "日期", "时间"):
        stamp = _stamp(row.get(key))
        if stamp:
            return date.fromisoformat(stamp)
    return None


class DailyBarsCache:
    """Per-symbol JSON cache shared by all historical-bar callers."""

    def __init__(self, *, namespace: str = "default", root: Path | None = None) -> None:
        self.root = root or state_path("cache", "daily_bars")
        self.namespace = _SAFE.sub("_", namespace) or "default"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        safe = _SAFE.sub("_", symbol) or "unknown"
        return self.root / f"{self.namespace}__{safe}.json"

    def read(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self._path(symbol).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            return []
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return []
        result = [
            row
            for row in rows
            if isinstance(row, dict) and (stamp := row_date(row)) is not None
            and start <= stamp <= end
        ]
        return sorted(result, key=lambda row: row_date(row) or date.min)

    def latest_date(self, rows: list[dict[str, Any]]) -> date | None:
        dates = [stamp for row in rows if (stamp := row_date(row)) is not None]
        return max(dates) if dates else None

    def write(self, symbol: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        path = self._path(symbol)
        existing: list[dict[str, Any]] = []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("schema") == SCHEMA:
                existing = [row for row in payload.get("rows", []) if isinstance(row, dict)]
        except (OSError, json.JSONDecodeError):
            pass
        merged = {
            (row_date(row), str(row.get("timestamp") or row.get("date") or "")): row
            for row in [*existing, *rows]
            if row_date(row) is not None
        }
        ordered = sorted(merged.values(), key=lambda row: row_date(row) or date.min)
        payload = {"schema": SCHEMA, "symbol": symbol, "rows": ordered}
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        temp.replace(path)


def cache_for(adapter: Any) -> DailyBarsCache:
    namespace = type(adapter).__name__
    adjust = getattr(adapter, "adjust", None)
    if adjust:
        namespace += f"_{adjust}"
    return DailyBarsCache(namespace=namespace)
