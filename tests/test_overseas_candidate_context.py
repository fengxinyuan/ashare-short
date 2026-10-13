"""外盘映射挂到候选行：利空进 warnings、利多进 reasons、不动分数。"""

from __future__ import annotations

from ashare_decision_support.adapters.global_market import (
    GlobalMarketReading,
    GlobalMarketSnapshot,
)
from ashare_decision_support.jobs.morning_select import _attach_overseas_context
from ashare_decision_support.jobs.sources import fetch_overseas
from ashare_decision_support.sectors.overseas_link import read_overseas_links


def _snapshot(*rows: tuple[str, str, float | None]) -> GlobalMarketSnapshot:
    return GlobalMarketSnapshot(
        readings=[
            GlobalMarketReading(
                key=key,
                name=name,
                symbol=f"x_{key}",
                source="sina",
                last=100.0,
                change_pct=change,
                as_of="2026-09-14 08:40:00",
            )
            for key, name, change in rows
        ]
    )


def test_bearish_link_lands_in_warnings_and_leaves_score_alone() -> None:
    read = read_overseas_links(_snapshot(("kosdaq", "韩国创业板", -3.1), ("gold", "金价", 0.1)))
    rows = [
        {"symbol": "688183.SH", "sector": "元件", "score": 78.3, "warnings": ["止损太近"]},
        {"symbol": "600519.SH", "sector": "白酒", "score": 60.0},
    ]

    _attach_overseas_context(rows, read)

    pcb = rows[0]
    assert pcb["score"] == 78.3
    assert pcb["warnings"][0] == "止损太近"
    assert any("韩国创业板 -3.10%" in item and "利空" in item for item in pcb["warnings"])
    assert pcb["overseas_context"][0]["direction"] == "bearish"
    assert "overseas_context" not in rows[1]
    assert "warnings" not in rows[1]


def test_bullish_link_is_a_reason_not_a_warning() -> None:
    read = read_overseas_links(_snapshot(("brent", "布伦特原油", 3.2)))
    rows = [
        {"symbol": "600938.SH", "sector": "油气开采及服务"},
        {"symbol": "600029.SH", "sector": "机场航运"},
    ]

    _attach_overseas_context(rows, read)

    assert any("利多" in item for item in rows[0]["reasons"])
    assert "warnings" not in rows[0]
    # 同一根油价对航空是成本端：同一条读数在另一个板块是利空。
    assert any("利空" in item for item in rows[1]["warnings"])


def test_industry_is_matched_but_concept_tags_are_not() -> None:
    read = read_overseas_links(
        _snapshot(("kosdaq", "韩国创业板", -3.1), ("ura", "铀业股ETF", -3.3))
    )
    rows = [
        # 以概念入选、行业是元件：按行业挂上 KOSDAQ 那条。
        {"symbol": "688183.SH", "sector": "东数西算(算力)", "industry": "元件"},
        # 概念里带「核电」的检测公司：不按概念挂铀价。
        {
            "symbol": "300012.SZ",
            "sector": "PCB概念",
            "industry": "其他社会服务",
            "concepts": ["核电", "芯片概念"],
        },
    ]

    _attach_overseas_context(rows, read)

    assert any("韩国创业板" in item for item in rows[0]["warnings"])
    assert "overseas_context" not in rows[1]


def test_quiet_night_attaches_nothing() -> None:
    read = read_overseas_links(_snapshot(("kosdaq", "韩国创业板", 0.2)))
    rows = [{"symbol": "688183.SH", "sector": "元件"}]
    _attach_overseas_context(rows, read)
    assert rows == [{"symbol": "688183.SH", "sector": "元件"}]
    _attach_overseas_context(rows, None)
    assert rows == [{"symbol": "688183.SH", "sector": "元件"}]


class _Boom:
    def fetch_snapshot(self):
        raise TimeoutError("sina")


class _Ok:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def fetch_snapshot(self):
        return self.snapshot


def test_fetch_overseas_never_raises_and_always_leaves_a_note() -> None:
    notes: list[str] = []
    assert fetch_overseas(None, notes) is None
    assert any("未配置海外快照源" in item for item in notes)

    notes = []
    assert fetch_overseas(_Boom(), notes) is None
    assert any("TimeoutError" in item for item in notes)

    notes = []
    read = fetch_overseas(_Ok(_snapshot(("kosdaq", "韩国创业板", -3.1))), notes)
    assert read is not None and read.links
    assert any("外盘映射 1 条" in item for item in notes)
