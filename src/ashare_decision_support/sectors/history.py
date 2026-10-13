"""按日期与分类保存板块排名，描述已发生的主线延续与轮动。

写入复用状态锁；坏记录按条隔离。回看窗口为最近一次记录向前的自然日范围，
available_days 表示其中实际记录的日期数，不补猜缺失的交易日或排名。
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from itertools import islice
from pathlib import Path
from typing import Any

from ashare_decision_support.jsoncache import read_stamped
from ashare_decision_support.sectors.base import LEVEL_INDUSTRY, SectorOutlook
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import file_lock, write_json

HISTORY_SCHEMA = "sector-rotation-v1"
# 写入方 morning 与读取方 monthly 共用分类名称。
DEFAULT_LEVEL = LEVEL_INDUSTRY
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
        record = {
            "trade_date": str(trade_date),
            "level": level,
            "leaders": [
                self._serialize(index, item)
                for index, item in enumerate(
                    islice((row for row in outlooks if row.trusted), self.history_depth),
                    start=1,
                )
            ],
        }
        with file_lock(self.path):
            payload = self._load_payload()
            records = [
                item
                for item in payload["records"]
                if (item["trade_date"], item["level"]) != (record["trade_date"], record["level"])
            ]
            records.append(record)
            records.sort(key=lambda item: (item["level"], item["trade_date"]))
            payload["records"] = records
            write_json(self.path, payload)

    def summarize(
        self,
        trade_date: date,
        level: str,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        leader_count: int = DEFAULT_LEADER_COUNT,
    ) -> RotationSummary:
        lookback_days = max(1, lookback_days)
        leader_count = max(1, leader_count)
        records = self._records(level, trade_date)
        current_date = date.fromisoformat(records[-1]["trade_date"]) if records else trade_date
        window_start = str(date.fromordinal(max(1, current_date.toordinal() - lookback_days + 1)))
        window = [item for item in records if item["trade_date"] >= window_start]
        summary = RotationSummary(
            level=level,
            trade_date=str(current_date),
            lookback_days=lookback_days,
            leader_count=leader_count,
            regime="样本不足",
            available_days=len(window),
        )
        if not records:
            return replace(
                summary,
                observations=("板块记忆暂无历史样本，本次只能按当天截面判断。",),
            )

        ranked = [self._top_names(item, leader_count) for item in window]
        today_names = ranked[-1]
        if not all(ranked):
            return replace(
                summary,
                observations=("板块覆盖率不足，暂无可用的轮动记忆。",),
            )

        prior = ranked[:-1]
        if not prior:
            return replace(
                summary,
                observations=("板块记忆样本不足，目前只有当天截面。",),
            )

        yesterday_names = prior[-1]
        today_set = set(today_names)
        yesterday_set = set(yesterday_names)
        overlap = len(today_set & yesterday_set) / max(len(today_names), len(yesterday_names), 1)

        prior_counts = Counter(name for names in prior for name in names)
        hit_threshold = max(2, math.ceil(len(window) / 2))

        persistent = tuple(name for name in today_names if prior_counts[name] + 1 >= hit_threshold)
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

        return replace(
            summary,
            regime=regime,
            overlap_with_yesterday=round(overlap, 4),
            persistent=persistent,
            new_entries=new_entries,
            faded=faded,
            observations=tuple(observations),
        )

    def _load_payload(self) -> dict[str, Any]:
        payload = read_stamped(self.path, HISTORY_SCHEMA) or {}
        raw = payload.get("records")
        records: dict[tuple[str, str], dict[str, Any]] = {}
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            day, level = item.get("trade_date"), item.get("level")
            if not isinstance(day, str) or not isinstance(level, str) or not level.strip():
                continue
            try:
                stamp = date.fromisoformat(day).isoformat()
            except ValueError:
                continue
            level = level.strip()
            records[(level, stamp)] = {**item, "trade_date": stamp, "level": level}
        return {**payload, "schema": HISTORY_SCHEMA, "records": list(records.values())}

    def _records(self, level: str, trade_date: date) -> list[dict[str, Any]]:
        return sorted(
            (
                item
                for item in self._load_payload()["records"]
                if item["level"] == level and item["trade_date"] <= str(trade_date)
            ),
            key=lambda item: item["trade_date"],
        )

    @staticmethod
    def _serialize(rank: int, item: SectorOutlook) -> dict[str, Any]:
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
        leaders = record.get("leaders")
        if not isinstance(leaders, list):
            return []
        names = (item.get("name") for item in leaders if isinstance(item, dict))
        valid = (name.strip() for name in names if isinstance(name, str) and name.strip())
        return list(dict.fromkeys(valid))[:leader_count]
