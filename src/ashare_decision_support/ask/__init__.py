"""单入口问答：省掉「先想清楚该敲哪个命令」这一步。

`chat` 已经能回答任何问题，但它要求你先知道 `/sectors` 装板块、`/positions` 装持仓、
`/alerts` 装告警、`/journal` 装台账。这是一份需要**先学会**的映射表，而它要解决的
问题（我想问一句）本来不该有前置条件。

`ask` 的做法是：**按时刻决定装什么**。

```text
开盘前   大盘方向 + 板块排序 + 盘前候选 + 持仓 + 隔夜舆情
盘中     上述 + 持仓与问到的票的实时买卖点 + 五档 + 当日告警
盘后     上述 + 当日兑现 + 台账统计
非交易时段  最近一次盘前/盘后的结果 + 台账统计（明确标注是隔夜的旧数据）
```

**它读的是自动化链路正在维护的那份状态**，不是自己重算一遍。你问的和守护进程
看到的是同一批数据——否则「系统刚才提醒我的那条是什么意思」这种问题会得到一个
用不同数据算出来的答案，那比不回答更糟。

只有盘中的实时价位是现算的，因为它按定义就没有「已经算好的那一份」。

不带问题直接跑，就是把装配好的上下文原样打印出来——**不经过模型**。没配 key
的时候这是全部功能，配了 key 的时候这是核对模型有没有胡说的办法。

## 深挖模式（`--deep`）

一次调用产出整个答案有两个后果：模型自己决定什么重要（而它没有依据可以决定），
以及数字闸门整段丢弃时**所有**小节一起消失。`--deep` 把它拆开：

```text
解析问题（标的 · 意图）→ 列研究计划 → 逐步研究（各自过闸门）→ 综合报告 → 价位轴
```

结论、排序、价位、触发与失效条件**全部由规则算**（`verdict.py`），模型只在每张卡
下面写一段论证。论证被丢弃时卡还在——能被丢弃的东西，本来就不该是结论。
"""

from ashare_decision_support.ask.context import (
    PHASE_INTRADAY,
    PHASE_OFF_HOURS,
    PHASE_POST_CLOSE,
    PHASE_PRE_OPEN,
    AskContext,
    assemble,
    current_phase,
    load_known_names,
    resolve_phase,
)
from ashare_decision_support.ask.plan import ResearchPlan, Step, build_plan
from ashare_decision_support.ask.question import QuestionSpec, parse_question
from ashare_decision_support.ask.report import render_report
from ashare_decision_support.ask.runner import run_ask
from ashare_decision_support.ask.verdict import Decision, assess, from_context, rank, summarise

__all__ = [
    "PHASE_INTRADAY",
    "PHASE_OFF_HOURS",
    "PHASE_POST_CLOSE",
    "PHASE_PRE_OPEN",
    "AskContext",
    "Decision",
    "QuestionSpec",
    "ResearchPlan",
    "Step",
    "assemble",
    "assess",
    "build_plan",
    "current_phase",
    "from_context",
    "load_known_names",
    "parse_question",
    "rank",
    "render_report",
    "resolve_phase",
    "run_ask",
    "summarise",
]
