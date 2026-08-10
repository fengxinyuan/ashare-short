from __future__ import annotations

from datetime import datetime, timedelta

from ashare_decision_support.models.enums import TrendState
from ashare_decision_support.schedule.timezone import BEIJING_TZ
from ashare_decision_support.signals.daily_technical import (
    DailyKChartRenderer,
    DailyTechnicalAnalyzer,
)


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


# ---- 时间戳解析 -------------------------------------------------------------


def test_bar_datetime_reads_every_shape_the_feeds_hand_back() -> None:
    """`fromisoformat` 排到 `strptime` 前面之后，兜底那几种格式必须一个不少。

    收口前的顺序是先试四个 `strptime` 格式、最后才 `fromisoformat`，于是最常见的
    日线日期每根要付两次 `strptime`。调换顺序只有在**斜杠格式与不补零的写法照样
    认得**的前提下才是等价的——这条钉的就是那个前提。
    """
    from ashare_decision_support.signals.series import bar_datetime

    expected = datetime(2025, 8, 1)
    assert bar_datetime("2025-08-01") == expected
    assert bar_datetime("20250801") == expected
    assert bar_datetime("2025/08/01") == expected
    assert bar_datetime("2025-8-1") == expected          # 月/日不补零，只有 strptime 认
    assert bar_datetime("2025/8/1") == expected

    stamped = datetime(2025, 8, 1, 9, 30, 0)
    assert bar_datetime("2025-08-01 09:30:00") == stamped
    assert bar_datetime("2025-08-01T09:30:00") == stamped
    assert bar_datetime("2025/08/01 09:30:00") == stamped

    # datetime 原样返回；秒与毫秒时间戳都认。
    assert bar_datetime(stamped) is stamped
    # epoch 要按**北京**折算，不能按宿主机时区：裸 `stamped.timestamp()` 在 UTC
    # 容器里会让这条断言凭空要求偏 8 小时的结果，而那正是 `beijing_from_epoch`
    # 存在的理由。
    epoch = stamped.replace(tzinfo=BEIJING_TZ).timestamp()
    assert bar_datetime(epoch) == stamped
    assert bar_datetime(epoch * 1000.0) == stamped


def test_bar_datetime_refuses_rather_than_inventing_a_date() -> None:
    """认不出来就抛——静默返回 `now()` 会让一根坏 K 线伪装成今天的数据。"""
    import pytest

    from ashare_decision_support.signals.series import bar_datetime

    with pytest.raises(ValueError):
        bar_datetime("八月一日")


def test_normalize_bars_is_shared_by_both_analyzers() -> None:
    """两个分析器归一出来的必须逐字段相同——它们此前是两份各自维护的实现。"""
    from ashare_decision_support.signals.wyckoff import WyckoffAnalyzer

    bars = build_bars()
    assert DailyTechnicalAnalyzer()._normalize_rows(bars) == WyckoffAnalyzer()._normalize_rows(bars)


def test_normalize_bars_skips_a_zero_timestamp_and_falls_back_to_date() -> None:
    """**这条修的是 wyckoff 那份的一个真错。**

    它此前只跳过 `None` 与 `""`，于是一根 `timestamp=0` 而 `date` 正常的 K 线会
    被读成 1970-01-01——而 `daily_technical` 那份用 `or` 链，落空就往后试，读到的
    是 `date`。两份实现对同一根 K 线给出相差 55 年的时间戳，且都不会报错。
    """
    from ashare_decision_support.signals.series import normalize_bars

    rows = normalize_bars([{"timestamp": 0, "date": "2025-08-01", "close": 10.0}])
    assert rows[0]["timestamp"] == datetime(2025, 8, 1)


def test_normalize_bars_fills_the_gaps_without_inventing_a_close() -> None:
    """收盘价是唯一不能猜的字段：缺了整根丢弃，其余按开收包络兜底。"""
    from ashare_decision_support.signals.series import normalize_bars

    rows = normalize_bars(
        [
            {"date": "2025-08-01", "close": 10.0},                    # 只有收盘
            {"date": "2025-08-02", "open": 9.0, "high": 11.0, "low": 8.5, "close": 10.5},
            {"date": "2025-08-03", "open": 9.0},                      # 没有收盘 -> 丢
            "not-a-dict",
        ]
    )
    assert len(rows) == 2
    first = rows[0]
    assert first["open"] == 10.0 and first["high"] == 10.0 and first["low"] == 10.0
    assert first["volume"] == 0.0
