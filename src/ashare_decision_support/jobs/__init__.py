"""按时间表自动运行的任务，以及它们的手工入口。

```text
daily_cycle  全天时间表编排（`daemon` 命令）
morning      盘前主链路：舆情 → 大盘 → 板块 → 选股 → 候选池 → 台账
monitor      盘中循环：时效闸门 → 跃迁触发 → 告警
advise       个股买卖点与情景推演
post_close   盘后：自选与持仓打分，产出次日执行卡
pre_open     把执行卡复述一遍（仅 CLI；守护进程 09:15 跑的是 advise）
review       兑现核对与统计
```

`morning` / `post_close` / `advise` 三条链路的选股段共用
`strategy/select.py`——差别只在标的从哪来、施不施加情绪周期约束。
"""

from ashare_decision_support.jobs.advise import run_advise
from ashare_decision_support.jobs.daily_cycle import run_daily_cycle
from ashare_decision_support.jobs.monitor import run_monitor
from ashare_decision_support.jobs.morning import MorningConfig, Sources, run_morning
from ashare_decision_support.jobs.post_close import run_post_close
from ashare_decision_support.jobs.pre_open import run_pre_open
from ashare_decision_support.jobs.review import run_review

__all__ = [
    "MorningConfig",
    "Sources",
    "run_advise",
    "run_daily_cycle",
    "run_monitor",
    "run_morning",
    "run_post_close",
    "run_pre_open",
    "run_review",
]
