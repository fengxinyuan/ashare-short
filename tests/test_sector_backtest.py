from datetime import date, timedelta

import pytest

from ashare_decision_support.sectors.backtest import backtest_price_baselines


def _series() -> dict:
    start = date(2026, 1, 1)
    result = {}
    for sector in range(25):
        price = 100.0
        bars = []
        for offset in range(80):
            drift = 0.01 if sector < 5 else 0.0
            price *= 1.0 + drift
            bars.append(
                {"date": str(start + timedelta(days=offset)).replace("-", ""), "close": price}
            )
        result[str(sector)] = (f"板块{sector}", bars)
    return result


def test_momentum_baseline_reports_walk_forward_holdout() -> None:
    payload = backtest_price_baselines(_series(), top=5)
    momentum = next(item for item in payload["methods"] if item["method"] == "momentum_5d")
    holdout = {item["horizon"]: item for item in momentum["holdout"]}

    assert payload["schema"] == "sector-price-baseline-backtest-v1"
    assert holdout[5]["samples"] > 0
    assert holdout[5]["win_rate"] == 1.0
    assert holdout[5]["mean_excess"] > 0
    assert any("未来收益不参与排序" in note for note in payload["notes"])


def test_each_horizon_uses_its_own_complete_windows() -> None:
    payload = backtest_price_baselines(_series())
    momentum = next(item for item in payload["methods"] if item["method"] == "momentum_5d")
    samples = {item["horizon"]: item["samples"] for item in momentum["full"]}
    assert samples == {1: 59, 3: 57, 5: 55}


def test_mixed_date_formats_and_duplicate_rows_do_not_split_sessions() -> None:
    original = _series()
    mixed = _series()
    for index, (_name, bars) in enumerate(mixed.values()):
        if index % 2:
            for bar in bars:
                bar["date"] = date.fromisoformat(bar["date"]).isoformat()
        bars.append(dict(bars[-1]))
    assert backtest_price_baselines(mixed) == backtest_price_baselines(original)


@pytest.mark.parametrize("keep_one", [True, False])
def test_low_coverage_day_is_not_removed_from_return_horizons(keep_one: bool) -> None:
    series = _series()
    for index, (_name, bars) in enumerate(series.values()):
        if not keep_one or index != 0:
            bars[60]["close"] = None
    payload = backtest_price_baselines(series)
    assert payload["date_range"]["sessions"] == 80
    momentum = next(item for item in payload["methods"] if item["method"] == "momentum_5d")
    daily = next(item for item in momentum["full"] if item["horizon"] == 1)
    assert daily["mean_excess"] == pytest.approx(0.01)
    assert daily["samples"] < 59
    assert any("日期" in note and "缺" in note for note in payload["notes"])


def test_sector_codes_keep_identical_names_separate() -> None:
    series = _series()
    same_names = {code: ("同名行业", bars) for code, (_name, bars) in series.items()}
    assert (
        backtest_price_baselines(same_names)["methods"]
        == backtest_price_baselines(series)["methods"]
    )


def test_holdout_cutoff_is_shared_even_when_a_style_stops_signalling() -> None:
    series = _series()
    for index, (_name, bars) in enumerate(series.values()):
        if index < 5:
            peak = bars[54]["close"]
            for offset in range(55, len(bars)):
                bars[offset]["close"] = peak * 0.97 ** (offset - 54)
    payload = backtest_price_baselines(series)
    cutoff = payload["holdout_start"]
    assert cutoff == str(date(2026, 1, 1) + timedelta(days=61))
    for method in payload["methods"]:
        for sample in method["holdout"]:
            if sample["samples"]:
                assert sample["start"] >= cutoff
    for horizon in (1, 3, 5):
        matched = [
            next(item for item in method["common_holdout"] if item["horizon"] == horizon)
            for method in payload["methods"]
        ]
        assert len({(item["samples"], item.get("start"), item.get("end")) for item in matched}) == 1


def test_equal_signals_use_code_order_and_cannot_see_future_returns(monkeypatch) -> None:
    from ashare_decision_support.sectors import backtest

    series = _series()
    for _name, bars in series.values():
        for bar in bars[:21]:
            bar["close"] = 100.0
    seen = []
    outcome = backtest._outcome

    def capture(panel, dates, index, horizon, ranked):
        if index == 20 and horizon == 1:
            seen.append([row.sector for row in ranked])
        return outcome(panel, dates, index, horizon, ranked)

    monkeypatch.setattr(backtest, "_outcome", capture)
    backtest_price_baselines(series)
    before = list(seen)
    assert before[0] == sorted(series)[:5]
    seen.clear()
    for _name, bars in series.values():
        for bar in bars[21:]:
            bar["close"] *= 7.0
    backtest_price_baselines(series)
    assert seen == before


@pytest.mark.parametrize("top", [0, -1, True, 2.5])
def test_invalid_selection_sizes_are_rejected(top) -> None:
    with pytest.raises(ValueError, match="正整数"):
        backtest_price_baselines(_series(), top=top)
