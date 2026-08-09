"""统一事实包导出。

这个模块的作用很窄：把系统已经算好的上下文装成一份稳定的 bundle，交给外部分析者
（Codex / Claude / 其他交互窗口）继续阅读和推理。它不做任何模型调用。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.ask.context import assemble
from ashare_decision_support.schedule.timezone import beijing_now

FACTS_SCHEMA = "ashare-facts-v1"


@dataclass(frozen=True)
class FactsBundle:
    phase: str
    phase_label: str
    as_of: str
    symbols: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    rendered: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": FACTS_SCHEMA,
            "contract": {
                "purpose": "deterministic A-share facts for external analysis",
                "as_of_field": "as_of",
                "missing_values": "null or empty collection; never infer a value",
                "calculation_policy": "numbers are produced by deterministic rules; models may only explain or select",
            },
            "phase": self.phase,
            "phase_label": self.phase_label,
            "as_of": self.as_of,
            "symbols": list(self.symbols),
            "facts": dict(self.facts),
            "rendered": self.rendered,
            "notes": list(self.notes),
        }


def build_facts_bundle(
    *,
    moment: datetime | None = None,
    symbols: list[str] | None = None,
    output_dir: str | Path | None = None,
    compute_symbols: Callable[[list[str]], dict] | None = None,
    phase: str | None = None,
    is_trading_day: bool | None = None,
    focus: list[str] | None = None,
) -> FactsBundle:
    """按当前时段装一份统一事实包。"""
    ctx = assemble(
        moment=moment or beijing_now(),
        symbols=symbols,
        output_dir=output_dir,
        compute_symbols=compute_symbols,
        phase=phase,
        is_trading_day=is_trading_day,
        focus=focus,
    )
    return FactsBundle(
        phase=ctx.phase,
        phase_label=ctx.phase_label,
        as_of=ctx.as_of,
        symbols=list(ctx.symbols),
        facts=ctx.facts(),
        rendered=ctx.render(),
        notes=list(ctx.notes),
    )
