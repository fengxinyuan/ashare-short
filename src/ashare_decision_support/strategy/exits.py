"""卖出判据：什么时候该走。与 `playbook.py` 对称——那个回答「凭什么买」。

`monitor/holdings.py` 已经有五类卖点（止损 / 目标 / 结构破坏 / 时间止损 / 浮盈
回吐），质量很高，但**没有一条是量价触发，也没有一条是环境触发**。这个模块补的
就是那两类。

## 为什么不放进盘中监控

盘中告警走的是分钟线。这里的判据全部算自**日线**——而盘中拿到的日线最后一根通常
还是昨天那根（当日那根要收盘才完整）。把日线口径的「天量滞涨」当成盘中告警发出去，
说的是昨天的事，却带着此刻的时间戳，那比不报更糟。

所以它们的位置在**日线是新鲜的那几个时刻**：

```text
post_close   收盘后，当日日线已完整 → 持仓的卖出判据在这里说
advise       你主动问一只票 → 连同买入判据一起说
morning      盘前 → 昨日收盘的判据，用来定今天的处置计划
```

盘中那五类保持不动。它们本来就是分钟线口径的，是对的。

## 环境类为什么单列一条

「今天行情很差」必须能直接对**已有持仓**说话。改造前 `strategy/regime.py` 只在
盘前选股时生效——情绪周期从发酵切到退潮，选股侧立刻收紧，而持仓侧一句话都没有。
仓位上限管的是「今天能买多少」，管不了「手上这些该不该减」。

**它是提示降仓，不是清仓信号**，所以永远带上判定当时的三个原始读数（炸板率、
最高连板、跌停家数），让人能自己复核而不是只看到一个标签。

## 阈值

同 `playbook.py`：先验设定，不是标定出来的。作用是让这些话第一次有确定的判据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.models.enums import EmotionCycle
from ashare_decision_support.signals.volume_price import (
    CLIMAX_STALL_TEXT,
    DIVERGENCE_TOP,
    QUADRANT_DOWN_EXPAND,
    TOP_DIVERGENCE_TEXT,
)

# 顶背离只对**已有浮盈**的持仓构成减仓理由。没有浮盈时价格根本没创新高，
# 这条判据也不会成立——留着这个常量是为了让「减仓」和「清仓」的分界写在代码里。
EXIT_TRIM = "trim"
EXIT_EXIT = "exit"
EXIT_WATCH = "watch"

# 环境降档：这两档意味着赚钱效应正在消失或已经消失。
_DERISK_CYCLES = frozenset({EmotionCycle.EBB, EmotionCycle.FREEZING})


@dataclass(frozen=True)
class ExitSignal:
    """一条卖出判据，以及它要求的动作强度。"""

    trigger: str
    action: str  # trim | exit | watch
    reason: str
    detail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "action": self.action,
            "reason": self.reason,
            "detail": list(self.detail),
        }

    def describe(self) -> str:
        label = {EXIT_EXIT: "离场", EXIT_TRIM: "减仓", EXIT_WATCH: "观察"}.get(
            self.action, self.action
        )
        return f"【{label}】{self.reason}"


@dataclass(frozen=True)
class ExitAssessment:
    """一只票在收盘口径下的全部卖出判据。"""

    signals: list[ExitSignal] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """有没有任何一条卖出判据成立。"""
        return bool(self.signals)

    @property
    def strongest(self) -> str | None:
        """最强的那个动作。离场 > 减仓 > 观察。"""
        for action in (EXIT_EXIT, EXIT_TRIM, EXIT_WATCH):
            if any(item.action == action for item in self.signals):
                return action
        return None

    @property
    def reasons(self) -> list[str]:
        return [item.describe() for item in self.signals]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.strongest,
            "signals": [item.to_dict() for item in self.signals],
        }


def assess_exits(
    volume_price: Any = None,
    snapshot: Any = None,
    regime: Any = None,
    has_unrealised_gain: bool | None = None,
) -> ExitAssessment:
    """收盘口径的卖出判据。

    每一项缺数据就跳过，不判负——「没测到」不该变成一条卖出理由，那和让数据缺失
    冒充风险判断是同一个错（同 `jobs/sources.py:fetch_market_scan` 的口径）。

    `has_unrealised_gain` 只影响顶背离那一条：它是**兑现**判据，对还在亏损的仓位
    说「减仓落袋」没有意义。传 None 表示不知道是否持仓，此时按「可能持有」处理并
    把不确定写进 detail。
    """
    signals: list[ExitSignal] = []

    signals.extend(_volume_price_exits(volume_price, snapshot, has_unrealised_gain))
    environment = _environment_exit(regime)
    if environment is not None:
        signals.append(environment)

    return ExitAssessment(signals=signals)


def _volume_price_exits(
    vp: Any, snapshot: Any, has_gain: bool | None
) -> list[ExitSignal]:
    signals: list[ExitSignal] = []
    if vp is None:
        return signals

    # ---- 天量滞涨 --------------------------------------------------------
    # 五类卖点没有一条会响：止损远在下方、目标没到、支撑没破、期限没到、浮盈
    # 还在。而买盘已经在这一天被吃完了。
    if getattr(vp, "climax_stall", False):
        ratio = getattr(vp, "vol_ratio_20d", None)
        detail = ["放巨量却收在当日区间下半段，说明买盘在这个位置被卖盘吃完"]
        if ratio is not None:
            detail.append(f"量比 {ratio:.2f}")
        signals.append(
            ExitSignal(
                trigger="climax_stall",
                action=EXIT_TRIM,
                reason=CLIMAX_STALL_TEXT,
                detail=detail,
            )
        )

    # ---- 放量跌破 MA20 ---------------------------------------------------
    # 「缩量回踩不破 MA20」是买点，这是它的镜像。同一条均线，放着量跌穿。
    if getattr(vp, "breakdown_on_volume", False):
        ma20 = getattr(snapshot, "ma20", None)
        detail = ["与「缩量回踩不破 MA20」正相反：放着量跌穿，抛压是真实的"]
        if ma20 is not None:
            detail.append(f"MA20 {ma20:.2f}")
        signals.append(
            ExitSignal(
                trigger="breakdown_on_volume",
                action=EXIT_EXIT,
                reason=f"{QUADRANT_DOWN_EXPAND}且收盘跌破 MA20",
                detail=detail,
            )
        )

    # ---- 顶背离 ----------------------------------------------------------
    # **减仓不清仓**：价还在创新高，只是推动力在衰减。把它当清仓信号会在主升段
    # 中途下车，而那正是这套系统最该避免的一类错。
    # 没有浮盈就没有可兑现的东西，这条不适用。
    if getattr(vp, "divergence", None) == DIVERGENCE_TOP and has_gain is not False:
        detail = ["价创 20 日新高，但量能低于创前高那一天——推动力在衰减"]
        if has_gain is None:
            detail.append("未知是否持仓：若已持有，这是兑现判据而非离场判据")
        signals.append(
            ExitSignal(
                trigger="top_divergence",
                action=EXIT_TRIM,
                reason=TOP_DIVERGENCE_TEXT,
                detail=detail,
            )
        )

    return signals


def _environment_exit(regime: Any) -> ExitSignal | None:
    """情绪周期降档。**对已有持仓说话**，不是选股约束。

    带上判定当时的三个原始读数——标签是结论，读数才是能被复核的东西。
    """
    if regime is None:
        return None
    cycle = getattr(regime, "cycle", None)
    if cycle not in _DERISK_CYCLES:
        return None

    action = EXIT_EXIT if cycle is EmotionCycle.FREEZING else EXIT_TRIM
    detail = list(getattr(regime, "reasons", []) or [])
    if getattr(regime, "trusted", True) is False:
        detail.append("涨停池不可用，该判定按保守档给出，可信度低")
    cap = getattr(regime, "position_cap", None)
    if cap is not None:
        detail.append(f"该档位仓位上限 {cap:.2f}")
    detail.append("这是降仓提示，不是清仓信号——先按上限对齐总仓位，再逐只看个股判据")

    return ExitSignal(
        trigger="regime_derisk",
        action=action,
        reason=f"情绪周期 {getattr(cycle, 'value', cycle)}：赚钱效应正在消失",
        detail=detail,
    )
