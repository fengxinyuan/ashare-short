"""盘中监控：时效闸门 · 跃迁触发 · 自适应降频.

```text
loop.py             何时轮询、失败如何降频、告警送到哪
session.py          一次轮询：分时结构 → 买卖点 → 触发器
feed.py             时效闸门——数据不新就停止产出信号
triggers.py         个股跃迁判定与去重策略
sector_session.py   一次板块截面：全市场扫描 → 按行业/概念归并 → 跃迁
sector_triggers.py  板块跃迁判定、噪声预算、同板块多触发器合并
```

个股与板块是两条并行的链路，共用 `loop.py` 的节奏与 `state/alerts.py` 的
跃迁机制，但**状态文件分开**——同一个路径上两个 `AlertState` 会互相覆盖。
"""

from ashare_decision_support.monitor.feed import FeedHealth, assess_feed
from ashare_decision_support.monitor.loop import LoopStats, run_monitor_loop
from ashare_decision_support.monitor.sector_session import (
    SECTOR_EVERY,
    SectorPassResult,
    SectorSession,
)
from ashare_decision_support.monitor.sector_triggers import (
    SectorAlert,
    SectorTriggerEngine,
    load_sector_state,
)
from ashare_decision_support.monitor.session import MonitorSession, PassResult
from ashare_decision_support.monitor.triggers import (
    DEFAULT_LEVELS,
    LEVEL_ACTION,
    LEVEL_INFO,
    LEVEL_WARN,
    POLICIES,
    Alert,
    TriggerEngine,
)

__all__ = [
    "DEFAULT_LEVELS",
    "LEVEL_ACTION",
    "LEVEL_INFO",
    "LEVEL_WARN",
    "POLICIES",
    "SECTOR_EVERY",
    "Alert",
    "FeedHealth",
    "LoopStats",
    "MonitorSession",
    "PassResult",
    "SectorAlert",
    "SectorPassResult",
    "SectorSession",
    "SectorTriggerEngine",
    "TriggerEngine",
    "assess_feed",
    "load_sector_state",
    "run_monitor_loop",
]
