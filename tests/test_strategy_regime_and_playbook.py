"""情绪周期闸门与趋势分支。

这两层是**决策骨架**：闸门决定今天做不做、允许哪类打法，分支决定一只票能用哪
条判据链。改造前两者都不存在——情绪周期只用于事后分层，趋势只是一个权重。

用例里带具体数字的那几条是**回归测试**，钉的是 2026-08-03 实盘输出暴露的两个
缺陷：炸板率 51.94% 当天照常输出 60 条建议，以及 60 条里 52 条是放量回调。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ashare_decision_support.models.enums import EmotionCycle, Playbook, TrendState
from ashare_decision_support.models.schemas import DailyTechnicalSnapshot, PriceLevel
from ashare_decision_support.signals.dipbuy import DipFeatures
from ashare_decision_support.strategy.playbook import evaluate_playbooks, playbooks_for_trend
from ashare_decision_support.strategy.regime import classify_regime, regime_from_limit_pool


def snap(**kw) -> DailyTechnicalSnapshot:
    base = {
        "symbol": "600519.SH",
        "bar_count": 250,
        "last_open": 99.0,
        "last_high": 101.0,
        "last_low": 98.0,
        "last_close": 100.0,
        "ma20": 96.0,
        "ma60": 90.0,
        "atr14": 2.0,
        "volume_ratio_20d": 1.0,
        "range_position_60d": 0.5,
        "trend_state": TrendState.UPTREND,
        "stop_reference": 95.0,
        "target_reference": 115.0,
        "explanation": "fixture",
        "source_ts": datetime(2026, 8, 3, 15, 0),
    }
    base.update(kw)
    return DailyTechnicalSnapshot(**base)


def dip(**kw) -> DipFeatures:
    base = {
        "pullback_pct": -0.06,
        "atr_pct": 0.02,
        "pullback_atr": 3.0,
        "pullback_band": "ideal",
        "shrinking": True,
        "volume_contraction": 0.60,
        "dist_to_ma20_pct": 0.04,
        "ret_1d": -0.01,
    }
    base.update(kw)
    return DipFeatures(**base)


class Wyckoff:
    def __init__(self, **kw) -> None:
        self.spring = kw.get("spring", False)
        self.last_point_of_support = kw.get("lps", False)
        self.sign_of_strength = kw.get("sos", False)
        self.upthrust = kw.get("upthrust", False)


# ---- 情绪周期闸门 ---------------------------------------------------------


def test_the_2026_08_03_reading_is_an_ebb_not_a_normal_day() -> None:
    """回归：当天炸板率 51.94%，系统照常输出了 60 条低吸建议、仓位 0.68。

    炸板率过半意味着昨天冲板的票一半没守住——这正是超短最该收手的信号。它当时
    被读到了、写进了台账，然后被忽略。
    """
    verdict = classify_regime(
        blowup_rate=0.5194, max_streak=9, limit_up_count=99, limit_down_count=0
    )

    assert verdict.cycle is EmotionCycle.EBB
    assert verdict.position_cap == 0.30
    assert verdict.leader_only is True
    # 退潮期允许低吸和超跌反转，但不允许追突破。
    assert verdict.permits(Playbook.PULLBACK)
    assert verdict.permits(Playbook.REVERSAL)
    assert not verdict.permits(Playbook.BREAKOUT)


def test_a_panic_tape_halts_selection_entirely() -> None:
    """冰点不是「压低仓位」，是**不出手**。"""
    verdict = classify_regime(
        blowup_rate=0.35, max_streak=3, limit_up_count=40, limit_down_count=22
    )

    assert verdict.cycle is EmotionCycle.FREEZING
    assert verdict.halted
    assert verdict.position_cap == 0.0
    assert verdict.allowed == frozenset()


def test_too_few_limit_ups_is_freezing_even_with_a_low_blowup_rate() -> None:
    """封板率好看但只有 8 家涨停，是没有赚钱效应，不是封板结实。

    分母小的时候炸板率本身失去意义——2/10 和 200/1000 都是 20%。
    """
    verdict = classify_regime(
        blowup_rate=0.20, max_streak=2, limit_up_count=8, limit_down_count=0
    )
    assert verdict.cycle is EmotionCycle.FREEZING


@pytest.mark.parametrize(
    "rate,streak,ups,expected",
    [
        (0.20, 3, 40, EmotionCycle.LAUNCH),
        (0.20, 6, 60, EmotionCycle.MOMENTUM),
        (0.20, 8, 100, EmotionCycle.CLIMAX),
        (0.40, 4, 50, EmotionCycle.DIVERGENCE),
        (0.55, 4, 50, EmotionCycle.EBB),
    ],
)
def test_cycle_boundaries(rate, streak, ups, expected) -> None:
    assert classify_regime(rate, streak, ups, 0).cycle is expected


def test_a_climax_allows_only_the_conservative_playbook() -> None:
    """高潮期只减不加：突破被关掉，只留强势票回踩，且仓位砍到 0.40。"""
    verdict = classify_regime(0.20, 8, 100, 0)

    assert verdict.allowed == frozenset({Playbook.PULLBACK})
    assert verdict.position_cap == 0.40
    assert verdict.leader_only is True


def test_a_missing_limit_pool_is_not_neutral() -> None:
    """没数据 ≠ 中性。涨停池取不到时按退潮档压仓位，并且只留最保守的打法。"""
    verdict = classify_regime(None)

    assert verdict.cycle is EmotionCycle.UNKNOWN
    assert verdict.trusted is False
    assert verdict.position_cap == 0.30
    assert verdict.allowed == frozenset({Playbook.PULLBACK})
    assert any("未知不等于中性" in text for text in verdict.warnings)


def test_a_limit_pool_that_reports_not_ok_is_treated_as_missing() -> None:
    """半份数据比没有数据更危险——它看起来像可以用。"""

    class Broken:
        ok = False
        blowup_rate = 0.10
        max_streak = 9
        limit_up_count = 200
        limit_down_count = 0

    assert regime_from_limit_pool(Broken()).cycle is EmotionCycle.UNKNOWN
    assert regime_from_limit_pool(None).cycle is EmotionCycle.UNKNOWN


# ---- 趋势分支 -------------------------------------------------------------


def test_each_trend_gets_a_different_judgement_chain() -> None:
    """趋势是分支不是权重：下跌趋势只剩一条路，而且那条路自带触发要求。"""
    assert playbooks_for_trend(TrendState.UPTREND) == frozenset(
        {Playbook.BREAKOUT, Playbook.PULLBACK}
    )
    assert playbooks_for_trend(TrendState.REBOUND) == frozenset({Playbook.PULLBACK})
    assert playbooks_for_trend(TrendState.RANGE) == frozenset({Playbook.RANGE_LOW})
    assert playbooks_for_trend(TrendState.DOWNTREND) == frozenset({Playbook.REVERSAL})


def test_an_expanding_volume_pullback_is_rejected() -> None:
    """回归：2026-08-03 的 60 条建议里 52 条是放量回调。

    `signals/dipbuy.py` 自己写着「回调放量意味着出货」，但改造前缩量只是 ±6 分的
    权重，压不过趋势与威科夫的 50 分。现在它是门槛。
    """
    match = evaluate_playbooks(snap(), dip(volume_contraction=1.35), None, None)

    assert not match.ok
    assert any("疑似出货" in text for item in match.rejected for text in item.reasons)


def test_a_contracting_volume_pullback_is_accepted() -> None:
    match = evaluate_playbooks(snap(), dip(volume_contraction=0.62), None, None)

    assert match.primary is Playbook.PULLBACK
    assert any("回踩" in text for text in match.reasons)


def test_a_close_below_ma20_is_a_breakdown_not_a_pullback() -> None:
    match = evaluate_playbooks(snap(), dip(dist_to_ma20_pct=-0.005), None, None)

    assert not match.ok
    assert any("跌破 MA20" in text for item in match.rejected for text in item.reasons)


def test_a_breakout_needs_volume_and_is_blocked_when_volume_is_blown_off() -> None:
    at_high = dip(pullback_band="none", pullback_atr=0.2, volume_contraction=1.1)

    quiet = evaluate_playbooks(snap(range_position_60d=0.95, volume_ratio_20d=0.9), at_high, None, None)
    assert not quiet.ok, "无量突破不成立"

    good = evaluate_playbooks(snap(range_position_60d=0.95, volume_ratio_20d=1.8), at_high, None, None)
    assert good.primary is Playbook.BREAKOUT

    blown = evaluate_playbooks(snap(range_position_60d=0.95, volume_ratio_20d=5.0), at_high, None, None)
    assert not blown.ok, "量比 5.0 是情绪透支，不是突破确认"


def test_a_downtrend_needs_a_reversal_trigger_not_just_depth() -> None:
    """**跌得多本身不是买入理由。** 这是「下跌趋势什么样的可以买」的答案。"""
    falling = snap(trend_state=TrendState.DOWNTREND, ma20=104.0, ma60=110.0)
    deep = dip(pullback_band="deep", pullback_atr=5.5, dist_to_ma20_pct=-0.05, ret_1d=-0.02)

    knife = evaluate_playbooks(falling, deep, None, None)
    assert not knife.ok
    assert any("接刀" in text for item in knife.rejected for text in item.reasons)

    sprung = evaluate_playbooks(falling, deep, Wyckoff(spring=True), None)
    assert sprung.primary is Playbook.REVERSAL
    assert any("仓位减半" in text for text in sprung.warnings)


def test_a_volume_engulf_also_triggers_a_reversal() -> None:
    falling = snap(trend_state=TrendState.DOWNTREND, ma20=104.0, ma60=110.0)
    engulf = dip(
        pullback_band="deep", pullback_atr=5.5, dist_to_ma20_pct=-0.05,
        ret_1d=0.04, volume_contraction=1.6,
    )

    match = evaluate_playbooks(falling, engulf, None, None)
    assert match.primary is Playbook.REVERSAL
    assert any("放量反包" in text for text in match.reasons)


def test_a_shallow_downtrend_name_never_reaches_the_reversal_check() -> None:
    """下跌趋势里只回调一点点的票不是反转位——它只是还在跌的路上。"""
    falling = snap(trend_state=TrendState.DOWNTREND, ma20=104.0, ma60=110.0)
    match = evaluate_playbooks(falling, dip(pullback_band="shallow"), Wyckoff(spring=True), None)

    assert not match.ok


def test_a_range_name_can_only_be_bought_at_the_low_end() -> None:
    support = PriceLevel(
        side="support", price=94.0, touches=3, strength=0.8,
        distance_pct=-0.06, label="区间下沿",
    )
    ranged = snap(trend_state=TrendState.RANGE, range_position_60d=0.20, supports=[support])

    match = evaluate_playbooks(ranged, dip(volume_contraction=0.8), None, None)
    assert match.primary is Playbook.RANGE_LOW

    high_in_range = snap(trend_state=TrendState.RANGE, range_position_60d=0.70)
    assert not evaluate_playbooks(high_in_range, dip(), None, None).ok


# ---- 两层求交集 -----------------------------------------------------------


def test_the_regime_can_veto_a_setup_the_trend_would_allow() -> None:
    """一只 UPTREND 突破票，在退潮期不该被选中——这正是两层求交集的意义。"""
    at_high = dip(pullback_band="none", pullback_atr=0.2, volume_contraction=1.1)
    strong = snap(range_position_60d=0.95, volume_ratio_20d=1.8)

    ebb = classify_regime(0.5194, 9, 99, 0)
    match = evaluate_playbooks(strong, at_high, None, ebb.allowed)

    assert not match.ok
    assert Playbook.BREAKOUT in match.blocked_by_regime


def test_a_frozen_regime_blocks_every_playbook() -> None:
    frozen = classify_regime(0.35, 3, 40, 22)
    match = evaluate_playbooks(snap(), dip(volume_contraction=0.6), None, frozen.allowed)

    assert not match.ok
    assert Playbook.PULLBACK in match.blocked_by_regime
