"""Build a concise, falsifiable sector forecast from the latest morning facts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.research.daily_sector import load_daily_sector_analysis
from ashare_decision_support.sectors.clusters import cluster_ranked_sectors

SCHEMA = "ashare-sector-forecast-v1"


def build_sector_forecast(output_dir: str | Path = "briefs") -> dict[str, Any]:
    payload = load_daily_sector_analysis(output_dir)
    sectors = [item for item in payload.get("sectors") or [] if isinstance(item, dict)]
    clusters = payload.get("sector_clusters") or cluster_ranked_sectors(sectors)
    validation = payload.get("sector_forecast_validation") or {}
    rotation = payload.get("rotation_price") or {}
    turnover = payload.get("turnover_migration") or {}
    market = payload.get("market") or {}
    forecasts = _cluster_forecasts(clusters, sectors)
    return {
        "schema": SCHEMA,
        "target_session": payload.get("trade_date"),
        "information_date": _iso_date(rotation.get("trade_date") or payload.get("trade_date")),
        "data_health": payload.get("data_health"),
        "market": {
            "state": _market_state(market, rotation),
            "direction": market.get("direction"),
            "score": market.get("score"),
            "breadth": market.get("breadth"),
            "rotation": rotation.get("adjacent_label") or rotation.get("switch_label"),
            "persistence": rotation.get("persistence"),
            "turnover_change": turnover.get("total_change"),
            "turnover_near_term": turnover.get("total_trend"),
        },
        "forecasts": forecasts,
        "crowding": clusters.get("crowded") or [],
        "historical_validation": validation,
        "confidence": {
            "status": "calibrated" if validation.get("trusted") else "uncalibrated",
            "meaning": (
                "历史样本足够时仅表示同类排名的实测跑赢频率；"
                "当前不得把板块评分解释为上涨概率。"
            ),
        },
        "execution_protocol": {
            "confirm_after": "09:35，优先使用09:45截面",
            "confirm": [
                "主题簇上涨广度维持在60%以上",
                "相对全市场超额收益为正且没有连续两轮下降",
                "成交额占比上升，同时价格没有转负",
                "至少两个不重叠行业共同确认；同一主题下多个概念不重复计数",
            ],
            "invalidate": [
                "上涨广度跌破45%",
                "高开后超额收益转负且成交额放大",
                "只有单只龙头维持、板块中位数和广度不跟随",
                "预测所用数据被标记陈旧或盘中行情源停更",
            ],
        },
        "notes": [
            "这是待盘中证伪的情景排序，不是收益保证或交易指令。",
            "板块评分不是概率；使用 sector_baseline_backtest 查看当前本地历史基线。",
            "未经独立样本验证的价格信号默认必须经过盘中确认。",
        ],
    }


def _cluster_forecasts(clusters: dict[str, Any], sectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {str(item.get("name")): item for item in sectors}
    result = []
    used: set[str] = set()
    for cluster in clusters.get("clusters") or []:
        names = [str(item.get("name")) for item in cluster.get("sectors") or []]
        rows = [by_name[name] for name in names if name in by_name]
        if not rows:
            continue
        used.update(names)
        result.append(_forecast(str(cluster.get("name")), rows, clustered=True))
    for item in sectors:
        name = str(item.get("name") or "")
        if name and name not in used:
            result.append(_forecast(name, [item], clustered=False))
    return sorted(result, key=lambda item: item["score"], reverse=True)


def _forecast(name: str, rows: list[dict[str, Any]], *, clustered: bool) -> dict[str, Any]:
    lead = max(rows, key=lambda item: float(item.get("score") or 0.0))
    ret_1d = _median(item.get("ret_1d") for item in rows)
    ret_5d = _median(item.get("ret_5d") for item in rows)
    breadth = _median(item.get("breadth") for item in rows)
    if ret_1d is not None and ret_5d is not None and ret_1d > 0 and ret_5d > 0:
        scenario = "延续观察"
    elif ret_5d is not None and ret_5d < 0 and breadth is not None and breadth >= 0.5:
        scenario = "超跌修复观察"
    else:
        scenario = "等待方向确认"
    return {
        "name": name,
        "scenario": scenario,
        "score": lead.get("score"),
        "members": [item.get("name") for item in rows],
        "exposures": [
            {"name": item.get("name"), "level": item.get("level")} for item in rows
        ],
        "independent_signal": not clustered or len(rows) == 1,
        "ret_1d": ret_1d,
        "ret_5d": ret_5d,
        "breadth": breadth,
        "reasons": (lead.get("reasons") or [])[:4],
        "risks": (lead.get("warnings") or [])[:3],
    }


def _market_state(market: dict[str, Any], rotation: dict[str, Any]) -> str:
    score = market.get("score")
    persistence = rotation.get("persistence")
    if isinstance(score, int | float) and score >= 68:
        if isinstance(persistence, int | float) and persistence < 0.25:
            return "偏强但快速轮动"
        return "偏强"
    if isinstance(score, int | float) and score <= 43:
        return "偏弱"
    return "震荡"


def _median(values: Any) -> float | None:
    usable = sorted(float(value) for value in values if isinstance(value, int | float))
    if not usable:
        return None
    middle = len(usable) // 2
    if len(usable) % 2:
        return round(usable[middle], 6)
    return round((usable[middle - 1] + usable[middle]) / 2.0, 6)


def _iso_date(value: Any) -> Any:
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    return value
