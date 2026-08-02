"""按时间表自动运行的任务，以及它们的手工入口。

```text
daily_cycle  全天时间表编排（`daemon` 命令）
morning      盘前主链路：舆情 → 大盘 → 板块 → 选股 → 候选池 → 台账
monitor      盘中循环：时效闸门 → 跃迁触发 → 告警
advise       个股买卖点与情景推演
post_close   盘后打分与次日矩阵
pre_open     把矩阵复述成执行卡
review       兑现核对与统计
```
"""

from ashare_decision_support.jobs.advise import run_advise
from ashare_decision_support.jobs.daily_cycle import run_daily_cycle
from ashare_decision_support.jobs.monitor import run_monitor
from ashare_decision_support.jobs.morning import run_morning
from ashare_decision_support.jobs.post_close import run_post_close
from ashare_decision_support.jobs.pre_open import run_pre_open
from ashare_decision_support.jobs.review import run_review

__all__ = [
    "run_advise",
    "run_daily_cycle",
    "run_monitor",
    "run_morning",
    "run_post_close",
    "run_pre_open",
    "run_review",
]
