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

SCHEMA = "sector-price-baseline-backtest-v1"
HORIZONS = (1, 3, 5)
LOOKBACK_LONG = 20
LOOKBACK_SHORT = 5
MIN_SECTORS = 20
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
    panel = _panel(series)
    dates = sorted(panel)
    top = max(1, top)
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
    for index in range(LOOKBACK_LONG, len(dates) - max(HORIZONS)):
        signals = _signals(panel, dates, index)
        if len(signals) < MIN_SECTORS:
            continue
        for key, _label, score, eligible in methods:
            ranked = sorted((row for row in signals if eligible(row)), key=score, reverse=True)[:top]
            if len(ranked) < top:
                continue
            for horizon in HORIZONS:
                outcome = _outcome(panel, dates, index, horizon, ranked)
                if outcome is not None:
                    observations[key][horizon].append(outcome)

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
                    _summary(horizon, _holdout(rows[horizon])) for horizon in HORIZONS
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
        "methods": result,
        "notes": [
            "所有信号只使用当日及此前收盘数据，未来收益不参与排序。",
            "holdout 是时间序列最后30%，用于观察近期是否失效；这些固定基线未拟合参数。",
            "3日和5日滚动窗口彼此重叠，样本数不是独立事件数。",
            "超额收益基准为同期全部可用行业的中位收益。",
        ],
    }


def _panel(series: dict[str, tuple[str, list[dict]]]) -> dict[str, dict[str, float]]:
    panel: dict[str, dict[str, float]] = {}
    for name, bars in series.values():
        for bar in bars:
            stamp = str(bar.get("date") or "")
            close = finite(bar.get("close"))
            if stamp and close is not None and close > 0:
                panel.setdefault(stamp, {})[name] = close
    return {stamp: rows for stamp, rows in panel.items() if len(rows) >= MIN_SECTORS}


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


def _holdout(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    start = int(len(rows) * (1.0 - HOLDOUT_SHARE))
    return rows[start:]


def _summary(horizon: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["excess_return"]) for row in rows]
    if not values:
        return {"horizon": horizon, "samples": 0, "win_rate": None, "mean_excess": None, "median_excess": None}
    return {
        "horizon": horizon,
        "samples": len(values),
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "win_rate": round(sum(value > 0 for value in values) / len(values), 4),
        "mean_excess": round(sum(values) / len(values), 6),
        "median_excess": round(median(values), 6),
    }
