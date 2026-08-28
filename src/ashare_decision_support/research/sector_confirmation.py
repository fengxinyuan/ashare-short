"""Confirm or falsify the latest sector forecast with intraday snapshots."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from statistics import median
from typing import Any

from ashare_decision_support.research.sector_forecast import build_sector_forecast
from ashare_decision_support.schedule.clock import (
    AFTERNOON_CLOSE,
    AFTERNOON_OPEN,
    MORNING_CLOSE,
)
from ashare_decision_support.sectors.intraday import (
    LEVEL_INDUSTRY,
    SectorRead,
    SectorSnapshot,
    load_history,
    store_path,
)

SCHEMA = "ashare-sector-confirmation-v2"
MIN_AMOUNT_SHARE_RISE = 0.0001


def build_sector_confirmation(
    output_dir: str | Path = "briefs", *, store_root: Path | None = None
) -> dict[str, Any]:
    forecast = build_sector_forecast(output_dir)
    target = str(forecast.get("target_session") or "")
    history = load_history(store_path(target, root=store_root)) if target else []
    history = [item for item in history if item.as_of.date().isoformat() == target]
    observed = history[-1] if history else None
    usable = [item for item in history if _confirmation_window(item.as_of.time())]
    if observed is not None and not _confirmation_window(observed.as_of.time()):
        latest, previous, prior = observed, None, None
    else:
        latest = usable[-1] if usable else None
        previous = usable[-2] if len(usable) >= 2 else None
        prior = usable[-3] if len(usable) >= 3 else None
    ready = bool(latest and _confirmation_window(latest.as_of.time()) and latest.tradable)
    return {
        "schema": SCHEMA,
        "target_session": target or None,
        "as_of": latest.as_of.isoformat(timespec="seconds") if latest else None,
        "snapshot_count": len(history),
        "data_status": _data_status(latest, ready),
        "results": [
            _evaluate(item, latest, previous, prior, ready)
            for item in forecast.get("forecasts") or []
        ],
        "notes": [
            "确认只使用盘中横截面，不改变盘前原始预测。",
            "概念与行业重叠时不累加成交额；独立行业确认数只统计行业层。",
        ],
    }


def _evaluate(
    forecast: dict[str, Any],
    latest: SectorSnapshot | None,
    previous: SectorSnapshot | None,
    prior: SectorSnapshot | None,
    ready: bool,
) -> dict[str, Any]:
    names = {str(name) for name in forecast.get("members") or []}
    matched = _matching(latest, names)
    current = _independent(matched)
    prev = _independent(_matching(previous, names))
    older = _independent(_matching(prior, names))
    breadth = _centre(current, "advance_ratio")
    excess = _centre(current, "excess_pct")
    amount = _centre(current, "amount_share")
    prev_amount = _centre(prev, "amount_share")
    excess_series = [_centre(rows, "excess_pct") for rows in (older, prev, current)]
    stability_observed = all(value is not None for value in excess_series)
    declining_twice = stability_observed and (
        excess_series[0] > excess_series[1] > excess_series[2]  # type: ignore[operator]
    )
    industries = sum(row.level == LEVEL_INDUSTRY and row.excess_pct > 0 for row in current)
    broad = breadth is not None and breadth >= 0.60
    outperforming = (
        excess is not None and excess > 0 and stability_observed and not declining_twice
    )
    # 一轮截面没有变化方向。未知不能冒充“成交关注没有下降”，否则第一轮只要
    # 广度与超额为正就会直接确认，绕过协议要求的成交额验证。
    attention = (
        amount is not None
        and prev_amount is not None
        and amount - prev_amount >= MIN_AMOUNT_SHARE_RISE
    )
    invalid = (breadth is not None and breadth < 0.45) or (
        excess is not None and excess < 0 and amount is not None
        and prev_amount is not None and amount > prev_amount
    )
    if not ready:
        status = "等待数据"
    elif not matched:
        status = "无法匹配"
    elif invalid:
        status = "已否决"
    elif broad and outperforming and attention and (
        industries >= 2 or bool(forecast.get("independent_signal"))
    ):
        status = "已确认"
    elif broad or outperforming:
        status = "部分确认"
    else:
        status = "未确认"
    return {
        "name": forecast.get("name"),
        "scenario": forecast.get("scenario"),
        "status": status,
        "matched_members": [row.name for row in matched],
        "evidence_members": [row.name for row in current],
        "overlap_ignored": [row.name for row in matched if row not in current],
        "breadth": breadth,
        "excess_pct": excess,
        "amount_share": amount,
        "amount_share_change": (
            round(amount - prev_amount, 6)
            if amount is not None and prev_amount is not None else None
        ),
        "independent_industries": industries,
        "checks": {
            "breadth_at_least_60pct": broad,
            "positive_stable_excess": outperforming,
            "attention_not_falling": attention,
            "stability_observed": stability_observed,
            "not_two_rounds_declining": not declining_twice,
        },
    }


def _matching(snapshot: SectorSnapshot | None, names: set[str]) -> list[SectorRead]:
    return [row for row in snapshot.reads if row.name in names] if snapshot else []


def _independent(rows: list[SectorRead]) -> list[SectorRead]:
    """Prefer the non-overlapping industry layer; concepts remain fallback evidence."""
    industries = [row for row in rows if row.level == LEVEL_INDUSTRY]
    return industries or rows


def _centre(rows: list[SectorRead], field: str) -> float | None:
    values = [float(getattr(row, field)) for row in rows]
    return round(median(values), 6) if values else None


def _data_status(latest: SectorSnapshot | None, ready: bool) -> str:
    if latest is None:
        return "没有当天盘中截面；先运行 monitor --once"
    if not latest.tradable:
        return "全市场成交额不足，截面不可用于确认"
    if MORNING_CLOSE < latest.as_of.time() < AFTERNOON_OPEN:
        return "午间休市，行情冻结；等待13:00后新截面"
    if not ready:
        return "09:35前噪声较大，等待确认窗口"
    return "可确认"


def _confirmation_window(moment: time) -> bool:
    return (
        time(9, 35) <= moment <= MORNING_CLOSE
        or AFTERNOON_OPEN <= moment <= AFTERNOON_CLOSE
    )
