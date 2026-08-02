from __future__ import annotations

from datetime import datetime, timedelta

from ashare_decision_support.models.enums import TrendState
from ashare_decision_support.signals.daily_technical import DailyKChartRenderer, DailyTechnicalAnalyzer


def build_bars() -> list[dict]:
    start = datetime(2026, 4, 1)
    bars: list[dict] = []
    for idx in range(90):
        base = 10.0 + idx * 0.045
        pullback = -0.22 if idx % 12 in (8, 9) else 0.0
        open_price = base - 0.06 + pullback
        close = base + 0.08 + pullback
        high = max(open_price, close) + 0.18
        low = min(open_price, close) - 0.20
        if idx in (20, 44, 68):
            low = 10.75
        if idx in (28, 52, 76):
            high = 14.55
        bars.append(
            {
                "timestamp": start + timedelta(days=idx),
                "open": round(open_price, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(close, 2),
                "volume": 100000 + idx * 1200,
            }
        )
    return bars


def test_daily_technical_snapshot_has_levels() -> None:
    analyzer = DailyTechnicalAnalyzer()
    snapshot = analyzer.analyze("603983.SH", build_bars())

    assert snapshot.bar_count == 90
    assert snapshot.trend_state in {TrendState.UPTREND, TrendState.REBOUND}
    assert snapshot.supports
    assert snapshot.resistances
    assert snapshot.supports[0].price < snapshot.last_close
    assert snapshot.resistances[0].price > snapshot.last_close
    assert snapshot.chart_bars


def test_daily_chart_renderer_outputs_svg() -> None:
    analyzer = DailyTechnicalAnalyzer()
    snapshot = analyzer.analyze("603983.SH", build_bars())
    svg = DailyKChartRenderer().render_svg(snapshot)

    assert "<svg" in svg
    assert "603983.SH" in svg
    assert "支撑" in svg or "压力" in svg
