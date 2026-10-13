"""评分比较只使用同时可比的记录，覆盖不足的指标不能决定方向。"""

from __future__ import annotations

import json
from datetime import date

import pytest

from ashare_decision_support.jobs.review import render_review, run_review
from ashare_decision_support.journal import Journal
from ashare_decision_support.journal.stats import summarize


def _row(day: int, score: float, change: float | None, **extra) -> dict:
    return {
        "kind": "outcome",
        "trade_date": f"2026-08-{day}",
        "source": "morning",
        "rec_kind": "recommendation",
        "sessions": 5,
        "score": score,
        "next_close_pct": change,
        "close_pct": change,
        "mfe_pct": 0.03,
        "mae_pct": -0.02,
        **extra,
    }


def _score_feedback(rows: list[dict]) -> dict:
    return next(item for item in summarize(rows)["rule_feedback"] if item["rule"] == "score")


def _factor(rows: list[dict]) -> dict:
    return next(item for item in summarize(rows)["by_component"] if item["component"] == "factor")


def _factor_rows() -> list[dict]:
    return [
        _row(day, score, change, score_components={"factor": value})
        for day in range(17, 22)
        for score, change, value in ((85, 0.02, 1), (65, 0.0, 0))
        for _ in range(10)
    ]


def test_score_direction_uses_daily_pairs_despite_reversed_pooled_medians() -> None:
    # 每天高分都领先 1 个百分点，但高分集中于跌日、低分集中于涨日。
    rows = [
        _row(17 + i, score, change)
        for i in range(5)
        for score, change, count in (
            (85, 0.06 if i % 2 == 0 else -0.02, 4 if i % 2 == 0 else 40),
            (65, 0.05 if i % 2 == 0 else -0.03, 40 if i % 2 == 0 else 4),
        )
        for _ in range(count)
    ]

    feedback = _score_feedback(rows)

    assert feedback["gap"] == pytest.approx(0.01)
    assert feedback["status"] == "保留观察"
    assert feedback["evidence"]["sessions"] == 5
    assert feedback["evidence"]["left_samples"] == 92
    assert feedback["evidence"]["right_samples"] == 128


def test_disjoint_score_dates_do_not_qualify_as_comparison_evidence() -> None:
    rows = [
        _row(day + offset, score, change)
        for day in range(17, 22)
        for score, change, offset in ((85, -0.02, 7), (65, 0.03, 0))
        for _ in range(4)
    ]

    feedback = _score_feedback(rows)

    assert feedback["gap"] is None
    assert feedback["status"] == "待补数据"
    assert feedback["evidence"]["sessions"] == 0
    assert feedback["evidence"]["trusted"] is False


@pytest.mark.parametrize(
    ("high_meta", "low_meta"),
    [
        ({"source": "advise"}, {"source": "morning"}),
        ({"strategy_version": "rule-v2"}, {"strategy_version": "rule-v1"}),
        ({"strategy_version": "rule-v2"}, {}),
    ],
)
def test_scores_and_components_do_not_mix_sources_or_strategy_versions(high_meta, low_meta) -> None:
    rows = [
        _row(day, score, change, score_components={"factor": score}, **meta)
        for day in range(17, 22)
        for score, change, meta in ((85, -0.02, high_meta), (65, 0.03, low_meta))
        for _ in range(4)
    ]

    feedback = _score_feedback(rows)
    component = _factor(rows)

    assert feedback["gap"] is None
    assert feedback["evidence"]["trusted"] is False
    assert component["next_close_gap"] is None
    assert component["trusted"] is False


@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf"), True])
def test_score_coverage_counts_only_valid_returns(invalid) -> None:
    rows = [_row(day, 85, -0.02) for day in range(17, 22)]
    rows += [_row(day, 85, invalid) for day in range(17, 22) for _ in range(20)]
    rows += [_row(day, 65, 0.03) for day in range(17, 22) for _ in range(4)]

    feedback = _score_feedback(rows)

    assert feedback["gap"] == pytest.approx(-0.05)
    assert feedback["status"] == "继续观察"
    assert feedback["evidence"]["left_samples"] == 5
    assert feedback["evidence"]["right_samples"] == 20


def test_unmatched_days_cannot_change_score_direction_or_coverage() -> None:
    rows = [
        _row(day, score, change)
        for day in range(17, 22)
        for score, change in ((85, -0.01), (65, 0.02))
        for _ in range(4)
    ]
    before = _score_feedback(rows)
    rows += [_row(24, 85, 0.90) for _ in range(200)]

    assert _score_feedback(rows) == before


def test_score_pair_selection_uses_coverage_and_not_the_sign_of_returns() -> None:
    rows = [
        _row(day, score, change)
        for day in range(17, 22)
        for score, change, count in ((85, -0.5, 1), (75, 0.02, 4), (65, 0.01, 4))
        for _ in range(count)
    ]

    feedback = _score_feedback(rows)

    assert feedback["evidence"]["top_bucket"] == "70-80"
    assert feedback["evidence"]["bottom_bucket"] == "60-70"
    assert feedback["gap"] == pytest.approx(0.01)
    for row in rows:
        row["next_close_pct"] *= -1
    assert _score_feedback(rows)["evidence"] == feedback["evidence"]


def test_valid_scores_below_sixty_are_included_in_the_comparison() -> None:
    rows = [
        _row(day, score, change)
        for day in range(17, 22)
        for score, change in ((85, 0.02), (55, 0.01))
        for _ in range(4)
    ]

    feedback = _score_feedback(rows)

    assert feedback["status"] == "保留观察"
    assert feedback["evidence"]["bottom_bucket"] == "0-60"


@pytest.mark.parametrize("score", [True, float("nan"), float("inf"), -1, 100.5])
def test_invalid_scores_are_not_assigned_to_a_scored_bucket(score) -> None:
    stats = summarize([_row(17, score, 0.01)])

    assert stats["by_score"][0]["bucket"] == "无分数"
    assert stats["rule_feedback"][3]["status"] == "待补数据"


def test_selection_comparison_also_requires_matching_strategy_versions() -> None:
    rows = [
        _row(day, 75, change, rec_kind=group, strategy_version=version)
        for day in range(17, 22)
        for group, version, change in (
            ("recommendation", "rule-v2", -0.02),
            ("control", "rule-v1", 0.03),
        )
        for _ in range(4)
    ]

    edge = summarize(rows)["selection_edge"]

    assert edge["available"] is False
    assert edge["trusted"] is False


@pytest.mark.parametrize(
    "mismatch",
    [
        {"trade_date": "2026-08-24"},
        {"source": "post_close"},
        {"strategy_version": "rule-v1"},
    ],
)
def test_model_comparisons_require_common_dates_sources_and_versions(mismatch) -> None:
    high = _row(17, 85, 0.03, adjudication={"overrode_rule": True})
    low = _row(17, 65, 0.01, adjudication={"used_fallback": True}, **mismatch)

    edge = summarize([high, low])["adjudication_edge"]

    assert edge["available"] is False
    assert edge["next_close_gap"] is None
    assert edge["trusted"] is False


def test_model_comparisons_exclude_abstentions_even_with_conflicting_flags() -> None:
    rows = [
        _row(17, 85, 0.03, adjudication={"overrode_rule": True}),
        _row(17, 65, 0.01, adjudication={"used_fallback": True}),
        _row(17, 75, -0.9, adjudication={"used_fallback": True, "abstained": True}),
        _row(17, 75, -0.9, rec_kind="vetoed"),
    ]

    edge = summarize(rows)["adjudication_edge"]

    assert edge["next_close_gap"] == pytest.approx(0.02)
    assert edge["default_samples"] == 1
    assert edge["abstained_samples"] == 1


def test_components_are_split_within_each_day_despite_drifting_levels() -> None:
    rows = _factor_rows()
    for row in rows:
        row["score_components"]["factor"] += (int(row["trade_date"][-2:]) - 17) * 10

    component = _factor(rows)

    assert component["next_close_gap"] == pytest.approx(0.02)
    assert component["matched_sessions"] == 5
    assert component["trusted"] is True
    assert component["cut_min"] == 0.5
    assert component["cut_max"] == 40.5


def test_components_keep_tied_values_together_when_most_are_at_the_maximum() -> None:
    rows = [
        _row(day, score, change, score_components={"factor": value})
        for day in range(17, 22)
        for score, change, value, count in ((85, 0.02, 1, 18), (65, 0.0, 0, 6))
        for _ in range(count)
    ]

    component = _factor(rows)

    assert component["direction"] == "同向"
    assert component["trusted"] is True
    assert component["high_samples"] == 90
    assert component["low_samples"] == 30
    assert component["next_close_gap"] == pytest.approx(0.02)


def test_constant_days_do_not_inflate_component_coverage_or_change_its_split() -> None:
    rows = _factor_rows()
    before = _factor(rows)
    rows += [_row(24, 75, -0.5, score_components={"factor": 999}) for _ in range(500)]

    component = _factor(rows)

    assert component["samples"] == before["samples"] + 500
    assert component["unpaired_samples"] == 500
    assert component["metric_coverage"] == before["metric_coverage"]
    assert component["next_close_gap"] == before["next_close_gap"]
    assert component["cut_min"] == component["cut_max"] == 0.5


def test_missing_component_dates_are_missing_data_and_not_a_constant_factor() -> None:
    rows = _factor_rows()
    for row in rows:
        row["trade_date"] = "unknown"

    component = _factor(rows)

    assert component["direction"] == "待补数据"
    assert component["trusted"] is False
    assert component["next_close_gap"] is None


def test_sparse_execution_cannot_overrule_a_well_covered_signal_direction() -> None:
    rows = _factor_rows()
    for row in (rows[0], rows[10]):
        row["execution"] = {
            "filled": True,
            "basis": "open_proxy",
            "exit": "time",
            "return_pct": -0.3 if row["score"] == 85 else 0.3,
        }

    component = _factor(rows)

    assert component["direction"] == "同向"
    assert component["direction_basis"] == "仅次日"
    assert component["metric_coverage"]["exec_return_gap"] == {
        "left_samples": 1,
        "right_samples": 1,
        "sessions": 1,
        "trusted": False,
    }
    assert component["exec_return_gap"] == pytest.approx(-0.6)
    content = render_review(
        date(2026, 8, 31), {"checked": 0, "resolved": 0, "notes": []}, summarize(rows)
    )
    assert "1/1 条 · 1 日" in content
    assert "仅次日" in content


def test_execution_can_supply_the_direction_when_next_day_readings_are_missing() -> None:
    rows = _factor_rows()
    for row in rows:
        row["execution"] = {
            "filled": True,
            "basis": "open_proxy",
            "exit": "time",
            "return_pct": -row["next_close_pct"],
        }
        row["next_close_pct"] = None

    component = _factor(rows)

    assert component["direction"] == "反向"
    assert component["direction_basis"] == "仅执行"
    assert component["trusted"] is True
    assert component["metric_coverage"]["exec_return_gap"]["sessions"] == 5
    assert component["metric_coverage"]["next_close_gap"]["trusted"] is False


def test_review_joins_components_from_the_same_journal_snapshot(tmp_path, monkeypatch) -> None:
    outcomes = _factor_rows()
    recommendations = []
    for index, row in enumerate(outcomes):
        row.update(symbol=f"{600000 + index:06d}.SH", decision_id=f"decision-{index}")
        recommendations.append(
            {
                "kind": "recommendation",
                "symbol": row["symbol"],
                "trade_date": row["trade_date"],
                "source": row["source"],
                "decision_id": row["decision_id"],
                "score_components": row.pop("score_components"),
            }
        )
    # 回放截止日之后才兑现的一整日不应进入统计或分项覆盖。
    for row in outcomes[:20]:
        row["resolved_on"] = "2026-09-01"
    path = tmp_path / "2026-08.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in recommendations + outcomes))
    original = path.read_bytes()
    monkeypatch.setattr(
        "ashare_decision_support.jobs.review.verify_pending",
        lambda *args, **kwargs: {"checked": 0, "resolved": 0, "notes": []},
    )

    result = run_review(
        object(), journal=Journal(tmp_path), notifiers=[], trade_date=date(2026, 8, 31)
    )

    assert result["total_outcomes"] == 80
    assert "| factor ⚠️ | 80 |" in result["content"]
    assert "40/40 条 · 4 日" in result["content"]
    assert path.read_bytes() == original
