"""派发/洗盘判别。

最重要的一条是 `test_the_muyuan_case_...`：单日相对强度脱离多日背景会读反，
那正是 2026-08-04 人工分析踩的坑。
"""

from __future__ import annotations

from types import SimpleNamespace

from ashare_decision_support.signals.distribution import (
    MIN_EVIDENCE,
    VERDICT_ACCELERATING,
    VERDICT_DISTRIBUTION,
    VERDICT_SHAKEOUT,
    VERDICT_UNDECIDED,
    judge_distribution,
)
from ashare_decision_support.signals.volume_price import (
    DIVERGENCE_TOP,
    QUADRANT_DOWN_CONTRACT,
    QUADRANT_DOWN_EXPAND,
)


def feats(**kw) -> SimpleNamespace:
    base = {
        "quadrant": None,
        "up_down_vol_ratio": None,
        "climax": None,
        "climax_stall": None,
        "dry_up": None,
        "breakdown_on_volume": None,
        "divergence": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def rel(day=None, week=None, month=None) -> SimpleNamespace:
    """`RelativeStrength.excess` 的键是窗口整数，不是字符串。"""
    excess = {}
    if day is not None:
        excess[1] = day
    if week is not None:
        excess[5] = week
    if month is not None:
        excess[20] = month
    return SimpleNamespace(excess=excess)


# 判别成立


def test_volume_down_plus_weak_flow_plus_high_position_reads_as_distribution() -> None:
    read = judge_distribution(
        features=feats(
            quadrant=QUADRANT_DOWN_EXPAND, up_down_vol_ratio=0.7, breakdown_on_volume=True
        ),
        relative=rel(day=-0.03, month=-0.08),
        range_position_60d=0.85,
        price_vs_vwap_pct=-0.012,
    )
    assert read.verdict == VERDICT_DISTRIBUTION
    assert read.distribution_points > read.shakeout_points
    assert any("下跌日的成交量大于上涨日" in e for e in read.evidence)


def test_contracting_volume_at_the_range_low_reads_as_shakeout() -> None:
    read = judge_distribution(
        features=feats(quadrant=QUADRANT_DOWN_CONTRACT, up_down_vol_ratio=1.4, dry_up=True),
        relative=rel(day=-0.001, month=0.05),
        range_position_60d=0.20,
        price_vs_vwap_pct=0.004,
    )
    assert read.verdict == VERDICT_SHAKEOUT
    assert any("卖压在衰竭" in e for e in read.counter_evidence)
    assert any("不保证价格会收回" in n for n in read.notes)


def test_two_consecutive_expanding_down_bars_escalate_to_accelerating() -> None:
    """放量下跌 → 放量下跌 = 加速出货。"""
    read = judge_distribution(
        features=feats(
            quadrant=QUADRANT_DOWN_EXPAND, up_down_vol_ratio=0.6, divergence=DIVERGENCE_TOP
        ),
        relative=rel(day=-0.03, month=-0.09),
        range_position_60d=0.80,
        prior_quadrants=[QUADRANT_DOWN_EXPAND],
    )
    assert read.verdict == VERDICT_ACCELERATING


def test_a_single_expanding_down_bar_is_not_accelerating() -> None:
    read = judge_distribution(
        features=feats(quadrant=QUADRANT_DOWN_EXPAND, up_down_vol_ratio=0.6),
        relative=rel(day=-0.03, month=-0.09),
        range_position_60d=0.80,
        prior_quadrants=[QUADRANT_DOWN_CONTRACT],
    )
    assert read.verdict == VERDICT_DISTRIBUTION


# 必须能说「不判别」


def test_thin_evidence_abstains_rather_than_guessing() -> None:
    read = judge_distribution(features=feats(quadrant=QUADRANT_DOWN_EXPAND))

    assert read.verdict == VERDICT_UNDECIDED
    assert any(f"需要 {MIN_EVIDENCE} 条" in n for n in read.notes)


def test_balanced_evidence_abstains() -> None:
    """两边都有话说的时候强行选边，是把「不知道」包装成「知道」。"""
    read = judge_distribution(
        features=feats(quadrant=QUADRANT_DOWN_EXPAND, up_down_vol_ratio=1.4),
        range_position_60d=0.20,
        price_vs_vwap_pct=-0.02,
    )
    assert read.verdict == VERDICT_UNDECIDED
    assert any("净分差" in n for n in read.notes)


def test_everything_missing_still_returns_a_usable_read() -> None:
    read = judge_distribution()

    assert read.verdict == VERDICT_UNDECIDED
    assert read.evidence == []
    assert read.counter_evidence == []


# 那次误判


def test_the_muyuan_case_does_not_count_a_rotation_day_as_distribution() -> None:
    """2026-08-04 牧原：单日超额 −4.5%，但 20 日超额 +13.4%。

    只看单日会判成「逆势破位」。方向相反时这一层不给分，只留一条说明——
    这正是人工分析当时读反的那个点。
    """
    read = judge_distribution(
        features=feats(quadrant=QUADRANT_DOWN_EXPAND, up_down_vol_ratio=0.95),
        relative=rel(day=-0.045, month=0.134),
        range_position_60d=0.52,
        price_vs_vwap_pct=-0.004,
    )

    assert any("方向相反" in n for n in read.notes)
    # 多日仍在跑赢，这条算洗盘侧的反证，不算派发证据。
    assert any("仍在跑赢" in e for e in read.counter_evidence)
    assert not any("单日超额" in e for e in read.evidence)


def test_same_direction_single_and_multi_day_weakness_does_count() -> None:
    """单日与多日同向走弱时，单日那条才计分。"""
    read = judge_distribution(
        features=feats(quadrant=QUADRANT_DOWN_EXPAND, up_down_vol_ratio=0.8),
        relative=rel(day=-0.03, month=-0.06),
        range_position_60d=0.75,
    )
    assert any("与多日弱势同向" in e for e in read.evidence)
    assert read.verdict == VERDICT_DISTRIBUTION


# 输出纪律


def test_the_read_never_claims_to_predict() -> None:
    read = judge_distribution(
        features=feats(quadrant=QUADRANT_DOWN_EXPAND, up_down_vol_ratio=0.7),
        relative=rel(day=-0.03, month=-0.08),
        range_position_60d=0.85,
    )
    assert any("不是对明天的预测" in line for line in read.describe())


def test_wyckoff_phase_contributes_without_dominating() -> None:
    read = judge_distribution(
        features=feats(quadrant=QUADRANT_DOWN_CONTRACT, up_down_vol_ratio=1.3),
        wyckoff_phase="DISTRIBUTION",
        range_position_60d=0.25,
    )
    assert any("威科夫阶段 DISTRIBUTION" in e for e in read.evidence)
    assert read.verdict == VERDICT_SHAKEOUT
