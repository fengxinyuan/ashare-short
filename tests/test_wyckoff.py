from __future__ import annotations

from ashare_decision_support.models.enums import WyckoffBias
from ashare_decision_support.signals.wyckoff import WyckoffAnalyzer


def _build_sideways_bars(base: float, last_close: float, last_low: float, last_high: float, volume: float):
    bars = []
    for idx in range(24):
        price = base + (0.01 * ((idx % 3) - 1))
        bars.append(
            {
                "timestamp": f"2026-07-{idx + 1:02d}",
                "open": price,
                "high": price + 0.15,
                "low": price - 0.15,
                "close": price + 0.02,
                "volume": 1000 - idx * 8,
            }
        )
    bars.append(
        {
            "timestamp": "2026-07-31",
            "open": base,
            "high": last_high,
            "low": last_low,
            "close": last_close,
            "volume": volume,
        }
    )
    return bars


def test_wyckoff_analyzer_identifies_accumulation_spring() -> None:
    analyzer = WyckoffAnalyzer()
    signal = analyzer.analyze(
        "300001.SZ",
        _build_sideways_bars(base=10.0, last_close=10.28, last_low=9.72, last_high=10.34, volume=1800),
    )

    assert signal.spring is True
    assert signal.bias in {WyckoffBias.ACCUMULATION, WyckoffBias.MARKUP}
    assert signal.score > 0


def test_wyckoff_analyzer_identifies_distribution_upthrust() -> None:
    analyzer = WyckoffAnalyzer()
    signal = analyzer.analyze(
        "600001.SH",
        _build_sideways_bars(base=20.0, last_close=19.95, last_low=19.82, last_high=20.35, volume=1900),
    )

    assert signal.upthrust is True
    assert signal.bias in {WyckoffBias.DISTRIBUTION, WyckoffBias.MARKDOWN}
    assert signal.score < 0
