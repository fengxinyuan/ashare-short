from datetime import date, timedelta

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
            bars.append({"date": str(start + timedelta(days=offset)).replace("-", ""), "close": price})
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
