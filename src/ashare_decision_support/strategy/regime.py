"""情绪周期：今天允许哪些打法、仓位上限多少。

**判据只用涨停池。** 指数、宽度、舆情都可以在封板意愿崩塌的同一天是绿的——
2026-07-31 实测半导体 +4.23% 收在当日最低正是这种情况。「昨天封住的板今天还
封得住吗」只有炸板率回答，别的读数一个都看不到。

**切点不在这里定，在 `emotion_scale.py`。** 这一层出的是判定阶梯，尺子是全仓
共用的一把——同一组切点还被简报措辞（`adapters/limit_pool.py`）、打分斜坡
（`sectors/predict.py`）和台账分层（`journal/stats.py`）读着。各定一套是个隐蔽
的坑，那个坑的实际长相记在 `emotion_scale.py` 的模块注释里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ashare_decision_support.emotion_scale import (
    BLOWUP_FROZEN,
    BLOWUP_HEALTHY,
    BLOWUP_STRESSED,
    STREAK_CLIMAX,
    STREAK_MOMENTUM,
    STREAK_WEAK,
)
from ashare_decision_support.models.enums import EmotionCycle, Playbook
from ashare_decision_support.numeric import finite

# 涨停家数。太少说明没有赚钱效应，太多且伴随高连板是情绪透支。

# 这三个留在本模块：只有闸门读它们，不构成跨层的那把尺子。
LIMIT_UP_THIN = 15
LIMIT_UP_CROWDED = 80

# 跌停家数：恐慌的直接读数，单独一票否决。
LIMIT_DOWN_PANIC = 15

# 每个周期允许的打法与仓位上限。

# 低吸（PULLBACK）几乎每一档都在，但**它在退潮期是主力打法、在启动期只是备选**
# ——两者的区别由仓位上限和 `_LEADER_ONLY` 表达，不由是否允许表达。
_ALLOWED: dict[EmotionCycle, frozenset[Playbook]] = {
    # 冰点：空集。不是仓位压到很低，是**不出手**。
    EmotionCycle.FREEZING: frozenset(),
    EmotionCycle.LAUNCH: frozenset({Playbook.BREAKOUT, Playbook.PULLBACK}),
    EmotionCycle.MOMENTUM: frozenset({Playbook.BREAKOUT, Playbook.PULLBACK, Playbook.RANGE_LOW}),
    # 高潮：只减不加。留 PULLBACK 是因为强势股回踩仍是可执行的进场，但仓位砍半，
    # 且这一档的真正重点在卖点侧（`monitor/`），不在选股侧。
    EmotionCycle.CLIMAX: frozenset({Playbook.PULLBACK}),
    EmotionCycle.DIVERGENCE: frozenset({Playbook.PULLBACK, Playbook.RANGE_LOW}),
    # 退潮：低吸超跌反弹是这一档的打法，但只做龙头，且仓位上限最低。
    EmotionCycle.EBB: frozenset({Playbook.PULLBACK, Playbook.REVERSAL}),
    # 未知：**不等于中性。** 涨停池取不到时，把仓位压到和退潮同级，并且只留
    # 最保守的那条打法。给中性默认值会让「无知」压过「测量出的走弱」。
    EmotionCycle.UNKNOWN: frozenset({Playbook.PULLBACK}),
}

_POSITION_CAP: dict[EmotionCycle, float] = {
    EmotionCycle.FREEZING: 0.0,
    EmotionCycle.LAUNCH: 0.50,
    EmotionCycle.MOMENTUM: 0.80,
    EmotionCycle.CLIMAX: 0.40,
    EmotionCycle.DIVERGENCE: 0.50,
    EmotionCycle.EBB: 0.30,
    EmotionCycle.UNKNOWN: 0.30,
}

# 这些档位下只做板块龙头。表达在 verdict 上而不是硬过滤，因为「谁是龙头」要靠
# 板块内排序回答，那是 `jobs/morning.py` 的事，这一层只负责说出这个约束。
_LEADER_ONLY = frozenset({EmotionCycle.EBB, EmotionCycle.CLIMAX})


@dataclass(frozen=True)
class RegimeVerdict:
    """今天的情绪周期，以及它对选股的三条约束。"""

    cycle: EmotionCycle
    allowed: frozenset[Playbook]
    position_cap: float
    leader_only: bool = False
    trusted: bool = True
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def halted(self) -> bool:
        """今天一条打法都不允许——不出手。"""
        return not self.allowed

    def to_dict(self) -> dict:
        return {
            "cycle": self.cycle.value,
            "allowed": sorted(item.value for item in self.allowed),
            "position_cap": self.position_cap,
            "leader_only": self.leader_only,
            "trusted": self.trusted,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


def classify_regime(
    blowup_rate: float | None,
    max_streak: int | None = None,
    limit_up_count: int | None = None,
    limit_down_count: int | None = None,
) -> RegimeVerdict:
    """从涨停池读数判定情绪周期。

    缺失或无效的读数保留未知，不能用零补齐。已验证的恐慌指标仍可独立停止
    新开仓；其余周期要求完整读数，避免缺少跌停统计时给出高仓位。
    """
    reasons: list[str] = []
    warnings: list[str] = []

    blowup_rate = finite(blowup_rate)
    if blowup_rate is not None and not 0.0 <= blowup_rate <= 1.0:
        blowup_rate = None
    streak = _count(max_streak)
    ups = _count(limit_up_count)
    downs = _count(limit_down_count)
    missing = [
        label
        for label, value in (
            ("炸板率", blowup_rate),
            ("最高连板", streak),
            ("涨停家数", ups),
            ("跌停家数", downs),
        )
        if value is None
    ]
    if missing:
        warnings.append(f"情绪读数缺失或无效：{'、'.join(missing)}；缺失未按零计入")

    panic = _panic_reason(blowup_rate, streak, ups, downs)
    if panic is not None:
        reasons.append(panic)
        return _verdict(EmotionCycle.FREEZING, reasons, warnings)

    if missing:
        warnings.append(
            "涨停池读数不完整，情绪周期未知。已按退潮档压仓位并只保留回踩打法——未知不等于中性"
        )
        return _verdict(EmotionCycle.UNKNOWN, reasons, warnings, trusted=False)

    if blowup_rate >= BLOWUP_STRESSED:
        reasons.append(
            f"炸板率 {blowup_rate:.1%}（≥{BLOWUP_STRESSED:.0%}），"
            f"冲板的票过半没守住；最高连板 {streak} 板"
        )
        return _verdict(EmotionCycle.EBB, reasons, warnings)

    if blowup_rate < BLOWUP_HEALTHY:
        if streak >= STREAK_CLIMAX and ups >= LIMIT_UP_CROWDED:
            reasons.append(
                f"炸板率 {blowup_rate:.1%} 偏低但最高连板已达 {streak} 板、"
                f"涨停 {ups} 家（≥{LIMIT_UP_CROWDED}），情绪透支"
            )
            return _verdict(EmotionCycle.CLIMAX, reasons, warnings)
        if streak >= STREAK_MOMENTUM:
            reasons.append(
                f"炸板率 {blowup_rate:.1%}（<{BLOWUP_HEALTHY:.0%}）封板结实，"
                f"最高连板 {streak} 板，有资金接力"
            )
            return _verdict(EmotionCycle.MOMENTUM, reasons, warnings)
        reasons.append(
            f"炸板率 {blowup_rate:.1%}（<{BLOWUP_HEALTHY:.0%}）封板结实，"
            f"但最高连板仅 {streak} 板，主线尚未成型"
        )
        return _verdict(EmotionCycle.LAUNCH, reasons, warnings)

    reasons.append(
        f"炸板率 {blowup_rate:.1%} 落在 {BLOWUP_HEALTHY:.0%}–{BLOWUP_STRESSED:.0%} 之间，"
        f"最高连板 {streak} 板，多空分歧"
    )
    return _verdict(EmotionCycle.DIVERGENCE, reasons, warnings)


def _count(value: object) -> int | None:
    number = finite(value)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None


def _panic_reason(
    rate: float | None, streak: int | None, ups: int | None, downs: int | None
) -> str | None:
    """只让已测得的风险触发冰点，不让其他字段的缺失盖过已知恐慌。"""
    if downs is not None and downs >= LIMIT_DOWN_PANIC:
        return f"跌停 {downs} 家（≥{LIMIT_DOWN_PANIC}），恐慌盘"
    if rate is not None and rate >= BLOWUP_FROZEN and streak is not None and streak <= STREAK_WEAK:
        return (
            f"炸板率 {rate:.1%}（≥{BLOWUP_FROZEN:.0%}）且最高连板仅 {streak} 板，"
            "封板意愿与接力资金同时缺席"
        )
    if ups is not None and ups <= LIMIT_UP_THIN:
        return f"涨停仅 {ups} 家（≤{LIMIT_UP_THIN}），没有赚钱效应"
    return None


def regime_from_limit_pool(limit_pool: object | None) -> RegimeVerdict:
    """从 `adapters/limit_pool.py` 的快照直接判定。

    快照缺失或自报 `ok=False` 时走 UNKNOWN 分支——半份数据比没有数据更危险，
    因为它看起来像可以用。
    """
    if limit_pool is None or not getattr(limit_pool, "ok", False):
        return classify_regime(None)
    return classify_regime(
        blowup_rate=getattr(limit_pool, "blowup_rate", None),
        max_streak=getattr(limit_pool, "max_streak", None),
        limit_up_count=getattr(limit_pool, "limit_up_count", None),
        limit_down_count=getattr(limit_pool, "limit_down_count", None),
    )


def _verdict(
    cycle: EmotionCycle,
    reasons: list[str],
    warnings: list[str],
    trusted: bool = True,
) -> RegimeVerdict:
    allowed = _ALLOWED[cycle]
    leader_only = cycle in _LEADER_ONLY
    if not allowed:
        warnings.append(f"情绪周期 {cycle.value}：今日不产出任何买入候选")
    elif leader_only:
        warnings.append(f"情绪周期 {cycle.value}：只做板块龙头，非龙头不进候选")
    return RegimeVerdict(
        cycle=cycle,
        allowed=allowed,
        position_cap=_POSITION_CAP[cycle],
        leader_only=leader_only,
        trusted=trusted,
        reasons=reasons,
        warnings=warnings,
    )
