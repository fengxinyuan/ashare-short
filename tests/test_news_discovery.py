from __future__ import annotations

from datetime import date

from ashare_decision_support.agents import AgentContext, NewsAgent
from ashare_decision_support.news import (
    LexiconSentiment,
    NewsDoc,
    detect_events,
    rank_news_targets,
    sector_coverage_snapshot,
)
from ashare_decision_support.sectors.base import SectorDefinition, SectorMetrics
from ashare_decision_support.sectors.predict import SectorPredictor


def test_paper_supply_chain_terms_reach_light_manufacturing() -> None:
    definition = SectorDefinition("轻工制造", "SW1", ("600001.SH",))
    doc = NewsDoc(title="针叶浆价格回落，纸企成本改善")

    by_sector = SectorPredictor(LexiconSentiment())._attribute_docs([definition], [doc])

    assert by_sector["轻工制造"] == [doc]


def test_event_evidence_keeps_price_and_supply_signals_explainable() -> None:
    evidence = detect_events(["文化纸提价函落地，纸厂停机检修，库存下降"])

    assert evidence.event_types == ("价格", "供给")
    assert "提价" in evidence.hits
    assert "停机" in evidence.hits


def test_coverage_snapshot_distinguishes_price_strength_from_news_blindness() -> None:
    definition = SectorDefinition("轻工制造", "SW1", ("600001.SH",))
    metrics = {
        "轻工制造": SectorMetrics(
            "轻工制造",
            constituents=1,
            covered=1,
            ret_5d=0.04,
            ret_20d=0.08,
            breadth=1.0,
            volume_ratio=1.4,
        )
    }

    snapshot = sector_coverage_snapshot(
        [definition], metrics, [NewsDoc(title="大盘维持震荡")]
    )

    assert snapshot["轻工制造"]["status"] == "行情强但新闻不足"
    assert snapshot["轻工制造"]["doc_count"] == 0


def test_strong_but_news_blind_sector_becomes_a_supplement_target() -> None:
    definition = SectorDefinition("轻工制造", "SW1", ("600001.SH",))
    metrics = {
        "轻工制造": SectorMetrics(
            "轻工制造",
            constituents=1,
            covered=1,
            ret_5d=0.04,
            ret_20d=0.08,
            breadth=1.0,
            volume_ratio=1.4,
        )
    }

    targets = rank_news_targets(
        [definition],
        metrics,
        [NewsDoc(title="大盘维持震荡")],
    )

    assert [target.name for target in targets] == ["轻工制造"]
    assert targets[0].matched_docs == 0


def test_targeted_news_pass_returns_new_paper_headlines() -> None:
    definition = SectorDefinition("轻工制造", "SW1", ("600001.SH",))
    metrics = {
        "轻工制造": SectorMetrics(
            "轻工制造",
            constituents=1,
            covered=1,
            ret_5d=0.04,
            ret_20d=0.08,
            breadth=1.0,
            volume_ratio=1.4,
        )
    }
    target = rank_news_targets([definition], metrics, [NewsDoc(title="宏观消息")])[0]

    class Source:
        def fetch_market(self, trade_date, limit=200):
            return [
                NewsDoc(title="文化纸提价函落地，纸企盈利改善", source="行业源"),
                NewsDoc(title="普通宏观消息", source="宏观源"),
            ], []

    result, _ = NewsAgent().supplement_market(
        AgentContext(trade_date=date(2026, 8, 4), news_source=Source()),
        [target],
        existing_docs=[NewsDoc(title="宏观消息")],
    )

    assert [doc.title for doc in result["docs"]] == ["文化纸提价函落地，纸企盈利改善"]
