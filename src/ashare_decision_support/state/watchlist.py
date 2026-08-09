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
    """自选股的一条。

    **持仓状态决定这条记录该被怎么监控。** 同一只票，没持仓时要盯的是买点
    （进入区间 → 止跌确认），已持仓时要盯的是卖点（止损、目标、结构破坏、
    时间止损）——两套判断的输入、阈值和紧急程度都不同。没有 `cost_price`
    就给不出「你已经浮盈 4%，跌破 X 该走」这类建议，只能报绝对价位。

    系统只出建议，不下单、不接管账户，所以这些字段全部由人手工填写维护。
    """

    symbol: str
    name: str | None = None
    sector: str | None = None
    industries: list[str] = field(default_factory=list)
    industry: str | None = None
    industry_leaf: str | None = None
    industry_path: str | None = None
    concepts: list[str] = field(default_factory=list)
    source: str = SOURCE_MANUAL
    note: str | None = None
    added_on: str | None = None
    score: float | None = None
    # ---- 持仓状态（人工维护）------------------------------------------------
    holding: bool = False
    cost_price: float | None = None
    shares: int | None = None
    opened_on: str | None = None      # 建仓日，时间止损从这天算起
    plan_stop: float | None = None    # 你自己定的止损，优先于系统算出来的
    plan_target: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> WatchEntry:
        return cls(
            symbol=normalize_symbol(payload.get("symbol", "")),
            name=payload.get("name"),
            sector=payload.get("sector"),
            industries=[str(item) for item in (payload.get("industries") or []) if str(item)],
            industry=payload.get("industry"),
            industry_leaf=payload.get("industry_leaf"),
            industry_path=payload.get("industry_path"),
            concepts=[str(item) for item in (payload.get("concepts") or []) if str(item)],
            source=payload.get("source") or SOURCE_MANUAL,
            note=payload.get("note"),
            added_on=payload.get("added_on"),
            score=payload.get("score"),
            # 老文件没有这些键，缺省即「未持仓」——读旧 watchlist.json 不会炸。
            holding=bool(payload.get("holding")),
            cost_price=payload.get("cost_price"),
            shares=payload.get("shares"),
            opened_on=payload.get("opened_on"),
            plan_stop=payload.get("plan_stop"),
            plan_target=payload.get("plan_target"),
        )

    @property
    def held_days(self) -> int | None:
        """持仓自然日数。时间止损用它，不精确到交易日——差一两天不影响判断。"""
        if not self.holding or not self.opened_on:
            return None
        try:
            return (date.today() - date.fromisoformat(str(self.opened_on))).days
        except (TypeError, ValueError):
            return None

    def unrealised_pct(self, price: float | None) -> float | None:
        """浮动盈亏。没有成本价就返回 None，不猜。"""
        if not self.holding or not self.cost_price or not price:
            return None
        return round(float(price) / float(self.cost_price) - 1.0, 6)


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
        industries: list[str] | None = None,
        industry: str | None = None,
        industry_leaf: str | None = None,
        industry_path: str | None = None,
        concepts: list[str] | None = None,
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
            for attribute, value in (
                ("name", name),
                ("sector", sector),
                ("industry", industry),
                ("industry_leaf", industry_leaf),
                ("industry_path", industry_path),
                ("note", note),
                ("score", score),
            ):
                if value is not None:
                    setattr(existing, attribute, value)
            if industries is not None:
                existing.industries = list(industries)
            if concepts is not None:
                existing.concepts = list(concepts)
            self.save()
            return existing, False

        entry = WatchEntry(
            symbol=code,
            name=name,
            sector=sector,
            industries=list(industries or []),
            industry=industry,
            industry_leaf=industry_leaf,
            industry_path=industry_path,
            concepts=list(concepts or []),
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

    def set_position(
        self,
        symbol: str,
        cost_price: float,
        shares: int | None = None,
        opened_on: date | None = None,
        plan_stop: float | None = None,
        plan_target: float | None = None,
        today: date | None = None,
    ) -> WatchEntry:
        """标记为已持仓。标的不在自选股里就先加进去——你买了它，它当然该被盯着。"""
        entry = self.get(symbol)
        if entry is None:
            entry, _ = self.add(symbol, today=today)
        entry.holding = True
        entry.cost_price = float(cost_price)
        entry.shares = shares if shares is None else int(shares)
        entry.opened_on = str(opened_on or today or date.today())
        if plan_stop is not None:
            entry.plan_stop = float(plan_stop)
        if plan_target is not None:
            entry.plan_target = float(plan_target)
        self.save()
        return entry

    def close_position(self, symbol: str) -> bool:
        """清掉持仓状态，但**保留在自选股里**——卖掉不等于不再关注。"""
        entry = self.get(symbol)
        if entry is None or not entry.holding:
            return False
        entry.holding = False
        entry.cost_price = None
        entry.shares = None
        entry.opened_on = None
        self.save()
        return True

    def holdings(self) -> list[WatchEntry]:
        return [entry for entry in self.entries if entry.holding]

    def unheld(self) -> list[WatchEntry]:
        return [entry for entry in self.entries if not entry.holding]

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
