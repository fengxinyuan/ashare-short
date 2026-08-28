"""行业/概念内的量价龙头候选。

这里的“龙头”只表示同一板块内近期量价、流动性和趋势相对靠前，不表示公司是产业
龙头，也不表示未来会上涨。产业地位、订单和政策受益关系需要外部研究者另行核验。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.base import fetch_daily_bars
from ashare_decision_support.adapters.ths_taxonomy import ThsTaxonomySource
from ashare_decision_support.adapters.tickflow_adapter import TickFlowAdapter
from ashare_decision_support.artifacts import LEADERS_LATEST_NAME
from ashare_decision_support.numeric import finite
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.sectors import ThsConceptTaxonomy, ThsIndustryTaxonomy
from ashare_decision_support.sectors.base import SectorDefinition
from ashare_decision_support.signals.daily_technical import DailyTechnicalAnalyzer
from ashare_decision_support.signals.series import change, series
from ashare_decision_support.symbols import normalize_symbol

LEADERS_SCHEMA = "ashare-sector-leaders-v1"
DEFAULT_LOOKBACK_DAYS = 180
DEFAULT_MIN_BARS = 65
DEFAULT_TOP_PER_SECTOR = 3

_WEIGHTS = {
    "return_5d": 0.10,
    "return_20d": 0.30,
    "return_60d": 0.20,
    "liquidity": 0.15,
    "trend_quality": 0.15,
    "volume_activity": 0.10,
}


def build_sector_leaders(
    bars_by_symbol: dict[str, list[dict]],
    definitions: list[SectorDefinition],
    *,
    names: dict[str, str | None] | None = None,
    as_of: str | None = None,
    top_per_sector: int = DEFAULT_TOP_PER_SECTOR,
    min_bars: int = DEFAULT_MIN_BARS,
) -> dict[str, Any]:
    """对已取好的日线做板块内相对排名；纯计算，不访问网络也不写文件。"""
    top_n = max(1, int(top_per_sector))
    minimum = max(20, int(min_bars))
    normalized_bars = {
        normalize_symbol(symbol): rows
        for symbol, rows in bars_by_symbol.items()
        if normalize_symbol(symbol)
    }
    metrics = _symbol_metrics(normalized_bars, minimum)
    labels = {normalize_symbol(key): value for key, value in (names or {}).items()}

    sectors: list[dict[str, Any]] = []
    for definition in definitions:
        members = [
            (symbol, metrics[symbol])
            for raw in definition.symbols
            if (symbol := normalize_symbol(raw)) in metrics
        ]
        if not members:
            sectors.append(
                {
                    "name": definition.name,
                    "level": definition.level,
                    "constituents": definition.size,
                    "covered": 0,
                    "coverage_ratio": 0.0,
                    "leaders": [],
                    "warnings": ["没有成分股具备足够日线，不能给出量价龙头候选"],
                }
            )
            continue
        percentiles = _percentiles(members)
        ranked = []
        for symbol, metric in members:
            components = percentiles[symbol]
            score = sum(components[key] * weight for key, weight in _WEIGHTS.items())
            ranked.append(
                {
                    "symbol": symbol,
                    "name": labels.get(symbol),
                    "leadership_rank": round(score, 2),
                    "rank_components": {
                        key: round(value, 4) for key, value in components.items()
                    },
                    **metric,
                }
            )
        ranked.sort(key=lambda item: (-item["leadership_rank"], item["symbol"]))
        coverage = len(members) / max(definition.size, 1)
        warnings = []
        if coverage < 0.5:
            warnings.append("可用日线覆盖不足 50%，板块内名次不稳定")
        sectors.append(
            {
                "name": definition.name,
                "level": definition.level,
                "constituents": definition.size,
                "covered": len(members),
                "coverage_ratio": round(coverage, 4),
                "leaders": ranked[:top_n],
                "warnings": warnings,
            }
        )

    sectors.sort(key=lambda item: (item["level"], item["name"]))
    return {
        "schema": LEADERS_SCHEMA,
        "as_of": as_of,
        "contract": {
            "leader_definition": "relative price-action and liquidity leader inside one sector",
            "not_business_leader": True,
            "not_forecast_or_recommendation": True,
            "missing_values": "null or empty collection; external analysts must not invent values",
        },
        "methodology": {
            "scope": "sector-relative percentile ranking",
            "minimum_bars": minimum,
            "weights": dict(_WEIGHTS),
            "warning": "weights are fixed and unbacktested; leadership_rank is not a probability",
        },
        "sector_count": len(sectors),
        "sectors": sectors,
    }


def refresh_sector_leaders(
    *,
    output_dir: str | Path = "briefs",
    levels: tuple[str, ...] = ("industry", "concept"),
    industry_level: str = "SW1",
    top_per_sector: int = DEFAULT_TOP_PER_SECTOR,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_bars: int = DEFAULT_MIN_BARS,
    adapter: Any | None = None,
    taxonomy_source: Any | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """刷新全市场日线并落一份供外部 AI 使用的板块龙头事实包。"""
    wanted = {str(level).lower() for level in levels}
    unknown = wanted - {"industry", "concept"}
    if unknown:
        raise ValueError(f"unsupported levels: {sorted(unknown)}")

    source = taxonomy_source or ThsTaxonomySource()
    report = progress or (lambda message: None)
    definitions: list[SectorDefinition] = []
    if "industry" in wanted:
        definitions.extend(ThsIndustryTaxonomy(source, level=industry_level).list_sectors())
    if "concept" in wanted:
        definitions.extend(ThsConceptTaxonomy(source).list_sectors())
    report(f"分类加载完成：{len(definitions)} 个板块")

    snapshot = source.load()
    symbols = sorted(
        {
            normalize_symbol(symbol)
            for definition in definitions
            for symbol in definition.symbols
            if normalize_symbol(symbol)
        }
    )
    today = beijing_today()
    market = adapter or TickFlowAdapter()
    fetch_notes: list[str] = []
    report(f"开始读取 {len(symbols)} 只股票的日线")
    bars = fetch_daily_bars(
        market,
        symbols,
        today - timedelta(days=max(90, int(lookback_days))),
        today,
        notes=fetch_notes,
    )
    report(f"日线读取完成：{sum(bool(rows) for rows in bars.values())}/{len(symbols)} 只有数据")
    payload = build_sector_leaders(
        bars,
        definitions,
        names={symbol: profile.name for symbol, profile in snapshot.profiles.items()},
        as_of=str(today),
        top_per_sector=top_per_sector,
        min_bars=min_bars,
    )
    report(f"板块内排名完成：{payload['sector_count']} 个板块")
    payload["data_notes"] = [*snapshot.notes, *fetch_notes]
    target = Path(output_dir) / LEADERS_LATEST_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    payload["payload_path"] = str(target)
    return payload


def _symbol_metrics(
    bars_by_symbol: dict[str, list[dict]], min_bars: int
) -> dict[str, dict[str, Any]]:
    analyzer = DailyTechnicalAnalyzer()
    snapshots = analyzer.analyze_many(bars_by_symbol)
    out: dict[str, dict[str, Any]] = {}
    for symbol, bars in bars_by_symbol.items():
        snapshot = snapshots.get(symbol)
        closes = series(bars, "close")
        if snapshot is None or len(closes) < min_bars or snapshot.last_close <= 0:
            continue
        amount_values = [value for row in bars[-20:] if (value := finite(row.get("amount"))) is not None]
        average_amount = sum(amount_values) / len(amount_values) if amount_values else None
        trend_quality = _trend_quality(snapshot)
        out[symbol] = {
            "signal_date": snapshot.source_ts.date().isoformat(),
            "last_close": snapshot.last_close,
            "return_5d": change(closes, 5),
            "return_20d": change(closes, 20),
            "return_60d": change(closes, 60),
            "average_amount_20d_yi": (
                round(average_amount / 1e8, 4) if average_amount is not None else None
            ),
            "volume_ratio_20d": snapshot.volume_ratio_20d,
            "range_position_60d": snapshot.range_position_60d,
            "ma20": snapshot.ma20,
            "ma60": snapshot.ma60,
            "trend_state": snapshot.trend_state.value,
            "trend_quality": round(trend_quality, 4),
        }
    return out


def _trend_quality(snapshot: Any) -> float:
    above20 = 1.0 if snapshot.ma20 and snapshot.last_close >= snapshot.ma20 else 0.0
    above60 = 1.0 if snapshot.ma60 and snapshot.last_close >= snapshot.ma60 else 0.0
    position = finite(snapshot.range_position_60d) or 0.0
    return (above20 + above60 + max(0.0, min(position, 1.0))) / 3.0


def _percentiles(
    members: list[tuple[str, dict[str, Any]]],
) -> dict[str, dict[str, float]]:
    raw: dict[str, dict[str, float]] = {}
    for symbol, metric in members:
        amount = finite(metric.get("average_amount_20d_yi"))
        volume = finite(metric.get("volume_ratio_20d"))
        return_5d = finite(metric.get("return_5d"))
        return_20d = finite(metric.get("return_20d"))
        return_60d = finite(metric.get("return_60d"))
        raw[symbol] = {
            "return_5d": return_5d if return_5d is not None else -1.0,
            "return_20d": return_20d if return_20d is not None else -1.0,
            "return_60d": return_60d if return_60d is not None else -1.0,
            "liquidity": math.log10(max(amount or 0.0, 0.001)),
            "trend_quality": finite(metric.get("trend_quality")) or 0.0,
            "volume_activity": min(max(volume or 0.0, 0.0), 3.0),
        }
    result = {symbol: {} for symbol, _ in members}
    for key in _WEIGHTS:
        ordered = sorted(raw, key=lambda symbol: (raw[symbol][key], symbol))
        denominator = max(len(ordered) - 1, 1)
        start = 0
        while start < len(ordered):
            end = start + 1
            value = raw[ordered[start]][key]
            while end < len(ordered) and raw[ordered[end]][key] == value:
                end += 1
            percentile = ((start + end - 1) / 2.0) / denominator
            for symbol in ordered[start:end]:
                result[symbol][key] = percentile
            start = end
    return result
