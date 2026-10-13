from __future__ import annotations

from datetime import date

import pytest

from ashare_decision_support.jobs.review import render_review, run_review
from ashare_decision_support.journal.stats import summarize


def _outcome(group: str, day: str, change: float, **extra) -> dict:
    return {
        "kind": "outcome",
        "source": "morning",
        "rec_kind": group,
        "trade_date": day,
        "sessions": 5,
        "next_close_pct": change,
        "close_pct": change,
        "mfe_pct": change + 0.02,
        "mae_pct": -0.01,
        "entered": True,
        **extra,
    }


def test_disjoint_sessions_are_not_used_as_a_control_group() -> None:
    stats = summarize(
        [
            _outcome("recommendation", "2026-08-17", 0.05),
            _outcome("control", "2026-08-18", -0.03),
        ]
    )

    assert stats["selection_edge"]["available"] is False
    assert stats["selection_edge"]["next_close_gap"] is None
    assert stats["rule_feedback"][0]["status"] == "待补数据"


def test_daily_matching_removes_bias_from_different_sample_counts() -> None:
    # 两天入选都领先 1 个百分点，但入选组集中在跌日，对照组集中在涨日。
    # 总体中位数相减会错误地报 -7 个百分点。
    rows = [_outcome("recommendation", "2026-08-17", 0.06)]
    rows += [_outcome("control", "2026-08-17", 0.05) for _ in range(50)]
    rows += [_outcome("recommendation", "2026-08-18", -0.02) for _ in range(50)]
    rows += [_outcome("control", "2026-08-18", -0.03)]

    edge = summarize(rows)["selection_edge"]

    assert edge["next_close_gap"] == pytest.approx(0.01)
    assert edge["close_gap"] == pytest.approx(0.01)
    assert edge["matched_sessions"] == 2
    assert edge["trusted"] is False


def test_unmatched_days_cannot_change_the_measured_selection_effect() -> None:
    rows = [
        _outcome("recommendation", "2026-08-17", 0.02),
        _outcome("control", "2026-08-17", 0.03),
    ]
    rows += [_outcome("recommendation", "2026-08-18", 0.5) for _ in range(30)]

    edge = summarize(rows)["selection_edge"]

    assert edge["next_close_gap"] == pytest.approx(-0.01)
    assert edge["metric_coverage"]["next_close_gap"]["left_samples"] == 1


def test_different_holding_windows_only_share_the_next_day_comparison() -> None:
    rows = [
        _outcome("recommendation", "2026-08-17", 0.02, sessions=1),
        _outcome("control", "2026-08-17", 0.01, sessions=5),
    ]

    edge = summarize(rows)["selection_edge"]

    assert edge["next_close_gap"] == pytest.approx(0.01)
    assert edge["close_gap"] is None
    assert edge["mfe_gap"] is None


def test_missing_holding_windows_are_not_assumed_to_match() -> None:
    rows = [
        _outcome("recommendation", "2026-08-17", 0.02, sessions=None),
        _outcome("control", "2026-08-17", 0.01, sessions=None),
    ]

    edge = summarize(rows)["selection_edge"]

    assert edge["next_close_gap"] == pytest.approx(0.01)
    assert edge["close_gap"] is None
    assert edge["mfe_gap"] is None


def test_missing_numbers_do_not_count_toward_comparison_coverage() -> None:
    rows = []
    for day in range(17, 22):
        rows.extend(
            [
                _outcome("recommendation", f"2026-08-{day}", -0.02),
                _outcome("control", f"2026-08-{day}", 0.01),
            ]
        )
        rows += [_outcome("recommendation", f"2026-08-{day}", float("nan")) for _ in range(20)]

    stats = summarize(rows)
    evidence = stats["selection_edge"]["metric_coverage"]["next_close_gap"]

    assert evidence["left_samples"] == 5
    assert evidence["trusted"] is False
    assert stats["rule_feedback"][0]["status"] == "继续观察"


def _adverse_rows() -> list[dict]:
    return [
        _outcome(group, f"2026-08-{day}", change)
        for day in range(17, 22)
        for group, change in (("recommendation", 0.01), ("control", 0.02), ("vetoed", 0.03))
        for _ in range(4)
    ]


def test_adverse_effects_produce_separate_evidence_backed_review_items() -> None:
    stats = summarize(_adverse_rows())
    feedback = {item["rule"]: item for item in stats["rule_feedback"]}

    assert stats["selection_edge"]["trusted"] is True
    assert feedback["selection"]["status"] == "建议复查"
    assert feedback["selection"]["next_close_gap"] == pytest.approx(-0.01)
    assert feedback["veto"]["status"] == "建议复查"
    assert feedback["veto"]["next_close_gap"] == pytest.approx(0.02)
    assert feedback["selection"]["evidence"]["sessions"] == 5
    assert "不据此自动放松" in feedback["veto"]["action"]
    assert stats["model_ready"] is False


def test_single_session_never_qualifies_as_sufficient_comparison_coverage() -> None:
    rows = [
        _outcome(group, "2026-08-17", change)
        for group, change in (("recommendation", -0.01), ("control", 0.02))
        for _ in range(200)
    ]

    stats = summarize(rows)

    assert stats["selection_edge"]["trusted"] is False
    assert stats["rule_feedback"][0]["status"] == "继续观察"


@pytest.mark.parametrize(
    "state",
    [
        {"regime": "未知"},
        {"blowup_rate": float("nan")},
        {"blowup_rate": True},
        {"blowup_rate": -1},
        {"blowup_rate": 2},
    ],
)
def test_unknown_or_invalid_regime_does_not_inflate_readiness(state) -> None:
    stats = summarize([_outcome("recommendation", "2026-08-17", 0.02, market_state=state)])

    assert stats["readiness"]["detail"]["regimes"]["actual"] == 0


def test_invalid_trade_dates_do_not_inflate_readiness() -> None:
    rows = [_outcome("recommendation", f"invalid-{number}", 0.02) for number in range(60)]

    stats = summarize(rows)

    assert stats["readiness"]["detail"]["sessions"]["actual"] == 0


def test_review_explains_which_readiness_requirements_are_still_missing() -> None:
    stats = summarize([_outcome("recommendation", "2026-08-17", 0.02) for _ in range(200)])

    content = render_review(date(2026, 8, 24), {"checked": 0, "resolved": 0, "notes": []}, stats)

    assert "记录数：200/200，已达标" in content
    assert "交易日覆盖：1/60，还差 59" in content
    assert "已知情绪周期：0/3，还差 3" in content
    assert "还差 **0** 条" not in content


def test_review_job_returns_feedback_and_renders_veto_cost(monkeypatch) -> None:
    class Book:
        def rows(self, until=None):
            assert until == date(2026, 8, 24)
            return _adverse_rows()

    monkeypatch.setattr(
        "ashare_decision_support.jobs.review.verify_pending",
        lambda *args, **kwargs: {"checked": 0, "resolved": 0, "notes": []},
    )

    summary = run_review(object(), journal=Book(), notifiers=[], trade_date=date(2026, 8, 24))

    assert "## 否决代价" in summary["content"]
    assert "## 规则观察与复查" in summary["content"]
    assert "建议复查" in summary["content"]
    assert summary["readiness"]["detail"]["sessions"]["actual"] == 5
    assert summary["rule_feedback"][0]["status"] == "建议复查"
