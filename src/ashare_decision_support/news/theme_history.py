"""Persist and evaluate theme-radar observations across trading days.

This is deliberately an observational ledger, not a model trainer.  Every
theme signal is retained with the market state seen at the time; subsequent
sector returns produce 1/5/20-session outcomes and make later calibration
possible without retrospective reconstruction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

from ashare_decision_support.news.theme_radar import ThemeSignal
from ashare_decision_support.sectors.base import SectorMetrics
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import read_json, write_json

SCHEMA = "theme-outcome-v1"
HORIZONS = (1, 5, 20)


@dataclass
class ThemeOutcomeStore:
    path: Path | None = None

    def __post_init__(self) -> None:
        if self.path is None:
            self.path = state_path("market", "theme_outcomes.json")

    def record(
        self, trade_date: date, signals: list[ThemeSignal], metrics: dict[str, SectorMetrics]
    ) -> dict[str, Any]:
        payload = self._load()
        daily = self._daily(metrics)
        payload["daily"] = [
            item for item in payload["daily"] if item.get("trade_date") != str(trade_date)
        ]
        payload["daily"].append({"trade_date": str(trade_date), **daily})

        keys = {(str(trade_date), item.name) for item in signals}
        payload["signals"] = [
            item for item in payload["signals"]
            if (item.get("trade_date"), item.get("name")) not in keys
        ]
        payload["signals"].extend({"trade_date": str(trade_date), **item.to_dict()} for item in signals)
        payload["daily"].sort(key=lambda item: str(item.get("trade_date") or ""))
        payload["signals"].sort(key=lambda item: (str(item.get("trade_date") or ""), str(item.get("name") or "")))
        self._assess(payload)
        write_json(self.path, payload)
        return self.summary(payload)

    def summary(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or self._load()
        completed = [item for item in data["signals"] if "outcomes" in item]
        by_stage: dict[str, list[float]] = {}
        for item in completed:
            outcome = (item.get("outcomes") or {}).get("5")
            excess = outcome.get("excess_return") if isinstance(outcome, dict) else None
            if isinstance(excess, (int, float)):
                by_stage.setdefault(str(item.get("stage") or "未知"), []).append(excess)
        return {
            "signals_recorded": len(data["signals"]),
            "signals_with_5d_outcome": sum(len(values) for values in by_stage.values()),
            "trusted": sum(len(values) for values in by_stage.values()) >= 30,
            "by_stage": [
                {
                    "stage": stage,
                    "samples": len(values),
                    "win_rate": round(sum(value > 0 for value in values) / len(values), 4),
                    "mean_excess_return": round(sum(values) / len(values), 4),
                }
                for stage, values in sorted(by_stage.items())
            ],
        }

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        payload = read_json(self.path, default={}) or {}
        if payload.get("schema") != SCHEMA:
            return {"schema": SCHEMA, "daily": [], "signals": []}
        daily = payload.get("daily")
        signals = payload.get("signals")
        if not isinstance(daily, list) or not isinstance(signals, list):
            return {"schema": SCHEMA, "daily": [], "signals": []}
        return {"schema": SCHEMA, "daily": daily, "signals": signals}

    @staticmethod
    def _daily(metrics: dict[str, SectorMetrics]) -> dict[str, Any]:
        returns = {
            name: value.ret_1d for name, value in metrics.items()
            if value.ret_1d is not None and value.coverage_ratio > 0
        }
        return {"returns": returns, "market_return": median(returns.values()) if returns else None}

    @staticmethod
    def _assess(payload: dict[str, list[dict[str, Any]]]) -> None:
        daily = payload["daily"]
        by_date = {str(item.get("trade_date")): index for index, item in enumerate(daily)}
        for signal in payload["signals"]:
            start = by_date.get(str(signal.get("trade_date") or ""))
            if start is None:
                continue
            outcomes: dict[str, dict[str, float | int]] = {}
            sectors = [str(item) for item in signal.get("sectors") or []]
            for horizon in HORIZONS:
                window = daily[start + 1 : start + 1 + horizon]
                if len(window) < horizon:
                    continue
                sector_steps: list[float] = []
                market_steps: list[float] = []
                for day in window:
                    values = [day.get("returns", {}).get(name) for name in sectors]
                    usable = [value for value in values if isinstance(value, (int, float))]
                    market_return = day.get("market_return")
                    if not usable or not isinstance(market_return, (int, float)):
                        break
                    sector_steps.append(median(usable))
                    market_steps.append(market_return)
                if len(sector_steps) != horizon:
                    continue
                sector_return = _compound(sector_steps)
                market_return = _compound(market_steps)
                outcomes[str(horizon)] = {
                    "sessions": horizon,
                    "return": round(sector_return, 6),
                    "excess_return": round(sector_return - market_return, 6),
                }
            if outcomes:
                signal["outcomes"] = outcomes


def _compound(returns: list[float]) -> float:
    value = 1.0
    for item in returns:
        value *= 1.0 + item
    return value - 1.0
