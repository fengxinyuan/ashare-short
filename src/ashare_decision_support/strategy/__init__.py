"""策略规则与筛选入口。

`regime.py` 给出市场允许的打法和仓位上限，`playbook.py` 按个股趋势检查入场条件。
`select.py` 供盘前、盘后和个股分析共用；独立参数化筛选在 `overnight.py`。
日线退出依据在 `exits.py`，分钟线持仓风险在 `monitor/holdings.py`。
计算细节见 `docs/PIPELINE.md`。
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
