"""Walk-forward price baselines for sector ranking.

These baselines use only information available at each signal date.  They do
not tune thresholds and are comparison anchors for the richer daily ranking.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from statistics import median
from typing import Any

from ashare_decision_support.numeric import finite
from ashare_decision_support.sectors.panel import MIN_SECTORS
from ashare_decision_support.signals.series import bar_day

SCHEMA = "sector-price-baseline-backtest-v1"
HORIZONS = (1, 3, 5)
LOOKBACK_LONG = 20
LOOKBACK_SHORT = 5
DEFAULT_TOP = 5
HOLDOUT_SHARE = 0.30


@dataclass(frozen=True)
class _Signal:
    date: str
    sector: str
    recent: float
    prior: float
    long: float


def backtest_price_baselines(
    series: dict[str, tuple[str, list[dict]]], *, top: int = DEFAULT_TOP
) -> dict[str, Any]:
    if isinstance(top, bool) or not isinstance(top, int) or top <= 0:
        raise ValueError("入选板块数必须是正整数")
    panel = _panel(series)
    dates = sorted(panel)
    signal_dates = dates[LOOKBACK_LONG : -min(HORIZONS)]
    cutoff_index = int(len(signal_dates) * (1.0 - HOLDOUT_SHARE))
    holdout_start = signal_dates[cutoff_index] if signal_dates else None
    methods: tuple[tuple[str, str, Callable[[_Signal], float], Callable[[_Signal], bool]], ...] = (
        ("momentum_5d", "5日强势延续", lambda row: row.recent, lambda row: True),
        ("reversal_5d", "5日超跌反转", lambda row: -row.recent, lambda row: True),
        (
            "pullback_in_uptrend",
            "20日上升趋势中的短期回调",
            lambda row: row.long - row.recent,
            lambda row: row.long > 0 and row.recent < row.long,
        ),
        (
            "strength_acceleration",
            "相邻5日强弱加速",
            lambda row: row.recent - row.prior,
            lambda row: True,
        ),
    )
    observations: dict[str, dict[int, list[dict[str, Any]]]] = {
        key: {horizon: [] for horizon in HORIZONS} for key, *_ in methods
    }
    for index in range(LOOKBACK_LONG, len(dates) - min(HORIZONS)):
        signals = _signals(panel, dates, index)
        if len(signals) < MIN_SECTORS:
            continue
        for key, _label, score, eligible in methods:
            ranked = sorted(
                (row for row in signals if eligible(row)),
                key=lambda row: (-score(row), row.sector),
            )[:top]
            if len(ranked) < top:
                continue
            for horizon in HORIZONS:
                if index + horizon >= len(dates):
                    continue
                outcome = _outcome(panel, dates, index, horizon, ranked)
                if outcome is not None:
                    observations[key][horizon].append(outcome)

    common_dates = {
        horizon: set.intersection(
            *(
                {row["date"] for row in _holdout(rows[horizon], holdout_start)}
                for rows in observations.values()
            )
        )
        for horizon in HORIZONS
    }
    result = []
    for key, label, _score, _eligible in methods:
        rows = observations[key]
        result.append(
            {
                "method": key,
                "label": label,
                "top": top,
                "full": [_summary(horizon, rows[horizon]) for horizon in HORIZONS],
                "holdout": [
                    _summary(horizon, _holdout(rows[horizon], holdout_start))
                    for horizon in HORIZONS
                ],
                "common_holdout": [
                    _summary(
                        horizon,
                        [row for row in rows[horizon] if row["date"] in common_dates[horizon]],
                    )
                    for horizon in HORIZONS
                ],
            }
        )
    latest = dates[-1] if dates else None
    return {
        "schema": SCHEMA,
        "source": "同花顺行业指数日线缓存",
        "date_range": {"start": dates[0] if dates else None, "end": latest, "sessions": len(dates)},
        "sector_count": len(series),
        "holdout_share": HOLDOUT_SHARE,
        "holdout_start": holdout_start,
        "execution_simulated": False,
        "transaction_costs_included": False,
        "methods": result,
        "notes": [
            "所有信号只使用当日及此前收盘数据，未来收益不参与排序。",
            "holdout 按共同信号日期轴的最后30%划分，各方法和持有期使用相同截止日；固定基线未拟合参数。",
            "common_holdout 只比较所有方法都有完整结果的相同日期；样本为空时不作横向优劣判断。",
            "缺失或低覆盖日期保留在日期轴上，窗口无法核对时跳过，不用更晚日期替代。整日未采集的缺口仍需交易日历核验。",
            "3日和5日滚动窗口彼此重叠，样本数不是独立事件数。",
            "超额收益基准为同期全部可用行业的中位收益。",
            "入选收益也是行业中位数，win_rate 表示跑赢该基准的比例，不是交易盈利胜率。",
            "收盘信号之后统计收盘到收盘的价格变化，未模拟成交、T+1可卖数量、涨跌停、费用或滑点，不是可实现净收益。",
            "回测使用当前缓存的行业样本，未重建历史成分与退市样本，仍可能存在幸存者偏差。",
        ],
    }


def _panel(series: dict[str, tuple[str, list[dict]]]) -> dict[str, dict[str, float]]:
    panel: dict[str, dict[str, float]] = {}
    for code, (_name, bars) in series.items():
        for bar in bars:
            stamp = bar_day(bar)
            if stamp is None:
                continue
            current = panel.setdefault(stamp, {})
            close = finite(bar.get("close"))
            if close is not None and close > 0:
                current[code] = close
    return panel


def _signals(panel: dict[str, dict[str, float]], dates: list[str], index: int) -> list[_Signal]:
    current = panel[dates[index]]
    short = panel[dates[index - LOOKBACK_SHORT]]
    prior = panel[dates[index - LOOKBACK_SHORT * 2]]
    long = panel[dates[index - LOOKBACK_LONG]]
    common = current.keys() & short.keys() & prior.keys() & long.keys()
    return [
        _Signal(
            date=dates[index],
            sector=name,
            recent=current[name] / short[name] - 1.0,
            prior=short[name] / prior[name] - 1.0,
            long=current[name] / long[name] - 1.0,
        )
        for name in common
    ]


def _outcome(
    panel: dict[str, dict[str, float]],
    dates: list[str],
    index: int,
    horizon: int,
    ranked: list[_Signal],
) -> dict[str, Any] | None:
    current = panel[dates[index]]
    future = panel[dates[index + horizon]]
    market_names = current.keys() & future.keys()
    market_returns = [future[name] / current[name] - 1.0 for name in market_names]
    selected = [
        future[row.sector] / current[row.sector] - 1.0
        for row in ranked
        if row.sector in current and row.sector in future
    ]
    if len(market_returns) < MIN_SECTORS or len(selected) != len(ranked):
        return None
    market_return = median(market_returns)
    selected_return = median(selected)
    return {
        "date": dates[index],
        "return": selected_return,
        "market_return": market_return,
        "excess_return": selected_return - market_return,
    }


def _holdout(rows: list[dict[str, Any]], start: str | None) -> list[dict[str, Any]]:
    return [row for row in rows if start is not None and row["date"] >= start]


def _summary(horizon: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["excess_return"]) for row in rows]
    if not values:
        return {
            "horizon": horizon,
            "samples": 0,
            "win_rate": None,
            "mean_excess": None,
            "median_excess": None,
        }
    return {
        "horizon": horizon,
        "samples": len(values),
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "win_rate": round(sum(value > 0 for value in values) / len(values), 4),
        "mean_excess": round(sum(values) / len(values), 6),
        "median_excess": round(median(values), 6),
    }
