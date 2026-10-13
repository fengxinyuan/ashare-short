"""连板梯队晋级与情绪退潮周期量化（Limit-up Ladder & Emotion Cycle）。

基于涨停池快照，构建超短梯队演进与情绪周期量化模型：
1. 连板梯队分布（首板、二板、三板、四板、高标妖股）；
2. 各层级晋级率（首板晋级率、二进三晋级率）与中位断层诊断；
3. 情绪四阶段量化划分（启动、发酵、高潮、退潮）与操作指引。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ashare_decision_support.adapters.limit_pool import LimitPoolSnapshot, LimitStock
from ashare_decision_support.emotion_scale import (
    BLOWUP_HEALTHY,
    BLOWUP_STRESSED,
    STREAK_CLIMAX,
    STREAK_MOMENTUM,
    STREAK_WEAK,
)


@dataclass(frozen=True)
class LadderTier:
    """单个梯队高度的标的聚集。"""

    streak: int
    count: int
    symbols: tuple[str, ...]
    names: tuple[str, ...]


@dataclass(frozen=True)
class ProgressionRate:
    """梯队层级晋级数据。"""

    from_streak: int
    to_streak: int
    yesterday_count: int
    today_count: int
    rate: float  # 0.0 ~ 1.0


@dataclass(frozen=True)
class LadderCycleReport:
    """连板梯队结构与情绪退潮周期综合量化报告。"""

    max_streak: int
    limit_up_count: int
    blowup_rate: float | None
    limit_down_count: int
    tiers: list[LadderTier]
    progressions: list[ProgressionRate]
    has_fault_line: bool  # 是否存在中位断层
    cycle_stage: str  # 启动期 / 发酵期 / 高潮期 / 退潮期 / 混沌震荡
    emotion_score: float  # 情绪温度打分 0 ~ 100
    action_advice: str  # 仓位与战术建议
    notes: list[str] = field(default_factory=list)


def _build_tiers(
    limit_up_stocks: list[LimitStock],
) -> tuple[dict[int, list[LimitStock]], list[LadderTier]]:
    tier_map: dict[int, list[LimitStock]] = {}
    for item in limit_up_stocks:
        s = max(1, item.streak)
        tier_map.setdefault(s, []).append(item)

    tiers: list[LadderTier] = []
    for s in sorted(tier_map.keys()):
        group = tier_map[s]
        tiers.append(
            LadderTier(
                streak=s,
                count=len(group),
                symbols=tuple(x.symbol for x in group),
                names=tuple(x.name for x in group),
            )
        )
    return tier_map, tiers


def _has_fault_line(max_streak: int, tier_map: dict[int, list[LimitStock]]) -> bool:
    if max_streak < 4:
        return False
    return any(mid not in tier_map or len(tier_map[mid]) == 0 for mid in range(2, max_streak))


def _calculate_progressions(
    yesterday: LimitPoolSnapshot | None,
    tier_map: dict[int, list[LimitStock]],
) -> tuple[list[ProgressionRate], float | None]:
    if yesterday is None or not yesterday.ok:
        return [], None
    progressions: list[ProgressionRate] = []
    first_to_second: float | None = None
    for y_streak, y_cnt in yesterday.streak_distribution.items():
        if y_cnt > 0:
            t_cnt = len(tier_map.get(y_streak + 1, []))
            rate = round(t_cnt / y_cnt, 4)
            progressions.append(
                ProgressionRate(
                    from_streak=y_streak,
                    to_streak=y_streak + 1,
                    yesterday_count=y_cnt,
                    today_count=t_cnt,
                    rate=rate,
                )
            )
            if y_streak == 1:
                first_to_second = rate
    return progressions, first_to_second


def _compute_emotion_score(
    limit_up_cnt: int,
    max_streak: int,
    blowup_rate: float | None,
    limit_down_cnt: int,
    fault_line: bool,
) -> float:
    score = 50.0
    if limit_up_cnt >= 60:
        score += 15.0
    elif limit_up_cnt <= 20:
        score -= 15.0

    if max_streak >= STREAK_CLIMAX:
        score += 15.0
    elif max_streak >= STREAK_MOMENTUM:
        score += 8.0
    elif max_streak <= STREAK_WEAK:
        score -= 10.0

    if blowup_rate is not None:
        if blowup_rate <= BLOWUP_HEALTHY:
            score += 10.0
        elif blowup_rate >= BLOWUP_STRESSED:
            score -= 20.0

    if limit_down_cnt >= 8:
        score -= 25.0
    elif limit_down_cnt >= 3:
        score -= 10.0
    elif limit_down_cnt == 0:
        score += 5.0

    if fault_line:
        score -= 8.0

    return max(5.0, min(95.0, score))


def _determine_stage_and_advice(
    limit_down_cnt: int,
    blowup_rate: float | None,
    max_streak: int,
    fault_line: bool,
) -> tuple[str, str, str]:
    if limit_down_cnt >= 5 or (blowup_rate is not None and blowup_rate >= 0.45):
        return (
            "退潮期",
            "防守为先，严控仓位；坚决规避中位接力与高位分歧股，防范极端核按钮",
            f"亏钱效应扩大（跌停 {limit_down_cnt} 只，炸板率 {blowup_rate or 0:.1%}），进入退潮防守",
        )
    if max_streak >= STREAK_CLIMAX and (blowup_rate or 0) >= BLOWUP_HEALTHY:
        return (
            "高潮分歧期",
            "减仓兑现浮盈；高标筹码松动，去弱留强，切勿盲目追高一字与后排跟风",
            f"连板高度达 {max_streak} 板出现高潮钝化迹象，炸板率开始抬升",
        )
    if max_streak >= 3 and not fault_line and (blowup_rate or 0) <= BLOWUP_HEALTHY:
        return (
            "发酵期",
            "积极进攻核心题材龙头；梯队结构健康，顺势参与主流 1 进 2 与分歧转一致",
            f"梯队完整（最高 {max_streak} 板），封板质量扎实，处于题材赚钱效应发酵段",
        )
    if max_streak <= 3 and (blowup_rate or 0) <= BLOWUP_HEALTHY and limit_down_cnt <= 2:
        return (
            "启动试错期",
            "轻仓试错新主线首板与前排核心，观察空间板破局标的",
            "市场恐慌缓和，涨停开始复苏，尝试新题材首板建仓",
        )
    return (
        "混沌震荡期",
        "控制仓位在半仓以下；快进快出，聚焦辨识度个股的低吸与套利",
        "市场主线不明朗，轮动速度加快，梯队缺乏合力",
    )


def analyze_ladder_and_cycle(
    today: LimitPoolSnapshot,
    yesterday: LimitPoolSnapshot | None = None,
) -> LadderCycleReport:
    """基于今日（及昨日）涨停池快照，输出完整的连板梯队与周期量化结论。"""
    tier_map, tiers = _build_tiers(today.limit_up)
    has_fault = _has_fault_line(today.max_streak, tier_map)
    progressions, first_to_second = _calculate_progressions(yesterday, tier_map)
    score = _compute_emotion_score(
        today.limit_up_count,
        today.max_streak,
        today.blowup_rate,
        today.limit_down_count,
        has_fault,
    )
    stage, advice, note = _determine_stage_and_advice(
        today.limit_down_count,
        today.blowup_rate,
        today.max_streak,
        has_fault,
    )

    notes = [note]
    if has_fault:
        notes.append("警惕：存在中位梯队断层，资金极端抱团龙头或回流低位，中位股性价比低")
    if first_to_second is not None:
        notes.append(f"首板晋级二板率：{first_to_second * 100:.1f}%")

    return LadderCycleReport(
        max_streak=today.max_streak,
        limit_up_count=today.limit_up_count,
        blowup_rate=today.blowup_rate,
        limit_down_count=today.limit_down_count,
        tiers=tiers,
        progressions=progressions,
        has_fault_line=has_fault,
        cycle_stage=stage,
        emotion_score=round(score, 1),
        action_advice=advice,
        notes=notes,
    )
