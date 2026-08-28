from __future__ import annotations

from ashare_decision_support.models.enums import EffortResult, WyckoffBias
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


def _build_exhaustion_bars(direction: int, final_volume: float, base: float = 10.0):
    """一段横盘之后，最后一根**缩量**走出 2% 的大幅波动。

    `direction` 取 +1 / -1 决定这根是急拉还是急杀，其余一切（开高低收的形状、
    前 24 根的量）两个方向完全对称，所以两次读数的差只来自方向本身。
    """
    bars = []
    for idx in range(24):
        price = base + 0.01 * ((idx % 3) - 1)
        bars.append(
            {
                "timestamp": f"2026-07-{idx + 1:02d}",
                "open": price,
                "high": price + 0.15,
                "low": price - 0.15,
                "close": price,
                "volume": 1000,
            }
        )
    last_close = base * (1.0 + 0.02 * direction)
    bars.append(
        {
            "timestamp": "2026-08-01",
            "open": base,
            "high": max(base, last_close) + 0.05,
            "low": min(base, last_close) - 0.05,
            "close": last_close,
            "volume": final_volume,
        }
    )
    return bars


def test_exhaustion_scores_by_direction_not_by_a_dead_branch() -> None:
    """缩量急拉要偏空，缩量急杀要偏多——这两件事不能拿到同一个分。

    这条测试盯的是 `wyckoff.py` 里 EXHAUSTION 那一档的**符号**。它原来写的是
    `8.0 if close_position >= 0.5 else 8.0`，两个分支同值，于是不论涨跌都恒扣
    8 分；`close_position` 那个判据是死的，还挂着一行 `noqa: RUF034` 压着告警。
    识别这个状态用的是 `abs(短期涨跌) >= 1.2`，方向在识别那一步就被丢掉了，所以
    打分这一步必须自己把它捡回来。

    **量的是这一档自己的贡献，不是最终分。** 两次读数的价格序列逐字相同，只有
    最后一根的成交量不同——500 落进 EXHAUSTION（量比 0.5），800 落进 NEUTRAL
    （量比 0.8，同时避开吸筹/压缩/强势信号/回踩支撑那几档的量比区间）。两者相减
    剩下的就是 EXHAUSTION 这一档，其余项全部抵消。
    """
    analyzer = WyckoffAnalyzer()

    for direction, expected in ((1, -8.0), (-1, +8.0)):
        exhausted = analyzer.analyze("300002.SZ", _build_exhaustion_bars(direction, 500))
        baseline = analyzer.analyze("300002.SZ", _build_exhaustion_bars(direction, 800))

        assert exhausted.effort_result is EffortResult.EXHAUSTION
        assert baseline.effort_result is EffortResult.NEUTRAL
        # 其余各档都不该被这次量能变化带进来，否则下面的差就不只是衰竭那一档。
        assert not any(
            flag
            for signal in (exhausted, baseline)
            for flag in (
                signal.spring,
                signal.upthrust,
                signal.sign_of_strength,
                signal.last_point_of_support,
            )
        )
        # 两个分数各自已经 round 到 2 位，作差要再 round 一次：28.01 - 36.01 在
        # 二进制浮点里是 -7.999999999999996。
        assert round(exhausted.score - baseline.score, 2) == expected
