"""台账读写。按月分文件的 JSONL，只追加。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from ashare_decision_support.journal.records import (
    DEFAULT_STRATEGY_VERSION,
    KIND_OUTCOME,
    KIND_RECOMMENDATION,
    VERIFIABLE_KINDS,
    Outcome,
    Recommendation,
    record_key,
)
from ashare_decision_support.numeric import as_int
from ashare_decision_support.schedule.timezone import beijing_now, beijing_today
from ashare_decision_support.signals.series import parse_bar_date
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import append_jsonl, file_lock, read_jsonl
from ashare_decision_support.symbols import normalize_symbol


class Journal:
    """按月分片的只追加台账。

    只追加而不改写：一条建议发出后就是历史，事后修改它等于让统计说谎。兑现结果
    作为新记录写入。新判断按股票、日期、来源与 decision_id 关联；旧台账保持三元键。
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else state_path("journal")
        self.root.mkdir(parents=True, exist_ok=True)

    def _file(self, day: date) -> Path:
        return self.root / f"{day:%Y-%m}.jsonl"

    # 写

    def record(self, item: Recommendation | Outcome) -> None:
        self.record_many([item])

    def record_many(self, items: list[Recommendation | Outcome]) -> int:
        """在月文件锁内追加；相同判断重跑去重，变化后保留新版本及前驱。"""
        grouped: dict[Path, list[dict]] = defaultdict(list)
        for item in items:
            day = parse_bar_date(item.trade_date) or beijing_today()
            grouped[self._file(day)].append(item.to_dict())
        written = 0
        for path, batch in grouped.items():
            with file_lock(path):
                existing = read_jsonl(path)
                known = {(row.get("kind"), record_key(row)) for row in existing}
                decision_contents = {
                    record_key(row): _signature(row)
                    for row in existing
                    if row.get("kind") in VERIFIABLE_KINDS and row.get("decision_id")
                }
                latest = {
                    _logical_key(row): row
                    for row in existing
                    if row.get("kind") in VERIFIABLE_KINDS
                }
                version_latest = {
                    (*_logical_key(row), row.get("strategy_version")): row
                    for row in existing
                    if row.get("kind") in VERIFIABLE_KINDS
                }
                for row in batch:
                    if row.get("kind") in VERIFIABLE_KINDS:
                        row["symbol"] = normalize_symbol(row.get("symbol") or "")
                        row["trade_date"] = str(
                            parse_bar_date(row.get("trade_date")) or row.get("trade_date")
                        )
                        row["strategy_version"] = (
                            row.get("strategy_version") or DEFAULT_STRATEGY_VERSION
                        )
                        previous = latest.get(_logical_key(row))
                        if not row.get("decision_id"):
                            if previous and _signature(previous) == _signature(row):
                                continue
                            row["revision"] = (
                                max(1, as_int(previous.get("revision"))) + 1 if previous else 1
                            )
                            same_version = version_latest.get(
                                (*_logical_key(row), row["strategy_version"])
                            )
                            row["strategy_revision"] = (
                                max(1, as_int(same_version.get("strategy_revision"))) + 1
                                if same_version
                                else 1
                            )
                            row["supersedes"] = record_key(previous) if previous else None
                            row["recorded_at"] = row.get("recorded_at") or beijing_now().isoformat()
                            identity = f"{row['supersedes']}|{_signature(row)}"
                            row["decision_id"] = hashlib.sha256(identity.encode()).hexdigest()[:24]
                        previous_content = decision_contents.get(record_key(row))
                        if previous_content is not None and previous_content != _signature(row):
                            raise ValueError("同一判断编号不能对应不同内容；请追加新判断版本")
                    key = (row.get("kind"), record_key(row))
                    if key in known:
                        continue
                    append_jsonl(path, row)
                    written += 1
                    known.add(key)
                    if row.get("kind") in VERIFIABLE_KINDS:
                        latest[_logical_key(row)] = row
                        version_latest[(*_logical_key(row), row.get("strategy_version"))] = row
                        decision_contents[record_key(row)] = _signature(row)
        return written

    # 读

    def rows(self, since: date | None = None, until: date | None = None) -> list[dict]:
        """区间内的全部记录。跨月自动读多个文件。"""
        end = until or beijing_today()
        start = since or (end - timedelta(days=365))
        rows: list[dict] = []
        for path in sorted(self.root.glob("*.jsonl")):
            for row in read_jsonl(path):
                day = parse_bar_date(row.get("trade_date"))
                if day is None or day < start or day > end:
                    continue
                rows.append(row)
        return rows

    def recommendations(self, since: date | None = None, until: date | None = None) -> list[dict]:
        return [row for row in self.rows(since, until) if row.get("kind") == KIND_RECOMMENDATION]

    def outcomes(self, since: date | None = None, until: date | None = None) -> list[dict]:
        return [row for row in self.rows(since, until) if row.get("kind") == KIND_OUTCOME]

    def pending(self, today: date, min_age_days: int = 1, lookback_days: int = 60) -> list[dict]:
        """待核对的记录：已过至少一个交易日，且还没有兑现记录。

        **对照组也在内。** 只核对推荐、不核对对照，等于把对照组记了个寂寞——
        没有对照的兑现数据就算不出「入选 vs 落选」的收益差。
        """
        rows = self.rows(since=today - timedelta(days=lookback_days), until=today)
        resolved = {record_key(row) for row in rows if row.get("kind") == KIND_OUTCOME}
        pending: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            if row.get("kind") not in VERIFIABLE_KINDS:
                continue
            key = record_key(row)
            day = parse_bar_date(row.get("trade_date"))
            if key in resolved or key in seen or day is None:
                continue
            if (today - day).days < min_age_days:
                continue
            seen.add(key)
            pending.append(row)
        return pending

    def history_for(self, symbol: str, limit: int = 20) -> list[dict]:
        """某只票的建议与兑现，最新在前——`chat` 的 `/journal` 读这个。"""
        target = normalize_symbol(symbol)
        rows = [row for row in self.rows() if normalize_symbol(row.get("symbol", "")) == target]
        rows.sort(
            key=lambda row: (
                str(row.get("trade_date")),
                str(row.get("recorded_at") or ""),
                row.get("kind") or "",
            ),
            reverse=True,
        )
        return rows[:limit]


def _logical_key(row: dict) -> tuple[str, str, str]:
    return (
        normalize_symbol(row.get("symbol") or ""),
        str(parse_bar_date(row.get("trade_date")) or row.get("trade_date")),
        str(row.get("source") or ""),
    )


def _signature(row: dict) -> str:
    ignored = {
        "decision_id",
        "recorded_at",
        "revision",
        "strategy_revision",
        "supersedes",
        "facts_hash",
    }
    content = {key: value for key, value in row.items() if key not in ignored}
    return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)
