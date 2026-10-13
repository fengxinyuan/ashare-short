from __future__ import annotations

import json
from datetime import date, timedelta

from ashare_decision_support.sectors.base import SectorMetrics, SectorOutlook
from ashare_decision_support.sectors.outcomes import SectorForecastOutcomeStore


def _metric(name: str, ret: float) -> SectorMetrics:
    return SectorMetrics(name=name, constituents=10, covered=10, ret_1d=ret)


def _outlook(name: str, score: float) -> SectorOutlook:
    return SectorOutlook(name=name, score=score, metrics=_metric(name, 0.0), level="THS_INDUSTRY")


def test_sector_forecasts_gain_forward_relative_outcomes(tmp_path) -> None:
    store = SectorForecastOutcomeStore(tmp_path / "sector-outcomes.json")
    start = date(2026, 8, 3)
    outlooks = [_outlook("电子", 80), _outlook("银行", 60)]

    summary = {}
    for offset in range(6):
        summary = store.record(
            start + timedelta(days=offset),
            outlooks,
            {
                "电子": _metric("电子", 0.02 if offset else 0.0),
                "银行": _metric("银行", 0.0),
                "煤炭": _metric("煤炭", -0.01 if offset else 0.0),
            },
            top=2,
        )

    by_horizon = {item["horizon"]: item for item in summary["horizons"]}
    assert by_horizon[1]["samples"] == 10
    assert by_horizon[5]["samples"] == 2
    assert by_horizon[5]["win_rate"] == 0.5
    assert summary["trusted"] is False
    assert "解读为概率" in summary["interpretation"]


def test_re_recording_a_day_is_idempotent(tmp_path) -> None:
    store = SectorForecastOutcomeStore(tmp_path / "sector-outcomes.json")
    today = date(2026, 8, 3)
    outlooks = [_outlook("电子", 80)]
    metrics = {"电子": _metric("电子", 0.01)}

    store.record(today, outlooks, metrics)
    summary = store.record(today, outlooks, metrics)

    assert summary["forecasts_recorded"] == 1


def test_signal_date_is_separate_from_information_date(tmp_path) -> None:
    path = tmp_path / "sector-outcomes.json"
    store = SectorForecastOutcomeStore(path)
    store.record(
        date(2026, 8, 28),
        [_outlook("电子", 80)],
        {"电子": _metric("电子", 0.03)},
        data_date=date(2026, 8, 27),
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["daily"][0]["trade_date"] == "2026-08-27"
    assert payload["forecasts"][0]["trade_date"] == "2026-08-28"
    assert payload["forecasts"][0]["information_date"] == "2026-08-27"
    assert "outcomes" not in payload["forecasts"][0]
