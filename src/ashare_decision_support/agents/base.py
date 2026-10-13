"""确定性采集器的共享输入、执行与结果契约。

独立采集器并行执行，单路异常记录为失败的 Finding，不中断其他采集器。
需要前序结果的融合步骤由调用方编排。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from ashare_decision_support.fanout import gather

DEFAULT_WORKERS = 6


@dataclass(frozen=True)
class Finding:
    """一个 agent 的产出。失败也是一种产出，不是异常。"""

    agent: str
    ok: bool
    data: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


@dataclass
class AgentContext:
    """一轮分析共享的输入。agent 只读，不写。"""

    trade_date: date
    adapter: Any = None
    intraday_adapter: Any = None
    news_source: Any = None
    symbols: list[str] = field(default_factory=list)
    period: str = "5"
    extras: dict = field(default_factory=dict)


class Agent(Protocol):
    name: str

    def run(self, ctx: AgentContext) -> tuple[dict, list[str]]:
        """返回 (data, notes)。抛异常由 `run_agents` 捕获成 ok=False 的 Finding。"""


def run_agents(
    agents: list[Agent],
    ctx: AgentContext,
    max_workers: int = DEFAULT_WORKERS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Finding]:
    """并行执行一批互不依赖的采集器，按名称返回结果与耗时。"""
    if not agents:
        return {}

    def execute(agent: Agent) -> Finding:
        started = clock()
        try:
            data, notes = agent.run(ctx)
            return Finding(
                agent=agent.name,
                ok=True,
                data=data or {},
                notes=list(notes or []),
                elapsed_ms=int((clock() - started) * 1000),
            )
        except Exception as exc:
            return Finding(
                agent=agent.name,
                ok=False,
                notes=[f"{agent.name} 失败：{type(exc).__name__}: {exc}"],
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=int((clock() - started) * 1000),
            )

    # `gather` 的失败列表在这里恒为空，这是**故意**的：agent 的失败不是「少一路」，
    # 是一份 `ok=False` 的 Finding——带着错误类名和它跑了多久，而耗时只有在
    # `execute` 内部才量得到。所以异常在 `execute` 里就落成了产出，不往上抛。
    done, _ = gather(agents, execute, max_workers=max_workers)
    return {finding.agent: finding for _agent, finding in done}


def timing_note(findings: dict[str, Finding]) -> str | None:
    """按耗时降序列出本批采集器；没有可用耗时时返回 None。"""
    timed = sorted(
        (finding for finding in findings.values() if finding.elapsed_ms > 0),
        key=lambda finding: finding.elapsed_ms,
        reverse=True,
    )
    if not timed:
        return None
    parts = "、".join(f"{finding.agent} {finding.elapsed_ms / 1000:.1f}s" for finding in timed)
    return f"本波并行取数耗时（最慢的决定整轮）：{parts}"
