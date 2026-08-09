"""确定性算子的统一契约。

叫 agent 是因为它们**自治、可并行、可单独失败、输出同构**——不是因为里面有模型。
系统里所有数字都只在这一层产生；LLM 那一层（`debate.py`）只能引用这里算出来的东西。

并行不是架构洁癖，是实测需求：分钟线每标的一次 HTTP，串行 1.0 秒/只、8 线程
0.19 秒/只；盘前主链路里舆情采集与成分股日线可以完全重叠。

单个 agent 失败只损失它那份数据并留下一条 note，不影响其余 agent，也不影响整轮。
这是「一个源挂了不能拖垮跑批」这条要求在代码里的落点。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

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
    """并行跑一批彼此独立的 agent，返回 {name: Finding}。

    这里跑的是**一波**互不依赖的 agent。需要上一波结果的融合逻辑（比如板块排序要
    先有舆情和动量）留在调用方，用普通代码写——为几个固定依赖搭一套 DAG 引擎是
    没人要求过的复杂度。
    """
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

    workers = min(max(1, max_workers), len(agents))
    if workers == 1:
        results = [execute(agent) for agent in agents]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(execute, agents))
    return {finding.agent: finding for finding in results}
