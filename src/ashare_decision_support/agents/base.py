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

    # `gather` 的失败列表在这里恒为空，这是**故意**的：agent 的失败不是「少一路」，
    # 是一份 `ok=False` 的 Finding——带着错误类名和它跑了多久，而耗时只有在
    # `execute` 内部才量得到。所以异常在 `execute` 里就落成了产出，不往上抛。
    done, _ = gather(agents, execute, max_workers=max_workers)
    return {finding.agent: finding for _agent, finding in done}


def timing_note(findings: dict[str, Finding]) -> str | None:
    """一句话说清这一波里哪条腿慢。取不到就返回 None。

    `elapsed_ms` 此前**量了但从没被带出来过**：每个 Finding 都记着自己跑了多久，
    而调用方只 `extend(finding.notes)`，于是「盘前哪一段慢」这个问题在生产环境里
    没有答案——只能靠临时加计时器重跑一遍。

    不设阈值、不判「慢」：并行跑批里最慢那条腿决定整轮墙钟时间，把耗时按降序原样
    列出来就够了，哪个数值算异常由读的人定。
    """
    timed = sorted(
        (finding for finding in findings.values() if finding.elapsed_ms > 0),
        key=lambda finding: finding.elapsed_ms,
        reverse=True,
    )
    if not timed:
        return None
    parts = "、".join(f"{finding.agent} {finding.elapsed_ms / 1000:.1f}s" for finding in timed)
    return f"本波并行取数耗时（最慢的决定整轮）：{parts}"
