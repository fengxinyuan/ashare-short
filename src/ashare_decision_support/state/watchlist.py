"""The two lists the system monitors.

They are deliberately separate. The watchlist is yours — you put a name on it
and it stays until you take it off. The candidate pool is the machine's,
regenerated every morning and expired the moment the date rolls over.

Merging them would be more convenient and would cost you the ability to answer
"is this something I decided to watch, or something the scanner pushed at me
this morning?" — which is exactly the question you need answered when an alert
fires at 10:30.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import read_json, write_json
from ashare_decision_support.symbols import normalize_symbol

WATCHLIST_FILE = "watchlist.json"
CANDIDATES_FILE = "candidates.json"

SOURCE_MANUAL = "manual"
SOURCE_SCAN = "sector-scan"


@dataclass
class WatchEntry:
    symbol: str
    name: str | None = None
    sector: str | None = None
    source: str = SOURCE_MANUAL
    note: str | None = None
    added_on: str | None = None
    score: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "WatchEntry":
        return cls(
            symbol=normalize_symbol(payload.get("symbol", "")),
            name=payload.get("name"),
            sector=payload.get("sector"),
            source=payload.get("source") or SOURCE_MANUAL,
            note=payload.get("note"),
            added_on=payload.get("added_on"),
            score=payload.get("score"),
        )


@dataclass
class _EntryList:
    """Shared behaviour for both lists: ordered, de-duplicated by symbol."""

    path: Path
    entries: list[WatchEntry] = field(default_factory=list)

    def symbols(self) -> list[str]:
        return [entry.symbol for entry in self.entries]

    def get(self, symbol: str) -> WatchEntry | None:
        target = normalize_symbol(symbol)
        return next((entry for entry in self.entries if entry.symbol == target), None)

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, symbol: object) -> bool:
        return self.get(str(symbol)) is not None


class Watchlist(_EntryList):
    """Human-maintained, persistent. Nothing removes an entry except you."""

    def __init__(self, path: Path | None = None) -> None:
        resolved = Path(path) if path is not None else state_path(WATCHLIST_FILE)
        rows = read_json(resolved, default=[]) or []
        super().__init__(
            path=resolved,
            entries=[WatchEntry.from_dict(row) for row in rows if isinstance(row, dict)],
        )

    def add(
        self,
        symbol: str,
        name: str | None = None,
        sector: str | None = None,
        note: str | None = None,
        source: str = SOURCE_MANUAL,
        score: float | None = None,
        today: date | None = None,
    ) -> tuple[WatchEntry, bool]:
        """Add or update. Returns (entry, created)."""
        code = normalize_symbol(symbol)
        if not code:
            raise ValueError(f"无法识别的标的代码：{symbol!r}")

        existing = self.get(code)
        if existing is not None:
            # Update in place, but never overwrite a field with None — a later
            # `add` without a note must not erase the note you typed earlier.
            for attribute, value in (("name", name), ("sector", sector), ("note", note), ("score", score)):
                if value is not None:
                    setattr(existing, attribute, value)
            self.save()
            return existing, False

        entry = WatchEntry(
            symbol=code,
            name=name,
            sector=sector,
            source=source,
            note=note,
            score=score,
            added_on=str(today or date.today()),
        )
        self.entries.append(entry)
        self.save()
        return entry, True

    def remove(self, symbol: str) -> bool:
        entry = self.get(symbol)
        if entry is None:
            return False
        self.entries.remove(entry)
        self.save()
        return True

    def clear(self) -> int:
        count = len(self.entries)
        self.entries = []
        self.save()
        return count

    def save(self) -> None:
        write_json(self.path, [entry.to_dict() for entry in self.entries])


class CandidatePool(_EntryList):
    """Machine-generated, dated. Reads as empty once the trade date rolls over."""

    def __init__(self, path: Path | None = None, today: date | None = None) -> None:
        resolved = Path(path) if path is not None else state_path(CANDIDATES_FILE)
        payload = read_json(resolved, default={}) or {}
        self.trade_date: str | None = payload.get("trade_date")
        rows = payload.get("entries") or []
        entries = [WatchEntry.from_dict(row) for row in rows if isinstance(row, dict)]

        # Stale pools are not silently served. Yesterday's candidates carry
        # yesterday's levels, and an automated loop cannot tell the difference
        # unless the store refuses to hand them over.
        self.stale = bool(self.trade_date) and self.trade_date != str(today or date.today())
        super().__init__(path=resolved, entries=[] if self.stale else entries)

    def replace(self, entries: list[WatchEntry], trade_date: date) -> None:
        self.entries = list(entries)
        self.trade_date = str(trade_date)
        self.stale = False
        write_json(
            self.path,
            {"trade_date": self.trade_date, "entries": [entry.to_dict() for entry in self.entries]},
        )
