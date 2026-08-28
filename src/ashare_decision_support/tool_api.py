"""Stable, read-mostly tool facade for Codex/Claude and other clients.

The facade deliberately exposes JSON-shaped results and keeps model calls out of the
project. MCP is one transport; the functions here are also usable from Python tests or
another local adapter without depending on an MCP package.
"""

from __future__ import annotations

from typing import Any

from ashare_decision_support.adapters.ths_sector_index import load_all_series
from ashare_decision_support.facts import build_facts_bundle
from ashare_decision_support.research.daily_sector import (
    compact_daily_sector_analysis,
    load_daily_sector_analysis,
)
from ashare_decision_support.research.leaders import refresh_sector_leaders
from ashare_decision_support.research.monthly import build_monthly_context
from ashare_decision_support.research.preopen import build_preopen_research
from ashare_decision_support.research.sector_confirmation import build_sector_confirmation
from ashare_decision_support.research.sector_forecast import build_sector_forecast
from ashare_decision_support.sectors.backtest import backtest_price_baselines

TOOL_INSTRUCTIONS = (
    "This server returns deterministic A-share facts only. It never places orders and does not "
    "predict returns. Use monthly_outlook_context for a research brief; the external client "
    "must collect and cite current world events, verify dates, and separate facts from hypotheses."
)


def get_facts(
    *,
    output_dir: str = "briefs",
    symbols: list[str] | None = None,
    phase: str | None = None,
    sections: list[str] | None = None,
) -> dict[str, Any]:
    """当前时段的事实包。`sections` 只取其中几块，口径与 `ashare-ds facts --sections`
    完全一致——过滤逻辑在 `build_facts_bundle` 里，这一层只是转发。"""
    bundle = build_facts_bundle(
        output_dir=output_dir, symbols=symbols, phase=phase, sections=sections
    )
    return bundle.to_dict(compact_rendered=True)


def monthly_outlook_context(
    *,
    output_dir: str = "briefs",
    horizon_days: int = 31,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    return build_monthly_context(
        output_dir=output_dir,
        horizon_days=horizon_days,
        symbols=symbols,
    )


def refresh_sector_leaders_tool(
    *,
    output_dir: str = "briefs",
    levels: list[str] | None = None,
    top_per_sector: int = 3,
    lookback_days: int = 180,
) -> dict[str, Any]:
    return refresh_sector_leaders(
        output_dir=output_dir,
        levels=tuple(levels or ("industry", "concept")),
        top_per_sector=top_per_sector,
        lookback_days=lookback_days,
    )


def daily_sector_analysis(
    *,
    output_dir: str = "briefs",
    detail: str = "summary",
) -> dict[str, Any]:
    """读取最近一次盘前板块分析，不联网、不重复取数。"""
    if detail not in {"summary", "full"}:
        raise ValueError("detail must be 'summary' or 'full'")
    payload = load_daily_sector_analysis(output_dir)
    return payload if detail == "full" else compact_daily_sector_analysis(payload)


def sector_baseline_backtest(*, top: int = 5) -> dict[str, Any]:
    """只读本地板块指数历史，评价固定价格基线，不联网。"""
    return backtest_price_baselines(load_all_series(), top=top)


def sector_forecast(*, output_dir: str = "briefs") -> dict[str, Any]:
    """最新盘前板块情景与盘中确认/失效协议。"""
    return build_sector_forecast(output_dir)


def sector_confirmation(*, output_dir: str = "briefs") -> dict[str, Any]:
    """用已落盘盘中板块截面确认或否决最新情景，不联网。"""
    return build_sector_confirmation(output_dir)


def preopen_research_context(
    *,
    output_dir: str = "briefs",
    include_global: bool = True,
) -> dict[str, Any]:
    """生成给 Codex 的盘前研究包，包含外盘快照和 A 股已落盘事实。"""
    from ashare_decision_support.adapters.global_market import GlobalMarketAdapter

    return build_preopen_research(
        output_dir=output_dir,
        market_adapter=GlobalMarketAdapter() if include_global else None,
    )
