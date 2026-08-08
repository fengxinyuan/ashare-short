"""Import audited social-topic snapshots without scraping consumer platforms.

Short-video and community-platform trends are useful discovery signals, but
they are neither primary facts nor reliable investment evidence.  This adapter
therefore reads an externally obtained daily JSON snapshot (official API,
licensed provider, or a manually exported public list) and deliberately keeps
those documents out of the sector scorer.  ``ThemeRadar`` may use them only to
surface a topic for verification against market and primary-news evidence.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.news.dedup import parse_published
from ashare_decision_support.state.store import read_json

ENV_SOCIAL_HEAT_PATH = "ASHARE_DS_SOCIAL_HEAT_PATH"
SUPPORTED_PLATFORMS = {"xiaohongshu", "douyin", "weibo", "other"}
MAX_SNAPSHOT_AGE_DAYS = 3


class SocialSnapshotSource:
    """Read one or more daily snapshots supplied through an approved channel."""

    name = "social_snapshot"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @classmethod
    def from_env(cls) -> SocialSnapshotSource | None:
        value = os.environ.get(ENV_SOCIAL_HEAT_PATH, "").strip()
        return cls(value) if value else None

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        files = self._files_for(trade_date)
        if not files:
            return [], [f"未配置或未找到社媒热点快照（{self.path}），主题雷达不含社媒线索"]

        docs: list[NewsDoc] = []
        notes: list[str] = []
        for path in files:
            payload = read_json(path, default=None)
            if payload is None:
                notes.append(f"社媒热点快照无法解析：{path}")
                continue
            rows, platform = _rows(payload)
            if platform not in SUPPORTED_PLATFORMS:
                notes.append(f"社媒快照 {path.name} 的 platform 不受支持，已跳过")
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                title = str(row.get("title") or row.get("topic") or "").strip()
                if not title:
                    continue
                published_at = str(row.get("published_at") or row.get("as_of") or "").strip()
                published = parse_published(published_at)
                if published is not None and (trade_date - published.date()).days > MAX_SNAPSHOT_AGE_DAYS:
                    continue
                docs.append(
                    NewsDoc(
                        title=title,
                        summary=str(row.get("summary") or "").strip(),
                        source=f"社媒快照:{platform}",
                        published_at=published_at,
                        url=str(row.get("url") or "").strip(),
                        engagement=_engagement(row),
                        heat_rank=_int_or_none(row.get("rank") or row.get("heat_rank")),
                    )
                )
        return docs[: max(0, limit)], notes

    def _files_for(self, trade_date: date) -> list[Path]:
        if self.path.is_file():
            return [self.path]
        if not self.path.is_dir():
            return []
        return sorted(self.path.glob(f"*{trade_date:%Y%m%d}*.json"))


def _rows(payload: Any) -> tuple[list[Any], str]:
    if isinstance(payload, list):
        return payload, "other"
    if not isinstance(payload, dict):
        return [], "other"
    rows = payload.get("items") or payload.get("topics") or []
    platform = str(payload.get("platform") or "other").strip().lower()
    return rows if isinstance(rows, list) else [], platform


def _engagement(row: dict[str, Any]) -> float | None:
    values = [row.get(key) for key in ("engagement", "heat", "likes", "收藏", "评论", "转发")]
    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    return sum(numbers) if numbers else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
