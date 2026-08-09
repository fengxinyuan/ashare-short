"""Persist and summarise recent sector leadership.

This fills one of the project-wide gaps called out in `docs/ROADMAP.md`: the
system could rank sectors for *today*, but it had no structured memory of what
led over the last few sessions. The goal here is deliberately modest:

- store the recent top-ranked sectors under the state directory;
- derive a lightweight "主线延续 / 温和轮动 / 轮动加快" summary from that history;
- keep the summary deterministic and local to the sector layer.

It does **not** invent a second scoring system. The memory reads the ranking the
project already produced and describes how stable that ranking has been.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.sectors.base import SectorOutlook
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import read_json, write_json

HISTORY_SCHEMA = "sector-rotation-v1"
DEFAULT_LEADER_COUNT = 5
DEFAULT_LOOKBACK_DAYS = 5
DEFAULT_HISTORY_DEPTH = 20
DEFAULT_PATH = ("market", "sector_rotation_history.json")


@dataclass(frozen=True)
class RotationSummary:
    """Recent leadership summary for the current trade date."""

    level: str
    trade_date: str
    lookback_days: int
    leader_count: int
    regime: str
    available_days: int
    overlap_with_yesterday: float | None = None
    persistent: tuple[str, ...] = ()
    new_entries: tuple[str, ...] = ()
    faded: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.observations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "trade_date": self.trade_date,
            "lookback_days": self.lookback_days,
            "leader_count": self.leader_count,
            "regime": self.regime,
            "available_days": self.available_days,
            "overlap_with_yesterday": self.overlap_with_yesterday,
            "persistent": list(self.persistent),
            "new_entries": list(self.new_entries),
            "faded": list(self.faded),
            "observations": list(self.observations),
        }


class SectorRotationStore:
    """Append-safe daily memory for sector leadership."""

    def __init__(
        self,
        path: str | Path | None = None,
        history_depth: int = DEFAULT_HISTORY_DEPTH,
    ) -> None:
        self.path = Path(path) if path is not None else state_path(*DEFAULT_PATH)
        self.history_depth = max(1, history_depth)

    def remember(
        self,
        trade_date: date,
        level: str,
        outlooks: list[SectorOutlook],
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        leader_count: int = DEFAULT_LEADER_COUNT,
    ) -> RotationSummary:
        self.record(trade_date, level, outlooks)
        return self.summarize(trade_date, level, lookback_days, leader_count)

    def record(self, trade_date: date, level: str, outlooks: list[SectorOutlook]) -> None:
        payload = self._load_payload()
        record = {
            "trade_date": str(trade_date),
            "level": level,
            "leaders": [
                self._serialize(index, item)
                for index, item in enumerate(
                    [row for row in outlooks if getattr(row, "trusted", True)][: self.history_depth],
                    start=1,
                )
            ],
        }
        records = [
            item
            for item in payload["records"]
            if not (
                item.get("trade_date") == record["trade_date"]
                and item.get("level") == record["level"]
            )
        ]
        records.append(record)
        records.sort(key=lambda item: (str(item.get("level") or ""), str(item.get("trade_date") or "")))
        payload["records"] = records
        write_json(self.path, payload)

    def summarize(
        self,
        trade_date: date,
        level: str,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        leader_count: int = DEFAULT_LEADER_COUNT,
    ) -> RotationSummary:
        leader_count = max(1, leader_count)
        records = self._records(level, trade_date)
        if not records:
            return RotationSummary(
                level=level,
                trade_date=str(trade_date),
                lookback_days=max(1, lookback_days),
                leader_count=leader_count,
                regime="样本不足",
                available_days=0,
                observations=("板块记忆暂无历史样本，本次只能按当天截面判断。",),
            )

        current = records[-1]
        current_date = date.fromisoformat(current["trade_date"])
        window_start = current_date - timedelta(days=max(1, lookback_days) - 1)
        window = [item for item in records if date.fromisoformat(item["trade_date"]) >= window_start]
        ranked = [self._top_names(item, leader_count) for item in window]
        today_names = ranked[-1]
        if not today_names:
            return RotationSummary(
                level=level,
                trade_date=current["trade_date"],
                lookback_days=max(1, lookback_days),
                leader_count=leader_count,
                regime="样本不足",
                available_days=len(window),
                observations=("板块覆盖率不足，暂无可用的轮动记忆。",),
            )

        prior = ranked[:-1]
        if not prior:
            return RotationSummary(
                level=level,
                trade_date=current["trade_date"],
                lookback_days=max(1, lookback_days),
                leader_count=leader_count,
                regime="样本不足",
                available_days=1,
                observations=("板块记忆样本不足，目前只有当天截面。",),
            )

        yesterday_names = prior[-1]
        today_set = set(today_names)
        yesterday_set = set(yesterday_names)
        overlap = len(today_set & yesterday_set) / max(len(today_names), len(yesterday_names), 1)

        hit_counts = self._counts(ranked)
        prior_counts = self._counts(prior)
        hit_threshold = max(2, math.ceil(len(window) / 2))

        persistent = tuple(name for name in today_names if hit_counts.get(name, 0) >= hit_threshold)
        new_entries = tuple(name for name in today_names if name not in prior_counts)
        faded = tuple(name for name in yesterday_names if name not in today_set)

        if overlap >= 0.6 and len(new_entries) <= 1:
            regime = "主线延续"
        elif overlap <= 0.25 or len(new_entries) >= max(2, math.ceil(len(today_names) / 2)):
            regime = "轮动加快"
        else:
            regime = "温和轮动"

        observations = [
            f"相对昨日前{leader_count}重合 {overlap:.0%}，当前属于{regime}。",
        ]
        if persistent:
            observations.append(
                f"近{len(window)}个交易样本里反复进入前{leader_count}的方向："
                + "、".join(persistent)
            )
        else:
            observations.append(f"近{len(window)}个交易样本里没有稳定占优的主线。")
        if new_entries:
            observations.append(f"今天新切入前{leader_count}：{'、'.join(new_entries)}")
        if faded:
            observations.append(f"昨天靠前、今天退到队外：{'、'.join(faded)}")

        return RotationSummary(
            level=level,
            trade_date=current["trade_date"],
            lookback_days=max(1, lookback_days),
            leader_count=leader_count,
            regime=regime,
            available_days=len(window),
            overlap_with_yesterday=round(overlap, 4),
            persistent=persistent,
            new_entries=new_entries,
            faded=faded,
            observations=tuple(observations),
        )

    def _load_payload(self) -> dict[str, Any]:
        payload = read_json(self.path, default={}) or {}
        if payload.get("schema") != HISTORY_SCHEMA or not isinstance(payload.get("records"), list):
            return {"schema": HISTORY_SCHEMA, "records": []}
        return payload

    def _records(self, level: str, trade_date: date) -> list[dict[str, Any]]:
        rows = []
        for item in self._load_payload()["records"]:
            if item.get("level") != level:
                continue
            day = item.get("trade_date")
            if not isinstance(day, str):
                continue
            try:
                parsed = date.fromisoformat(day)
            except ValueError:
                continue
            if parsed <= trade_date:
                rows.append(item)
        rows.sort(key=lambda item: str(item.get("trade_date") or ""))
        return rows

    def _serialize(self, rank: int, item: SectorOutlook) -> dict[str, Any]:
        metrics = item.metrics
        return {
            "rank": rank,
            "name": item.name,
            "score": item.score,
            "ret_1d": metrics.ret_1d,
            "ret_5d": metrics.ret_5d,
            "breadth": metrics.breadth,
            "volume_ratio": metrics.volume_ratio,
        }

    @staticmethod
    def _top_names(record: dict[str, Any], leader_count: int) -> list[str]:
        names: list[str] = []
        for item in record.get("leaders", [])[:leader_count]:
            name = item.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        return names

    @staticmethod
    def _counts(days: list[list[str]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for names in days:
            for name in names:
                counts[name] = counts.get(name, 0) + 1
        return counts
