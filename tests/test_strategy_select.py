"""选股段的测试：门槛、漏斗说明、以及「为什么不买」有没有被说出来。

这一段此前散在 `jobs/morning.py` 里，`jobs/post_close.py` 则完全绕过它。收成一个
模块之后，三条链路的行为第一次可以被同一组断言钉住。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ashare_decision_support.models.enums import EmotionCycle, Playbook
from ashare_decision_support.strategy.regime import classify_regime
from ashare_decision_support.strategy.select import (
    MIN_SETUP_CANDIDATES,
    analyze_universe,
    evaluate_universe,
    select_from,
)


def make_bars(
    symbol: str,
    count: int = 140,
    start_price: float = 20.0,
    drift: float = 0.15,
    volume: int = 100_000,
    volume_drift: int = 100,
) -> list[dict]:
    bars: list[dict] = []
    price = start_price
    day = date(2026, 1, 5)
    for index in range(count):
        price = max(price + drift, 1.0)
        size = max(volume + index * volume_drift, 1)
        bars.append(
            {
                "symbol": symbol,
                "timestamp": datetime.combine(day + timedelta(days=index), datetime.min.time()),
                "open": round(price - 0.1, 2),
                "high": round(price + 0.3, 2),
                "low": round(price - 0.4, 2),
                "close": round(price, 2),
                "volume": size,
                "amount": size * price * 100,
            }
        )
    return bars


def test_analysis_is_computed_once_and_reused_across_scoring_passes() -> None:
    """两遍打分共用一份结构读数——这是把 analyze 和 select 拆开的全部理由。"""
    bars = {"AAA.SH": make_bars("AAA.SH"), "BBB.SZ": make_bars("BBB.SZ", start_price=30.0)}
    analysis = analyze_universe(bars)

    assert set(analysis.snapshots) == {"AAA.SH", "BBB.SZ"}
    assert set(analysis.dips) == {"AAA.SH", "BBB.SZ"}
    assert analysis.scope == ["AAA.SH", "BBB.SZ"]

    coarse = select_from(analysis, require_playbook=False)
    fine = select_from(analysis, scope=["AAA.SH"], require_playbook=False)

    assert len(coarse.selected) + len(coarse.rejected) == 2
    assert len(fine.selected) + len(fine.rejected) == 1


def test_empty_and_barless_input_is_survivable() -> None:
    assert analyze_universe({}).scope == []
    assert analyze_universe({"X.SH": []}).scope == []
    assert evaluate_universe({}).selected == []


def test_freezing_regime_produces_no_candidates_at_all() -> None:
    """冰点档一条打法都不允许——不是把仓位压低，是不出手。"""
    regime = classify_regime(blowup_rate=0.9, max_streak=1, limit_up_count=3, limit_down_count=40)
    assert regime.cycle is EmotionCycle.FREEZING and regime.halted

    result = evaluate_universe({"AAA.SH": make_bars("AAA.SH")}, regime=regime)

    assert result.selected == []
    assert any("不产出候选" in note for note in result.notes)


def test_require_playbook_false_annotates_instead_of_filtering() -> None:
    """自选股与个股问答走这条路：没有打法是一个结论，不是让它消失的理由。"""
    # 一路下跌：DOWNTREND 只允许 REVERSAL，而它需要一个反转触发。
    bars = {"FALL.SH": make_bars("FALL.SH", start_price=60.0, drift=-0.25)}

    filtered = evaluate_universe(bars, require_playbook=True)
    annotated = evaluate_universe(bars, require_playbook=False)

    assert filtered.selected == []
    # 标注模式下这只票仍然被打分，只是落在 rejected 一侧。
    assert [item.symbol for item in annotated.rejected] == ["FALL.SH"]
    assert annotated.rejected[0].score > 0
    assert annotated.rejected[0].playbook is None


def test_reject_reasons_survive_serialisation() -> None:
    """`to_dict()` 此前不带 rejected，于是「为什么不买」在台账与卡上全都看不到。"""
    bars = {"FALL.SH": make_bars("FALL.SH", start_price=60.0, drift=-0.25)}
    result = evaluate_universe(bars, require_playbook=False)

    item = result.rejected[0]
    assert item.match.reject_reasons, "判据链必须给出拒绝理由"

    row = item.to_row()
    reasons = [text for entry in row["setup"]["rejected"] for text in entry["reasons"]]
    assert reasons == item.match.reject_reasons


def test_row_shape_carries_everything_the_journal_reads() -> None:
    """台账按字段名读这一行，改名不会报错、只会让统计静默分叉。"""
    result = evaluate_universe({"AAA.SH": make_bars("AAA.SH")}, require_playbook=False)
    row = (result.selected + result.rejected)[0].to_row()

    for key in (
        "symbol", "name", "sector", "score", "reasons", "warnings",
        "last_close", "trend_state", "stop_reference", "target_reference",
        "wyckoff_bias", "news", "dip", "playbook", "setup",
    ):
        assert key in row, key


def test_funnel_note_reports_how_many_were_blocked() -> None:
    """被挡掉多少必须说出来——只报「选出 N 只」会读成「剩下的都不够好」。"""
    bars = {
        "UP.SH": make_bars("UP.SH"),
        "FALL.SH": make_bars("FALL.SH", start_price=60.0, drift=-0.25),
    }
    result = evaluate_universe(bars)

    joined = " ".join(result.notes)
    assert "入围" in joined or "不产出候选" in joined
    # 满足判据的数量必然远低于最低样本，这条提醒必须在。
    assert any(str(MIN_SETUP_CANDIDATES) in note for note in result.notes) or not result.selected


def test_regime_narrows_the_allowed_playbooks() -> None:
    """趋势决定这只票能用哪些打法，情绪周期决定今天允许哪些，取交集。"""
    ebb = classify_regime(blowup_rate=0.55, max_streak=3, limit_up_count=30, limit_down_count=2)
    assert ebb.cycle is EmotionCycle.EBB
    assert Playbook.BREAKOUT not in ebb.allowed

    bars = {"UP.SH": make_bars("UP.SH")}
    unconstrained = evaluate_universe(bars, require_playbook=False)
    constrained = evaluate_universe(bars, regime=ebb, require_playbook=False)

    item = (constrained.selected + constrained.rejected)[0]
    # 突破在 UPTREND 下本来允许，退潮档把它挡掉了——这件事必须留痕。
    blocked = item.to_row()["setup"]["blocked_by_regime"]
    assert Playbook.BREAKOUT.value in blocked
    assert unconstrained.matches["UP.SH"].blocked_by_regime == []


# ---- 筹码按需计算 -----------------------------------------------------------


def test_chips_are_computed_only_for_the_symbols_that_get_scored() -> None:
    """筹码是这条链最贵的一项，却不进门槛也不进打分——只该为打了分的那批算。

    钉住的是一个**性能契约**，不是实现细节：`analyze_universe` 一次算完的写法
    会为几百只过不了门槛的票各算一遍再扔掉（实测占该函数 57%）。这条测试保证
    它不会被无意改回去。
    """

    bars = {f"{600000 + i}.SH": make_bars(f"{600000 + i}.SH") for i in range(6)}
    shares = dict.fromkeys(bars, 2.0e8)

    analysis = analyze_universe(bars, shares)
    # 分析阶段一只都不算。
    assert analysis.chip_cache == {}

    result = select_from(analysis, require_playbook=False)
    scored = {item.symbol for item in result.selected + result.rejected}
    assert scored, "这组样本应当至少打出一只，否则这条测试什么也没验证"
    assert set(analysis.chip_cache) == scored


def test_lazy_chips_match_computing_them_directly() -> None:
    """按需算出来的必须和直接调 `estimate_chips` 逐字段相同。"""
    from ashare_decision_support.signals.chips import estimate_chips

    rows = make_bars("600001.SH")
    analysis = analyze_universe({"600001.SH": rows}, {"600001.SH": 2.0e8})
    assert analysis.chips_of("600001.SH").to_dict() == estimate_chips(rows, 2.0e8).to_dict()


def test_chips_are_remembered_so_two_scoring_passes_do_not_recompute() -> None:
    """morning 粗排 + 精排两遍共用同一份读数，筹码不该算两次。"""

    analysis = analyze_universe({"600001.SH": make_bars("600001.SH")}, {"600001.SH": 2.0e8})
    first = analysis.chips_of("600001.SH")
    assert analysis.chips_of("600001.SH") is first


def test_chips_of_declines_for_a_symbol_with_no_bars() -> None:

    analysis = analyze_universe({"600001.SH": make_bars("600001.SH")}, {"600001.SH": 2.0e8})
    assert analysis.chips_of("999999.SZ") is None
