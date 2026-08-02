"""标的适配性筛选：把「哪些票适合超短线」拆成可执行、可证伪的读数。

和 `signals/` 的分工：`signals/` 回答「这只票**今天**是什么状态」，本模块回答
「这只票**这类打法**上历史是否稳定可重复」。前者是当日读数，后者是标的画像。

入口四个：`screen_symbol()` 单票、`screen_many()` 批量、`scan_limit_rules()` 限价单
参数扫描（都是纯函数），`run_screen()` 带取数与报告。
"""

from ashare_decision_support.screening.limit_order import (
    LimitResult,
    LimitRule,
    LimitScan,
    scan_limit_rules,
    simulate,
)
from ashare_decision_support.screening.runner import ScreenReport, run_screen
from ashare_decision_support.screening.suitability import (
    Behavior,
    Capture,
    Consistency,
    CostModel,
    RoundTrip,
    ScreenConfig,
    SuitabilityCard,
    Tradability,
    screen_many,
    screen_symbol,
)

__all__ = [
    "Behavior",
    "Capture",
    "Consistency",
    "CostModel",
    "LimitResult",
    "LimitRule",
    "LimitScan",
    "RoundTrip",
    "ScreenConfig",
    "ScreenReport",
    "SuitabilityCard",
    "Tradability",
    "run_screen",
    "scan_limit_rules",
    "screen_many",
    "screen_symbol",
    "simulate",
]
