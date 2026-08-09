"""买卖点：三层自主度的落点。

```text
engine.py       编排三层，对外仍是一个 StockAdvice
candidates.py   L2 规则生成带出处的候选集
adjudicate.py   L3 模型在候选集里取舍，或者弃权
```

见 `docs/ARCHITECTURE.md` §一。
"""

from ashare_decision_support.advice.adjudicate import (
    Adjudication,
    LevelAdjudicator,
    rule_default,
)
from ashare_decision_support.advice.candidates import (
    CandidateSet,
    PriceCandidate,
    build_candidates,
)
from ashare_decision_support.advice.engine import (
    AdviceEngine,
    Scenario,
    StockAdvice,
    risk_reward,
)
from ashare_decision_support.advice.external import (
    ExternalDecisionCheck,
    build_adjudication_packet,
    validate_external_decisions,
)

__all__ = [
    "Adjudication",
    "AdviceEngine",
    "CandidateSet",
    "ExternalDecisionCheck",
    "LevelAdjudicator",
    "PriceCandidate",
    "Scenario",
    "StockAdvice",
    "build_adjudication_packet",
    "build_candidates",
    "risk_reward",
    "rule_default",
    "validate_external_decisions",
]
