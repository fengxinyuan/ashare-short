"""评分分项拆解：分数分档反向之后，哪一项在帮倒忙。

分项只存在建议记录上，兑现记录靠判断编号接回；两半只比共同交易日；
反向的分项排最前并被写进「分数分档」复查项的说明里。
"""

from __future__ import annotations

from datetime import date

import pytest

from ashare_decision_support.jobs.review import render_review
from ashare_decision_support.journal.stats import summarize

REVIEW = date(2026, 8, 31)


def _pair(
    index: int,
    day: str,
    *,
    news: float,
    technical: float,
    change: float,
    score: float,
    realised: float | None = None,
) -> tuple[dict, dict]:
    """同一判断编号的一条建议和它的兑现记录。"""
    decision_id = f"d{day}-{index}"
    symbol = f"{600000 + index:06d}.SH"
    recommendation = {
        "kind": "recommendation",
        "symbol": symbol,
        "trade_date": day,
        "source": "morning",
        "decision_id": decision_id,
        "score": score,
        "score_components": {"news": news, "technical": technical, "catalyst": 0.0},
    }
    outcome = {
        "kind": "outcome",
        "symbol": symbol,
        "trade_date": day,
        "source": "morning",
        "decision_id": decision_id,
        "rec_kind": "recommendation",
        "sessions": 5,
        "score": score,
        "next_close_pct": change,
        "close_pct": change,
        "mfe_pct": change + 0.02,
        "mae_pct": -0.01,
        "entered": False,
        "execution": {
            "filled": True,
            "basis": "开盘成交",
            "exit": "time",
            "return_pct": change if realised is None else realised,
            "fill_session": day,
        },
    }
    return recommendation, outcome


def _ledger() -> tuple[list[dict], list[dict]]:
    """舆情分给得高的那半事后更差（反向），技术分给得高的那半事后更好（同向）。

    高分档（80–100）全是高舆情分的票，于是分数分档也反向——这正是要被点名的组合。
    """
    recommendations: list[dict] = []
    outcomes: list[dict] = []
    for day in (17, 18, 19, 20, 21):
        stamp = f"2026-08-{day}"
        for index in range(40):
            high_news = index % 2 == 0
            high_tech = index % 4 < 2
            change = (-0.02 if high_news else 0.02) + (0.01 if high_tech else -0.01)
            rec, out = _pair(
                index,
                stamp,
                news=6.0 if high_news else 0.0,
                technical=45.0 if high_tech else 30.0,
                change=change,
                score=85.0 if high_news else 65.0,
            )
            recommendations.append(rec)
            outcomes.append(out)
    return recommendations, outcomes


def test_components_are_joined_by_decision_id_and_ranked_worst_first() -> None:
    recommendations, outcomes = _ledger()

    stats = summarize(outcomes, recommendations=recommendations)
    by_name = {row["component"]: row for row in stats["by_component"]}

    assert next(row["component"] for row in stats["by_component"]) == "news"
    assert by_name["news"]["direction"] == "反向"
    assert by_name["news"]["trusted"] is True
    assert by_name["news"]["next_close_gap"] == pytest.approx(-0.04, abs=1e-4)
    assert by_name["news"]["exec_return_gap"] == pytest.approx(-0.04, abs=1e-4)
    assert by_name["news"]["matched_sessions"] == 5
    assert by_name["technical"]["direction"] == "同向"
    assert by_name["technical"]["next_close_gap"] == pytest.approx(0.02, abs=1e-4)
    # 全是 0 的分项切不出两半：它几乎没参与排序，标为无区分度而不是沉默。
    assert by_name["catalyst"]["direction"] == "无区分度"
    assert by_name["catalyst"]["trusted"] is False


def test_signal_and_realised_views_can_disagree_on_a_component() -> None:
    recommendations, outcomes = _ledger()
    for row in outcomes:
        # 执行口径里舆情分高的那半反而更好：两口径异号，判读为分歧。
        row["execution"]["return_pct"] = -row["next_close_pct"]

    by_name = {
        row["component"]: row for row in summarize(outcomes, recommendations)["by_component"]
    }

    assert by_name["news"]["direction"] == "分歧"
    assert by_name["technical"]["direction"] == "分歧"


def test_score_feedback_names_the_most_inverted_component() -> None:
    recommendations, outcomes = _ledger()

    feedback = {
        item["rule"]: item for item in summarize(outcomes, recommendations)["rule_feedback"]
    }

    assert feedback["score"]["status"] == "建议复查"
    assert feedback["score"]["evidence"]["inverted_component"] == "news"
    assert "反向最重的分项是 news" in feedback["score"]["action"]


def test_without_recommendations_the_breakdown_is_empty_and_nothing_else_changes() -> None:
    recommendations, outcomes = _ledger()

    stats = summarize(outcomes)

    assert stats["by_component"] == []
    assert stats["rule_feedback"][3]["status"] == "建议复查"
    assert "score_components" in stats["rule_feedback"][3]["action"]
    assert stats["samples"] == summarize(outcomes, recommendations)["samples"]


def test_legacy_rows_without_decision_ids_join_on_symbol_date_and_source() -> None:
    recommendations, outcomes = _ledger()
    for row in recommendations + outcomes:
        row.pop("decision_id")

    by_name = {
        row["component"]: row for row in summarize(outcomes, recommendations)["by_component"]
    }

    assert by_name["news"]["samples"] == 200


def test_small_components_are_left_out_rather_than_reported_as_noise() -> None:
    recommendations, outcomes = _ledger()
    recommendations[0]["score_components"]["rare"] = 3.0

    names = {row["component"] for row in summarize(outcomes, recommendations)["by_component"]}

    assert "rare" not in names


def test_review_renders_the_breakdown_table() -> None:
    recommendations, outcomes = _ledger()
    stats = summarize(outcomes, recommendations=recommendations)

    content = render_review(REVIEW, {"checked": 0, "resolved": 0, "notes": []}, stats)

    assert "## 评分分项拆解（高值组 − 低值组，按日配对）" in content
    assert "| news | 200 |" in content and "| 反向 |" in content
    assert "| catalyst ⚠️ |" in content and "无区分度" in content
    assert "反向最重的分项是 news" in content
