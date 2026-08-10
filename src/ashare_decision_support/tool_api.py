"""Stable, read-mostly tool facade for Codex/Claude and other clients.

The facade deliberately exposes JSON-shaped results and keeps model calls out of the
project. MCP is one transport; the functions here are also usable from Python tests or
another local adapter without depending on an MCP package.
"""

from __future__ import annotations

from typing import Any

from ashare_decision_support.facts import build_facts_bundle
from ashare_decision_support.research.leaders import refresh_sector_leaders
from ashare_decision_support.research.monthly import build_monthly_context

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
) -> dict[str, Any]:
    bundle = build_facts_bundle(output_dir=output_dir, symbols=symbols, phase=phase)
    return bundle.to_dict()


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


def get_tool_catalog() -> dict[str, Any]:
    return {
        "schema": "ashare-tool-catalog-v1",
        "role": "deterministic evidence provider for external AI research",
        "tools": [
            "get_facts",
            "monthly_outlook_context",
            "refresh_sector_leaders",
        ],
        "constraints": [
            "read-only tools do not write orders or account state",
            "refresh_sector_leaders performs network reads and writes one JSON facts file",
            "the client owns open-ended web research and final interpretation",
        ],
    }
