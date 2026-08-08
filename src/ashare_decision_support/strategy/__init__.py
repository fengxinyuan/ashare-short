"""决策骨架：先决定**今天做不做、允许哪一类打法**，再决定选哪只票。

改造前这一层是不存在的。舆情、板块动量、威科夫、五档、龙虎榜、涨停池六路读数
全部汇进一个 0–100 分和一道「回调过没有」的门槛，代码里**没有任何一处在决定
今天该干什么**。后果是两个：

1. **情绪周期读出来了却不接管决策。** 2026-08-03 实测：炸板率 51.94%（系统自己
   的定义里 ≥50% 就是退潮），当天照常输出 60 条低吸建议，仓位系数 0.68。
2. **只有一套打法。** `jobs/morning.py` 的低吸门槛 + `advice/candidates.py` 里
   「高于现价的买入价直接不生成」，两处合起来让系统在结构上**不可能**产出一个
   突破买点。趋势只是权重（UPTREND +12 / DOWNTREND −15），不是分支——所以
   「下跌趋势什么样的能买、上涨趋势什么样的能买」这两个问题从来没被回答过。

现在拆成两个正交的判断：

```text
情绪周期（全市场）  →  今天允许哪些打法 + 仓位上限     regime.py
趋势状态（单只票）  →  这只票能用哪些打法             playbook.py
                    ↓
                取交集，空集就是不买
                    ↓
        对留下的打法逐条验判据链（每条打法的入场条件不同）
```

低吸不是默认打法，**它是退潮期的打法**。把一个特定周期的战术当成全天候策略，
正是选股结果长期对不上口径的根因。

`select.py` 是这两个判断的**唯一落地点**：三条链路（盘前扫描、盘后自选、个股问答）
共用它，差别只剩标的从哪来、施不施加周期约束。

`exits.py` 是 `playbook.py` 的对称面——那个回答「凭什么买」，它回答「什么时候走」。
量价类与环境类卖点在这里；盘中那五类（止损/目标/结构破坏/时间止损/浮盈回吐）留在
`monitor/holdings.py`，因为它们是分钟线口径，而这里全是日线口径。
"""

from ashare_decision_support.strategy.exits import (
    ExitAssessment,
    ExitSignal,
    assess_exits,
)
from ashare_decision_support.strategy.overnight import (
    PROFILES,
    StrategyCandidate,
    StrategyProfile,
    StrategyScreenConfig,
    StrategyScreenResult,
    diversified_shortlist,
    screen_overnight,
)
from ashare_decision_support.strategy.playbook import (
    PlaybookMatch,
    SetupVerdict,
    evaluate_playbooks,
    playbooks_for_trend,
)
from ashare_decision_support.strategy.positioning import (
    PositionSizingPlan,
    resolve_position_sizing,
)
from ashare_decision_support.strategy.regime import (
    RegimeVerdict,
    classify_regime,
)
from ashare_decision_support.strategy.select import (
    MIN_SETUP_CANDIDATES,
    SelectedSymbol,
    UniverseAnalysis,
    UniverseResult,
    analyze_universe,
    evaluate_universe,
    select_from,
)

__all__ = [
    "MIN_SETUP_CANDIDATES",
    "PROFILES",
    "ExitAssessment",
    "ExitSignal",
    "PlaybookMatch",
    "PositionSizingPlan",
    "RegimeVerdict",
    "SelectedSymbol",
    "SetupVerdict",
    "StrategyCandidate",
    "StrategyProfile",
    "StrategyScreenConfig",
    "StrategyScreenResult",
    "UniverseAnalysis",
    "UniverseResult",
    "analyze_universe",
    "assess_exits",
    "classify_regime",
    "diversified_shortlist",
    "evaluate_playbooks",
    "evaluate_universe",
    "playbooks_for_trend",
    "resolve_position_sizing",
    "screen_overnight",
    "select_from",
]
