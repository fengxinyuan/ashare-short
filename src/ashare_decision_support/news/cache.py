"""Day-scoped JSON cache for news feeds.

The pre-open job may be run several times while you are reading the brief, and
every run would otherwise re-hit the same public endpoints. Caching per trading
day per feed keeps repeat runs free and, more importantly, makes the morning
reproducible: two runs at 07:40 and 08:20 read the same documents.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

CACHE_DIRNAME = "news_cache"


class NewsCache:
    def __init__(self, root: str | Path, ttl_seconds: int = 3600) -> None:
        self.root = Path(root) / CACHE_DIRNAME
        self.ttl_seconds = ttl_seconds

    def path_for(self, trade_date: date, key: str) -> Path:
        safe_key = "".join(char if char.isalnum() or char in "-_" else "_" for char in key)
        return self.root / str(trade_date) / f"{safe_key}.json"

    def read(self, trade_date: date, key: str) -> Any | None:
        path = self.path_for(trade_date, key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        stamp = payload.get("cached_at")
        if not isinstance(stamp, str):
            return None
        try:
            cached_at = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        age = (datetime.now(timezone.utc) - cached_at).total_seconds()
        if age > self.ttl_seconds:
            return None
        return payload.get("data")

    def write(self, trade_date: date, key: str, data: Any) -> bool:
        path = self.path_for(trade_date, key)
        payload = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "trade_date": str(trade_date),
            "key": key,
            "data": data,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # `default=str` because feeds hand back native date/Timestamp values
            # (news_cctv does) that json cannot encode. Stringifying is correct
            # here: the cache round-trips into text-scoring code either way.
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        except (OSError, TypeError, ValueError):
            # A cache that cannot be written is a performance problem, never a
            # correctness one — the caller already has the live data.
            return False
        return True
