"""盘中监控：时效闸门 · 跃迁触发 · 自适应降频.

```text
loop.py      何时轮询、失败如何降频、告警送到哪
session.py   一次轮询：分时结构 → 买卖点 → 触发器
feed.py      时效闸门——数据不新就停止产出信号
triggers.py  跃迁判定与去重策略
```
"""

from ashare_decision_support.monitor.feed import FeedHealth, assess_feed
from ashare_decision_support.monitor.loop import LoopStats, run_monitor_loop
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
    "Alert",
    "DEFAULT_LEVELS",
    "FeedHealth",
    "LEVEL_ACTION",
    "LEVEL_INFO",
    "LEVEL_WARN",
    "LoopStats",
    "MonitorSession",
    "POLICIES",
    "PassResult",
    "TriggerEngine",
    "assess_feed",
    "run_monitor_loop",
]
