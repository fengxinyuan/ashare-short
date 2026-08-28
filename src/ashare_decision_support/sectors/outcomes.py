"""Record sector rankings and evaluate their forward relative returns.

The ranking score is not a probability.  This ledger is the bridge from an
unvalidated same-day ranking to evidence about whether top-ranked sectors have
actually outperformed over the following 1/3/5 sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

from ashare_decision_support.sectors.base import SectorMetrics, SectorOutlook
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import read_json, write_json

SCHEMA = "sector-forecast-outcomes-v2"
HORIZONS = (1, 3, 5)
DEFAULT_TOP = 10
MIN_CALIBRATION_SAMPLES = 30


@dataclass
class SectorForecastOutcomeStore:
    path: Path | None = None

    def __post_init__(self) -> None:
        if self.path is None:
            self.path = state_path("market", "sector_forecast_outcomes.json")

    def record(
        self,
        trade_date: date,
        outlooks: list[SectorOutlook],
        metrics: dict[str, SectorMetrics],
        *,
        data_date: date | None = None,
        top: int = DEFAULT_TOP,
    ) -> dict[str, Any]:
        payload = self._load()
        stamp = str(trade_date)
        information_stamp = str(data_date or trade_date)
        returns = {
            name: item.ret_1d
            for name, item in metrics.items()
            if item.ret_1d is not None and item.coverage_ratio > 0
        }
        payload["daily"] = [
            row for row in payload["daily"] if row.get("trade_date") != information_stamp
        ]
        payload["daily"].append(
            {
                "trade_date": information_stamp,
                "returns": returns,
                "market_return": median(returns.values()) if returns else None,
            }
        )

        ranked = [item for item in outlooks if item.trusted][: max(1, top)]
        payload["forecasts"] = [
            row for row in payload["forecasts"] if row.get("trade_date") != stamp
        ]
        payload["forecasts"].extend(
            {
                "trade_date": stamp,
                "information_date": information_stamp,
                "sector": item.name,
                "level": item.level,
                "rank": rank,
                "score": item.score,
                "direction": item.direction,
            }
            for rank, item in enumerate(ranked, start=1)
        )
        payload["daily"].sort(key=lambda row: str(row.get("trade_date") or ""))
        payload["forecasts"].sort(
            key=lambda row: (str(row.get("trade_date") or ""), int(row.get("rank") or 999))
        )
        self._assess(payload)
        write_json(self.path, payload)
        return self.summary(payload)

    def summary(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or self._load()
        rows = []
        for horizon in HORIZONS:
            values = []
            for item in data["forecasts"]:
                outcome = (item.get("outcomes") or {}).get(str(horizon))
                excess = outcome.get("excess_return") if isinstance(outcome, dict) else None
                if isinstance(excess, (int, float)):
                    values.append(float(excess))
            rows.append(_stats(horizon, values))
        samples_5d = next(row["samples"] for row in rows if row["horizon"] == 5)
        return {
            "schema": SCHEMA,
            "forecasts_recorded": len(data["forecasts"]),
            "trusted": samples_5d >= MIN_CALIBRATION_SAMPLES,
            "minimum_calibration_samples": MIN_CALIBRATION_SAMPLES,
            "horizons": rows,
            "interpretation": (
                "只评价历史排名后续是否跑赢板块中位数；样本不足时不得把 score 解读为概率。"
            ),
        }

    def _load(self) -> dict[str, Any]:
        payload = read_json(self.path, default={}) or {}
        if payload.get("schema") != SCHEMA:
            return {"schema": SCHEMA, "daily": [], "forecasts": []}
        if not isinstance(payload.get("daily"), list) or not isinstance(
            payload.get("forecasts"), list
        ):
            return {"schema": SCHEMA, "daily": [], "forecasts": []}
        return payload

    @staticmethod
    def _assess(payload: dict[str, Any]) -> None:
        daily = payload["daily"]
        positions = {str(row.get("trade_date")): index for index, row in enumerate(daily)}
        for forecast in payload["forecasts"]:
            start = positions.get(
                str(forecast.get("information_date") or forecast.get("trade_date") or "")
            )
            if start is None:
                continue
            outcomes = dict(forecast.get("outcomes") or {})
            for horizon in HORIZONS:
                window = daily[start + 1 : start + 1 + horizon]
                if len(window) < horizon:
                    continue
                sector_steps = [row.get("returns", {}).get(forecast.get("sector")) for row in window]
                market_steps = [row.get("market_return") for row in window]
                if not all(isinstance(value, (int, float)) for value in [*sector_steps, *market_steps]):
                    continue
                sector_return = _compound(sector_steps)
                market_return = _compound(market_steps)
                outcomes[str(horizon)] = {
                    "sessions": horizon,
                    "return": round(sector_return, 6),
                    "market_return": round(market_return, 6),
                    "excess_return": round(sector_return - market_return, 6),
                    "outperformed": sector_return > market_return,
                }
            if outcomes:
                forecast["outcomes"] = outcomes


def _compound(values: list[float]) -> float:
    result = 1.0
    for value in values:
        result *= 1.0 + value
    return result - 1.0


def _stats(horizon: int, values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "horizon": horizon,
            "samples": 0,
            "win_rate": None,
            "mean_excess_return": None,
            "median_excess_return": None,
        }
    return {
        "horizon": horizon,
        "samples": len(values),
        "win_rate": round(sum(value > 0 for value in values) / len(values), 4),
        "mean_excess_return": round(sum(values) / len(values), 6),
        "median_excess_return": round(median(values), 6),
    }
