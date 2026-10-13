"""连板梯队晋级与情绪退潮周期量化单元测试。"""

from __future__ import annotations

from datetime import date

from ashare_decision_support.adapters.limit_pool import LimitPoolSnapshot, LimitStock
from ashare_decision_support.sectors.ladder_cycle import (
    analyze_ladder_and_cycle,
)


def _stock(symbol: str, name: str, streak: int) -> LimitStock:
    return LimitStock(
        symbol=symbol,
        name=name,
        price=10.0,
        change_pct=0.10,
        amount=100_000_000.0,
        streak=streak,
    )


def test_initiation_stage_and_ladder_building() -> None:
    # 启动期：首板多、最高板 2、无跌停、炸板率低
    today = LimitPoolSnapshot(
        trade_date=date(2026, 9, 15),
        limit_up=[
            _stock("000001", "首板A", 1),
            _stock("000002", "首板B", 1),
            _stock("000003", "二板A", 2),
        ],
        blowups=[],
        limit_down=[],
        ok=True,
    )
    res = analyze_ladder_and_cycle(today)
    assert res.max_streak == 2
    assert res.limit_up_count == 3
    assert len(res.tiers) == 2
    assert res.has_fault_line is False
    assert res.cycle_stage == "启动试错期"
    assert "轻仓试错" in res.action_advice


def test_expansion_stage_and_progression_rate() -> None:
    # 昨日 10 个首板，今日 4 个二板（晋级率 40%），梯队递进最高 4 板
    yest = LimitPoolSnapshot(
        trade_date=date(2026, 9, 14),
        limit_up=[_stock(f"0000{i:02d}", f"昨首板{i}", 1) for i in range(10)],
        ok=True,
    )
    today = LimitPoolSnapshot(
        trade_date=date(2026, 9, 15),
        limit_up=[
            *[_stock(f"1000{i:02d}", f"今首板{i}", 1) for i in range(15)],
            *[_stock(f"2000{i:02d}", f"今二板{i}", 2) for i in range(4)],
            _stock("300001", "今三板A", 3),
            _stock("400001", "今四板A", 4),
        ],
        blowups=[_stock("500001", "炸板A", 1)],
        limit_down=[],
        ok=True,
    )
    res = analyze_ladder_and_cycle(today, yesterday=yest)
    assert res.max_streak == 4
    assert res.has_fault_line is False
    assert len(res.progressions) >= 1
    prog = next(p for p in res.progressions if p.from_streak == 1)
    assert prog.rate == 0.40
    assert res.cycle_stage == "发酵期"
    assert "积极进攻" in res.action_advice


def test_fault_line_detected_when_middle_tier_missing() -> None:
    # 最高 5 板，但 2、3 板断层
    today = LimitPoolSnapshot(
        trade_date=date(2026, 9, 15),
        limit_up=[
            _stock("000001", "首板A", 1),
            _stock("000002", "五板妖股", 5),
        ],
        ok=True,
    )
    res = analyze_ladder_and_cycle(today)
    assert res.has_fault_line is True
    assert any("中位梯队断层" in n for n in res.notes)


def test_retreat_stage_when_blowup_and_drop_high() -> None:
    # 退潮期：跌停 8 家，炸板率 50%
    today = LimitPoolSnapshot(
        trade_date=date(2026, 9, 15),
        limit_up=[_stock("000001", "高标A", 3)],
        blowups=[_stock("000002", "炸板A", 2)],
        limit_down=[_stock(f"6000{i:02d}", f"跌停{i}", 0) for i in range(8)],
        ok=True,
    )
    res = analyze_ladder_and_cycle(today)
    assert res.cycle_stage == "退潮期"
    assert "防守为先" in res.action_advice
    assert res.emotion_score <= 40.0
