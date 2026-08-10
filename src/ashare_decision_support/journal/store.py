"""台账读写。按月分文件的 JSONL，只追加。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.journal.records import (
    KIND_OUTCOME,
    KIND_RECOMMENDATION,
    VERIFIABLE_KINDS,
    Outcome,
    Recommendation,
)
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import append_jsonl, read_jsonl


class Journal:
    """按月分片的只追加台账。

    只追加而不改写：一条建议发出后就是历史，事后修改它等于让统计说谎。兑现结果
    作为**新记录**写入，靠 (symbol, trade_date, source) 关联。
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else state_path("journal")
        self.root.mkdir(parents=True, exist_ok=True)

    def _file(self, day: date) -> Path:
        return self.root / f"{day:%Y-%m}.jsonl"

    # ---- 写 ---------------------------------------------------------------

    def record(self, item: Recommendation | Outcome) -> None:
        day = _parse_date(item.trade_date) or beijing_today()
        append_jsonl(self._file(day), item.to_dict())

    def record_many(self, items: list[Recommendation | Outcome]) -> int:
        for item in items:
            self.record(item)
        return len(items)

    # ---- 读 ---------------------------------------------------------------

    def rows(self, since: date | None = None, until: date | None = None) -> list[dict]:
        """区间内的全部记录。跨月自动读多个文件。"""
        end = until or beijing_today()
        start = since or (end - timedelta(days=365))
        rows: list[dict] = []
        for path in sorted(self.root.glob("*.jsonl")):
            for row in read_jsonl(path):
                day = _parse_date(row.get("trade_date"))
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
        resolved = {
            f"{row.get('symbol')}|{row.get('trade_date')}|{row.get('source')}"
            for row in rows
            if row.get("kind") == KIND_OUTCOME
        }
        pending: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            if row.get("kind") not in VERIFIABLE_KINDS:
                continue
            key = f"{row.get('symbol')}|{row.get('trade_date')}|{row.get('source')}"
            day = _parse_date(row.get("trade_date"))
            if key in resolved or key in seen or day is None:
                continue
            if (today - day).days < min_age_days:
                continue
            seen.add(key)
            pending.append(row)
        return pending

    def history_for(self, symbol: str, limit: int = 20) -> list[dict]:
        """某只票的建议与兑现，最新在前——`chat` 的 `/journal` 读这个。"""
        from ashare_decision_support.symbols import normalize_symbol

        target = normalize_symbol(symbol)
        rows = [row for row in self.rows() if normalize_symbol(row.get("symbol", "")) == target]
        rows.sort(key=lambda row: (str(row.get("trade_date")), row.get("kind") or ""), reverse=True)
        return rows[:limit]


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
