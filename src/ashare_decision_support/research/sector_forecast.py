"""Build a concise, falsifiable sector forecast from the latest morning facts."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any

from ashare_decision_support.numeric import median_of
from ashare_decision_support.research.daily_sector import (
    information_date,
    load_daily_sector_analysis,
)
from ashare_decision_support.schedule.calendar import next_session
from ashare_decision_support.sectors.clusters import cluster_ranked_sectors
from ashare_decision_support.sectors.panel import rounded
from ashare_decision_support.sectors.predict import DIRECTION_BANDS
from ashare_decision_support.signals.series import parse_bar_date

SCHEMA = "ashare-sector-forecast-v1"

# 盘中确认/否决的判据，**盘前写进假设、盘中拿来对账的是同一组数**。

# 此前 `execution_protocol` 里的「60%」「45%」「09:35」是三段字面量，
# `sector_confirmation.py` 里判 `breadth >= 0.60` / `< 0.45` / `time(9, 35)` 是另外三段。
# 两处各自演化的话，盘前对读者承诺的失效条件和盘中真正执行的会是两套数，而且不报错。
CONFIRM_AFTER = time(9, 35)
PREFERRED_CROSS_SECTION = time(9, 45)
CONFIRM_BREADTH = 0.60
INVALIDATE_BREADTH = 0.45
# 「至少几个不重叠行业共同确认」——概念之间成分股大量重叠，一个主题下三个概念同涨
# 只是一条证据。
MIN_INDEPENDENT_INDUSTRIES = 2

CONFIRM_CONDITIONS = (
    f"主题簇上涨广度维持在{CONFIRM_BREADTH:.0%}以上",
    "相对全市场超额收益为正且没有连续两轮下降",
    "成交额占比上升，同时价格没有转负",
    f"至少{MIN_INDEPENDENT_INDUSTRIES}个不重叠行业共同确认；同一主题下多个概念不重复计数",
)
INVALIDATE_CONDITIONS = (
    f"上涨广度跌破{INVALIDATE_BREADTH:.0%}",
    "高开后超额收益转负且成交额放大",
    "只有单只龙头维持、板块中位数和广度不跟随",
    "预测所用数据被标记陈旧或盘中行情源停更",
)

# 「偏强 / 偏弱」的切点取 `sectors/predict.py` 打分时用的那一组，不在这里再写一遍
# 68 和 43：两份数字各自演化的话，预测包会把一个 `predict` 判成「中性偏多」的分数
# 写成「偏强」，而且不报错。
_BULLISH_FLOOR = next(threshold for threshold, label in DIRECTION_BANDS if label == "偏多")
_NEUTRAL_FLOOR = next(threshold for threshold, label in DIRECTION_BANDS if label == "中性")


def execution_protocol() -> dict[str, Any]:
    """盘中对账协议。预测包、盘前假设两处都从这里取，字面量只此一份。"""
    return {
        "confirm_after": (f"{CONFIRM_AFTER:%H:%M}，优先使用{PREFERRED_CROSS_SECTION:%H:%M}截面"),
        "confirm": list(CONFIRM_CONDITIONS),
        "invalidate": list(INVALIDATE_CONDITIONS),
    }


def build_sector_forecast(
    output_dir: str | Path = "briefs",
    *,
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """供最新事实包与按时点归档的计划共用同一套预测口径。"""
    payload = load_daily_sector_analysis(output_dir) if facts is None else facts
    sectors = [item for item in payload.get("sectors") or [] if isinstance(item, dict)]
    clusters = payload.get("sector_clusters") or cluster_ranked_sectors(sectors)
    validation = payload.get("sector_forecast_validation") or {}
    rotation = payload.get("rotation_price") or {}
    turnover = payload.get("turnover_migration") or {}
    market = payload.get("market") or {}
    forecasts = _cluster_forecasts(clusters, sectors)
    return {
        "schema": SCHEMA,
        # 三个日期各说各的：目标交易日是盘中要对账的那一天，行情截止日是排序用到的
        # 最后一根日线，`data_health` 里还带着计划日。周日跑的那份三者各不相同。
        "target_session": target_session(payload.get("trade_date")),
        "information_date": information_date(payload),
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
                "历史样本足够时仅表示同类排名的实测跑赢频率；当前不得把板块评分解释为上涨概率。"
            ),
        },
        "execution_protocol": execution_protocol(),
        "notes": [
            "这是待盘中证伪的情景排序，不是收益保证或交易指令。",
            "板块评分不是概率；使用 sector_baseline_backtest 查看当前本地历史基线。",
            "未经独立样本验证的价格信号默认必须经过盘中确认。",
        ],
    }


def _cluster_forecasts(
    clusters: dict[str, Any], sectors: list[dict[str, Any]]
) -> list[dict[str, Any]]:
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
    # 中位数走 `numeric.median_of`（bool 与 NaN 不进样本），小数位走 `panel.rounded`
    # ——此前这里自带一份排序取中，和全仓另外十几处中位数不是同一个口径。
    ret_1d = rounded(median_of(item.get("ret_1d") for item in rows))
    ret_5d = rounded(median_of(item.get("ret_5d") for item in rows))
    breadth = rounded(median_of(item.get("breadth") for item in rows))
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
        "exposures": [{"name": item.get("name"), "level": item.get("level")} for item in rows],
        "independent_signal": not clustered or len(rows) == 1,
        "ret_1d": ret_1d,
        "ret_5d": ret_5d,
        "breadth": breadth,
        "reasons": (lead.get("reasons") or [])[:4],
        "risks": (lead.get("warnings") or [])[:3],
    }


def target_session(trade_date: Any) -> str | None:
    """开市计划日返回当天，休市日返回下一交易日；日期无效时返回 None。"""
    day = parse_bar_date(trade_date)
    return next_session(day).isoformat() if day is not None else None


def _market_state(market: dict[str, Any], rotation: dict[str, Any]) -> str:
    score = market.get("score")
    persistence = rotation.get("persistence")
    if isinstance(score, int | float) and score >= _BULLISH_FLOOR:
        if isinstance(persistence, int | float) and persistence < 0.25:
            return "偏强但快速轮动"
        return "偏强"
    # 低于「中性」档的下沿才算偏弱；`predict` 把 43 本身判成中性，这里跟它一致。
    if isinstance(score, int | float) and score < _NEUTRAL_FLOOR:
        return "偏弱"
    return "震荡"
