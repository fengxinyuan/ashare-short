from __future__ import annotations

from datetime import date

import pytest

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


@pytest.mark.parametrize(
    ("headline", "industry"),
    [
        ("HBM订单超预期，存储芯片涨价", "半导体"),
        ("电网投资增加，变压器订单超预期", "电网设备"),
        ("面板价格上涨，OLED需求增长", "光学光电子"),
    ],
)
def test_news_reaches_the_specific_industry_without_spilling_into_sibling_industries(
    headline, industry
):
    definitions = [
        SectorDefinition(name, "THS_INDUSTRY", ("600001.SH",))
        for name in ("半导体", "电网设备", "光学光电子", "光伏设备")
    ]
    doc = NewsDoc(title=headline)

    attributed = SectorPredictor(LexiconSentiment()).attribute_docs(definitions, [doc])

    assert {name for name, docs in attributed.items() if docs} == {industry}


def test_paper_supply_chain_terms_reach_paper_industry() -> None:
    definition = SectorDefinition("造纸", "THS_INDUSTRY", ("600001.SH",))
    doc = NewsDoc(title="针叶浆价格回落，纸企成本改善")

    by_sector = SectorPredictor(LexiconSentiment()).attribute_docs([definition], [doc])

    assert by_sector["造纸"] == [doc]


def test_event_evidence_keeps_price_and_supply_signals_explainable() -> None:
    evidence = detect_events(["文化纸提价函落地，纸厂停机检修，库存下降"])

    assert evidence.event_types == ("价格", "供给")
    assert "提价" in evidence.hits
    assert "停机" in evidence.hits


def test_coverage_snapshot_distinguishes_price_strength_from_news_blindness() -> None:
    definition = SectorDefinition("造纸", "THS_INDUSTRY", ("600001.SH",))
    metrics = {
        "造纸": SectorMetrics(
            "造纸",
            constituents=1,
            covered=1,
            ret_5d=0.04,
            ret_20d=0.08,
            breadth=1.0,
            volume_ratio=1.4,
        )
    }

    snapshot = sector_coverage_snapshot([definition], metrics, [NewsDoc(title="大盘维持震荡")])

    assert snapshot["造纸"]["status"] == "行情强但新闻不足"
    assert snapshot["造纸"]["doc_count"] == 0


def test_strong_but_news_blind_sector_becomes_a_supplement_target() -> None:
    definition = SectorDefinition("造纸", "THS_INDUSTRY", ("600001.SH",))
    metrics = {
        "造纸": SectorMetrics(
            "造纸",
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

    assert [target.name for target in targets] == ["造纸"]
    assert targets[0].matched_docs == 0


def test_targeted_news_pass_returns_new_paper_headlines() -> None:
    definition = SectorDefinition("造纸", "THS_INDUSTRY", ("600001.SH",))
    metrics = {
        "造纸": SectorMetrics(
            "造纸",
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
