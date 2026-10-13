"""Small disk cache for historical daily bars.

The cache is deliberately a fallback, not a second data source: live data is
always attempted first, and cached rows are only used after the adapter fails.
Every fallback is reported by the caller so stale data cannot look current.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.jsoncache import read_stamped
from ashare_decision_support.signals.series import bar_date
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import file_lock, write_json

SCHEMA = "ashare-daily-bars-cache-v1"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _dated_rows(rows: Any) -> dict[date, dict[str, Any]]:
    """日线按交易日唯一；同一天不同时间格式的后到记录覆盖旧记录。"""
    if not isinstance(rows, list):
        return {}
    return {
        stamp: row for row in rows if isinstance(row, dict) and (stamp := bar_date(row)) is not None
    }


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
        payload = read_stamped(self._path(symbol), SCHEMA)
        if payload is None or payload.get("symbol") != symbol:
            return []
        dated = _dated_rows(payload.get("rows"))
        return [dated[stamp] for stamp in sorted(dated) if start <= stamp <= end]

    def latest_date(self, rows: list[dict[str, Any]]) -> date | None:
        dates = [stamp for row in rows if (stamp := bar_date(row)) is not None]
        return max(dates) if dates else None

    def write(self, symbol: str, rows: list[dict[str, Any]]) -> None:
        fresh = _dated_rows(rows)
        if not fresh:
            return
        path = self._path(symbol)
        with file_lock(path):
            stored = read_stamped(path, SCHEMA)
            merged = (
                _dated_rows(stored.get("rows"))
                if stored is not None and stored.get("symbol") == symbol
                else {}
            )
            merged.update(fresh)
            ordered = [merged[stamp] for stamp in sorted(merged)]
            payload = {"schema": SCHEMA, "symbol": symbol, "rows": ordered}
            write_json(path, payload, indent=None)


def cache_for(adapter: Any) -> DailyBarsCache:
    namespace = type(adapter).__name__
    adjust = getattr(adapter, "adjust", None)
    if adjust:
        namespace += f"_{adjust}"
    return DailyBarsCache(namespace=namespace)
