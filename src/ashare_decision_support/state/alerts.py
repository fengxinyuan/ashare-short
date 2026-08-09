"""Per-session alert bookkeeping: what has already fired, and what the last
observed state was.

The monitor polls every couple of minutes. Over a four-hour session that is
~145 passes, so anything that alerts on a *state* ("price is above VWAP")
fires 145 times. This store is what lets the monitor alert on *transitions*
instead, and enforce a cooldown when a symbol oscillates across a level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import read_json, write_json
from ashare_decision_support.symbols import normalize_symbol


@dataclass
class AlertState:
    """Alert history and per-symbol observed state for one trading day."""

    session: date
    path: Path
    fired: dict[str, dict] = field(default_factory=dict)
    observed: dict[str, dict] = field(default_factory=dict)
    pinned: dict[str, dict] = field(default_factory=dict)
    peaks: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls, session: date, path: Path | None = None) -> AlertState:
        resolved = Path(path) if path is not None else state_path("alerts", f"{session}.json")
        payload = read_json(resolved, default={}) or {}
        return cls(
            session=session,
            path=resolved,
            fired=payload.get("fired") or {},
            observed=payload.get("observed") or {},
            pinned=payload.get("pinned") or {},
            # 老的 alerts/*.json 没有这个键。缺省空字典即「今天还没记过峰值」，
            # 读旧文件不会炸。
            peaks=payload.get("peaks") or {},
        )

    # ---- what has already fired -----------------------------------------

    def last_fired(self, symbol: str, trigger: str) -> datetime | None:
        record = self.fired.get(_key(symbol, trigger))
        if not record:
            return None
        try:
            return datetime.fromisoformat(record["at"])
        except (KeyError, TypeError, ValueError):
            return None

    def fire_count(self, symbol: str, trigger: str) -> int:
        return int(self.fired.get(_key(symbol, trigger), {}).get("count", 0))

    def mark_fired(self, symbol: str, trigger: str, now: datetime) -> None:
        key = _key(symbol, trigger)
        record = self.fired.get(key) or {"count": 0}
        self.fired[key] = {"at": now.isoformat(timespec="seconds"), "count": int(record["count"]) + 1}

    # ---- last observed state, for transition detection -------------------

    def state_of(self, symbol: str) -> dict:
        return dict(self.observed.get(normalize_symbol(symbol)) or {})

    def set_state(self, symbol: str, state: dict) -> None:
        self.observed[normalize_symbol(symbol)] = dict(state)

    # ---- pinned levels ---------------------------------------------------

    def pinned_of(self, symbol: str) -> dict | None:
        """Levels frozen when the symbol was armed, or None if not yet armed.

        A stop recomputed from the live session low sits below the live low by
        construction, so it can never be broken and `stop_break` never fires.
        Pinning is what makes the level mean what it says.
        """
        return self.pinned.get(normalize_symbol(symbol))

    def pin(self, symbol: str, stop: float | None, targets: list[float], at: datetime) -> None:
        self.pinned[normalize_symbol(symbol)] = {
            "stop": stop,
            "targets": list(targets),
            "at": at.isoformat(timespec="seconds"),
        }

    # ---- 持仓期最高价 -----------------------------------------------------

    def peak_of(self, symbol: str) -> float | None:
        """这笔持仓到目前为止摸到过的最高价，移动止损的分母。"""
        value = self.peaks.get(normalize_symbol(symbol))
        return float(value) if isinstance(value, (int, float)) and value > 0 else None

    def observe_peak(self, symbol: str, price: float | None) -> float | None:
        """记下新高并返回当前峰值。只涨不跌——峰值回落就不是峰值了。

        **必须持久化。** 监控进程盘中重启是常事（改配置、断网重连），而峰值只
        存在内存里的话，重启会把上午冲出来的高点抹掉，于是「已回吐一半浮盈」
        重新读成「刚建仓没多久」。移动止损被静默关掉，恰好在最需要它的那天。
        """
        code = normalize_symbol(symbol)
        current = self.peak_of(code)
        if price is not None and price > 0 and (current is None or price > current):
            self.peaks[code] = float(price)
            return float(price)
        return current

    def save(self) -> None:
        write_json(
            self.path,
            {
                "session": str(self.session),
                "fired": self.fired,
                "observed": self.observed,
                "pinned": self.pinned,
                "peaks": self.peaks,
            },
        )

    def fired_summary(self) -> list[dict]:
        """Everything that fired today, newest first — what `/alerts` reads."""
        rows = [
            {"symbol": key.split("|", 1)[0], "trigger": key.split("|", 1)[1], **record}
            for key, record in self.fired.items()
            if "|" in key
        ]
        return sorted(rows, key=lambda row: str(row.get("at", "")), reverse=True)


def _key(symbol: str, trigger: str) -> str:
    return f"{normalize_symbol(symbol)}|{trigger}"
