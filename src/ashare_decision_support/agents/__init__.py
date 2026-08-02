"""agent 分两层，分界就是数字的产生位置。

```text
base.py        统一契约 Finding / AgentContext / run_agents（并行 + 失败隔离）
collectors.py  确定性算子：市场舆情 · 个股舆情 · 板块动量 · 个股日线 · 个股分时
debate.py      LLM 角色：多头举证 · 空头举证 · 裁判指出分歧点
```

`collectors.py` 里的 agent 不含模型，系统里**所有数字都在这一层产生**。
`debate.py` 里的角色只能引用这些数字，越界的整段丢弃。
"""

from ashare_decision_support.agents.base import (
    Agent,
    AgentContext,
    Finding,
    collect_notes,
    run_agents,
)
from ashare_decision_support.agents.collectors import (
    NewsAgent,
    SectorMetricsAgent,
    SymbolDailyAgent,
    SymbolIntradayAgent,
    SymbolNewsAgent,
)
from ashare_decision_support.agents.debate import Argument, DebatePanel, DebateResult

__all__ = [
    "Agent",
    "AgentContext",
    "Argument",
    "DebatePanel",
    "DebateResult",
    "Finding",
    "NewsAgent",
    "SectorMetricsAgent",
    "SymbolDailyAgent",
    "SymbolIntradayAgent",
    "SymbolNewsAgent",
    "collect_notes",
    "run_agents",
]
