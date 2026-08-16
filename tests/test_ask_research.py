"""`ask --deep`：分步研究。

这一层的价值不在「多问几次模型」，在**把结论从模型手里拿走**：结论标签、排序、
触发与失效条件全部由规则算，模型只在每张卡下面写一段论证。所以测试也分两半：

- **规则那一半**（`verdict.py`）——判据对不对，是这里最该被钉死的东西。
- **编排那一半**（`plan.py` / `runner.py`）——一步坏掉会不会拖垮其余，
  以及不支持的问题有没有被明确标成不支持，而不是让模型去补。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from ashare_decision_support.ask import assemble, run_ask
from ashare_decision_support.ask.plan import MAX_SYMBOL_STEPS, build_plan
from ashare_decision_support.ask.question import (
    INTENT_ENTRY,
    INTENT_EXIT,
    INTENT_GAP,
    parse_question,
)
from ashare_decision_support.ask.verdict import (
    LABEL_ACTIONABLE,
    LABEL_BLOCKED,
    LABEL_CONFIRM,
    SIDE_SELL,
    assess,
    from_context,
    rank,
)
from ashare_decision_support.ask.visual import price_ladder
from ashare_decision_support.state import Positions


def payload(
    symbol: str = "600519.SH",
    name: str = "贵州茅台",
    last: float = 23.16,
    entry: tuple[float, float] = (22.80, 23.30),
    stop: float = 22.15,
    targets: list[float] | None = None,
    rrr: float | None = 2.4,
    score: float = 61.0,
    warnings: list[str] | None = None,
    book: dict | None = None,
    intraday: dict | None = None,
    current: bool = True,
) -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "score": score,
        "last_price": last,
        "session_date": "2026-08-03",
        "is_current_session": current,
        "trend_state": "上升",
        "entry_low": entry[0],
        "entry_high": entry[1],
        "stop": stop,
        "targets": targets if targets is not None else [25.40],
        "rrr": rrr,
        "warnings": warnings or [],
        "intraday": intraday if intraday is not None else {"open": 23.00, "volume_ratio": 0.9},
        "order_book": book or {},
        "adjudication": None,
    }


class StubClient:
    """每一步都回同一句话。默认那句里没有任何数字，闸门放行。"""

    configured = True

    def __init__(self, reply: str = "读数支持该结论。", replies: dict[str, str] | None = None) -> None:
        self.reply = reply
        self.replies = replies or {}
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        body = messages[-1]["content"]
        for needle, reply in self.replies.items():
            if needle in body:
                return reply
        return self.reply


# ---- 问题解析 -------------------------------------------------------------


def test_codes_and_known_names_both_resolve() -> None:
    spec = parse_question(
        "丸美生物和 688363 今天低开了，该不该止损",
        known_names={"丸美生物": "603983.SH"},
    )

    assert spec.symbols == ["688363.SH", "603983.SH"]
    assert INTENT_GAP in spec.intents and INTENT_EXIT in spec.intents


def test_a_name_the_system_does_not_know_is_named_out_loud() -> None:
    """猜一个代码比认不出来危险得多：后面整条链路会拿另一只票的数据回答你。"""
    spec = parse_question("华熙生物今天怎么样", known_names={})

    assert spec.symbols == []
    assert "华熙生物" in spec.unresolved
    assert any("华熙生物" in note for note in spec.notes)


@pytest.mark.parametrize("text", ["20260802 的复盘", "成本 1350.60 要不要走", "涨了 12.5%"])
def test_six_digit_lookalikes_are_not_symbols(text: str) -> None:
    """日期、金额、百分比里都有数字串。认错一个，就会去取一只不存在的票。"""
    assert parse_question(text).symbols == []


def test_an_unrecognised_question_falls_back_to_the_phase_default() -> None:
    spec = parse_question("嗯", fallback_intents=[INTENT_ENTRY])

    assert spec.intents == [INTENT_ENTRY]
    assert any("默认关注点" in note for note in spec.notes)


# ---- 决策卡：判据 ---------------------------------------------------------


def test_price_inside_the_band_with_enough_rrr_is_actionable() -> None:
    decision = assess(payload(last=23.00))

    assert decision.label == LABEL_ACTIONABLE
    assert any("已在买入带" in item for item in decision.triggers)
    assert any("跌破止损" in item for item in decision.invalidations)


def test_price_above_the_band_needs_a_pullback_first() -> None:
    decision = assess(payload(last=24.50))

    assert decision.label == LABEL_CONFIRM
    assert any("回落进买入带" in item for item in decision.triggers)


def test_price_below_the_band_is_a_breakdown_not_a_discount() -> None:
    """低开砸穿回踩带之后，那条带就不是「更便宜的买点」了——画它的依据已经没了。

    和 `advice/candidates.py` 对同一情形的措辞必须一致，否则同一件事在两个地方
    有两种说法，读的人会以为是两回事。
    """
    decision = assess(payload(last=22.40))

    assert decision.label == LABEL_CONFIRM
    assert any("失守" in item for item in decision.invalidations)
    assert any("站回" in item for item in decision.triggers)


def test_a_risk_warning_blocks_the_whole_card() -> None:
    decision = assess(payload(warnings=["⚠️ 近 30 日内有风险公告：立案调查"]))

    assert decision.label == LABEL_BLOCKED
    assert decision.blockers == ["近 30 日内有风险公告：立案调查"]
    # 被阻断就不该再给触发条件——给了就等于说「满足这个就能做」，而它做不了。
    assert decision.triggers == []


def test_stale_data_blocks_the_card_even_when_the_levels_look_fine() -> None:
    decision = assess(payload(current=False))

    assert decision.label == LABEL_BLOCKED
    assert any("不是当日盘中" in item for item in decision.blockers)


def test_a_thin_rrr_is_not_actionable() -> None:
    assert assess(payload(rrr=1.1, last=23.00)).label == LABEL_CONFIRM


def test_sealed_at_the_upper_limit_downgrades_instead_of_blocking() -> None:
    """「到位了」和「动得了」是两个问题。区间是真的进了，但此刻买不进。"""
    decision = assess(
        payload(last=23.00, book={"at_limit_up": True, "limit_up": 23.00, "is_degenerate": True})
    )

    assert decision.label == LABEL_CONFIRM
    assert any("买不进" in item for item in decision.triggers)


def test_a_missing_limit_price_is_not_treated_as_sealed() -> None:
    """未知不算封板。一个缺失字段悄悄压掉本该给出的结论，比给错还难查。"""
    assert assess(payload(last=23.00, book={})).label == LABEL_ACTIONABLE


# ---- 决策卡：持仓侧 -------------------------------------------------------


def test_a_position_switches_the_card_to_the_sell_side() -> None:
    decision = assess(payload(last=23.16), holding={"cost_price": 22.00, "plan_stop": 22.50})

    assert decision.side == SIDE_SELL
    assert decision.holding["生效止损"] == 22.50
    assert decision.holding["止损来源"] == "手填"
    assert decision.holding["浮动盈亏"] == pytest.approx(0.0527, abs=1e-4)


def test_a_manual_stop_beats_the_computed_one() -> None:
    """手填的是决定，系统算的是建议。同 `monitor/holdings.py:effective_stop`。"""
    decision = assess(payload(stop=22.15), holding={"cost_price": 22.0, "plan_stop": 23.00})

    assert decision.stop == 23.00
    assert decision.holding["止损来源"] == "手填"
    # 现价 23.16 还在 23.00 之上，两个方向都没触发。
    assert decision.label == LABEL_CONFIRM
    assert any("23.0" in item for item in decision.invalidations)


def test_a_sell_signal_that_cannot_be_filled_says_so() -> None:
    """该卖但卖不出，是一个必须说出口的状态：不是「不用管」，是「今天做不到」。"""
    decision = assess(
        payload(last=20.00, book={"at_limit_down": True, "limit_down": 20.00}),
        holding={"cost_price": 22.0, "plan_stop": 21.0},
    )

    assert decision.label == LABEL_BLOCKED
    assert any("卖不出" in item for item in decision.blockers)


def test_the_gap_against_yesterdays_close_reaches_the_card() -> None:
    """低开幅度问的是今开对**昨收**，不是现价对今开。两者混用会把「低开 3% 涨回来」
    和「平开一路阴跌 3%」说成同一件事。"""
    decision = assess(payload(intraday={"open": 22.50, "open_gap_pct": -0.0312}))

    assert any("低开" in item and "-3.12%" in item for item in decision.readings)


# ---- 排序 -----------------------------------------------------------------


def test_ranking_is_deterministic_and_puts_actionable_first() -> None:
    cards = [
        assess(payload(symbol="000001.SZ", warnings=["⚠️ ST"])),
        assess(payload(symbol="600000.SH", last=23.00, rrr=1.8)),
        assess(payload(symbol="600519.SH", last=23.00, rrr=3.0)),
        assess(payload(symbol="300750.SZ", last=24.90)),
    ]
    ordered = [item.symbol for item in rank(cards)]

    assert ordered[:2] == ["600519.SH", "600000.SH"]  # 可执行档内按盈亏比
    assert ordered[-1] == "000001.SZ"  # 被阻断的垫底
    assert [item.symbol for item in rank(list(reversed(cards)))] == ordered


# ---- 计划 -----------------------------------------------------------------


def test_a_question_the_system_has_no_data_for_becomes_a_declared_gap() -> None:
    """问基本面时，报告里要出现「本系统没有这项数据」，而不是一段像模像样的
    营收分析。这一步**不进模型**——不给它编的机会比叮嘱它别编可靠。"""
    spec = parse_question("华熙生物的营收和净利润怎么样", known_names={})
    plan = build_plan(spec, {}, [])

    gaps = [step for step in plan.steps if not step.supported]
    assert any("基本面" in step.title for step in gaps)
    assert all(step.key.startswith("gap:") is False for step in plan.runnable)


def test_deep_mode_keeps_question_parse_notes_in_the_result() -> None:
    report, result = _deep("华熙生物今天低开了怎么办", _facts(payload(last=23.00)), StubClient())

    assert "华熙生物" in report or any("华熙生物" in note for note in result["notes"])
    assert any("华熙生物" in note for note in result["notes"])


def test_symbol_steps_are_capped_and_the_drop_is_reported() -> None:
    """静默截断读起来和「全都看过了」一模一样。"""
    cards = [assess(payload(symbol=f"60000{i}.SH", last=23.00)) for i in range(6)]
    plan = build_plan(parse_question("这几只怎么样"), {}, cards)

    symbol_steps = [step for step in plan.steps if step.key.startswith("symbol:")]
    assert len(symbol_steps) == MAX_SYMBOL_STEPS
    assert any("仍进速览表与排序" in note for note in plan.notes)


def test_each_symbol_step_only_carries_its_own_facts() -> None:
    """一步看见别的票的读数，就等于把「分步」退回成一次全量调用。"""
    cards = [
        assess(payload(symbol="600519.SH", name="贵州茅台")),
        assess(payload(symbol="000001.SZ", name="平安银行")),
    ]
    plan = build_plan(parse_question("这两只怎么样"), {}, cards)

    step = next(item for item in plan.steps if item.key == "symbol:600519.SH")
    text = json.dumps(step.facts, ensure_ascii=False)
    assert "600519.SH" in text
    assert "000001.SZ" not in text


# ---- 分步执行 -------------------------------------------------------------


def _facts(*items: dict) -> dict:
    return {"trade_date": "2026-08-03", "symbols": list(items)}


def _deep(question: str, facts: dict, client, symbols=("600519.SH",), **kwargs) -> tuple[str, dict]:
    """标的显式传进来——自选股删掉之后，`ask` 只看问题里点名的和持仓那几只。"""
    printed: list[str] = []
    result = run_ask(
        question=question,
        symbols=list(symbols),
        now=datetime(2026, 8, 3, 10, 30),
        is_trading_day=True,
        deep=True,
        compute_symbols=lambda symbols: facts,
        client=client,
        write=printed.append,
        **kwargs,
    )
    return printed[0], result


def test_one_discarded_step_costs_only_that_step() -> None:
    """原来是一次调用产出整个答案：一个追溯不到的数字下去，整段丢弃。
    现在丢的只是那一步的论证——卡、排序、价位轴全部由规则产生，一个字不少。"""
    facts = _facts(
        payload(symbol="600519.SH", name="贵州茅台", last=23.00),
        payload(symbol="000001.SZ", name="平安银行", last=23.00),
    )
    client = StubClient(replies={"600519.SH": "目标看到 999.99 元。"})

    report, result = _deep(
        "这两只怎么样", facts, client, symbols=("600519.SH", "000001.SZ")
    )

    assert "论证段缺失：含数据中不存在的数字" in report
    # 被丢弃的**句子**不能出现；那个数字本身会出现在「丢了什么」的说明里，
    # 那是必须留的——不说清丢了什么，缺失就变成了沉默。
    assert "目标看到" not in report
    # 被丢掉那一只的结论与价位照出。
    assert "600519.SH" in report and "23.0" in report
    assert result["steps_run"] < result["steps_total"]


def test_a_dead_provider_still_produces_the_whole_deterministic_report() -> None:
    class Boom:
        configured = True

        def chat(self, messages):
            raise RuntimeError("provider down")

    report, _ = _deep("怎么看", _facts(payload(last=23.00)), Boom())

    assert "## 先看结论" in report
    assert "可执行" in report
    assert "调用失败" in report


def test_without_a_key_the_cards_and_the_ladder_still_come_out() -> None:
    """没配 key 不是降级兜底，是这套系统一贯的分层：数字只在确定性层产生。"""

    class NoKey:
        configured = False

        def chat(self, messages):  # pragma: no cover - 不该被调用
            raise AssertionError("不该调用模型")

    report, result = _deep("怎么看", _facts(payload(last=23.00)), NoKey())

    assert "结论" in report and "止损" in report
    assert result["steps_run"] == 0
    assert any("未配置 LLM key" in note for note in result["notes"])


def test_only_the_named_symbols_get_recomputed() -> None:
    """问一只票不该把上下文里的其余标的全算一遍——那是几十次 HTTP，
    而其中绝大多数不会出现在答案里。"""
    Positions().open("000001.SZ", cost_price=12.0)
    Positions().open("300750.SZ", cost_price=200.0)
    seen: list[list[str]] = []

    def compute(symbols: list[str]) -> dict:
        seen.append(list(symbols))
        return _facts(payload(symbol="600519.SH", last=23.00))

    run_ask(
        question="600519 怎么样",
        now=datetime(2026, 8, 3, 10, 30),
        is_trading_day=True,
        deep=True,
        compute_symbols=compute,
        client=StubClient(),
        write=lambda _: None,
    )

    assert seen == [["600519.SH"]]


def test_the_conclusion_comes_first() -> None:
    """「没重点」的解法不是写得更短，是把结论放在读的人第一眼的位置。"""
    report, _ = _deep("怎么看", _facts(payload(last=23.00)), StubClient())

    assert report.index("## 先看结论") < report.index("## 逐只决策卡")
    assert "可执行" in report.split("## 先看结论", 1)[1]


def test_the_report_stays_natural_when_there_are_no_decisions() -> None:
    """没有决策卡时也不该回到编号式目录。"""
    report, _ = _deep("大盘怎么样", _facts(), StubClient())

    assert "## 先看结论" in report
    assert "## 一、" not in report
    assert "## 逐只决策卡" not in report


# ---- 可视化 ---------------------------------------------------------------


def test_the_ladder_keeps_every_label_readable() -> None:
    """两个标签挨在一起会排成「现价买入带」，读起来是一个词，而它是两个位置。"""
    lines = price_ladder(assess(payload(last=23.16)))
    labels = lines[1]

    for text in ("止损", "现价", "买入带", "目标"):
        assert text in labels
    assert "现价买入带" not in labels


def test_a_symbol_without_levels_says_so_instead_of_drawing_a_flat_line() -> None:
    card = assess(payload(entry=(None, None), stop=None, targets=[], rrr=None))
    assert "画不出" in price_ladder(card)[0]


# ---- 装配 -----------------------------------------------------------------


def test_holdings_reach_the_sell_side_through_the_assembled_context() -> None:
    """`context.py` 把持仓摊平成中文字段名，`state.Position` 用的是英文名。对不上时
    不会报错，只会**静默**走成买入侧——而持仓最需要的恰恰是卖点那一套。"""
    Positions().open("600519.SH", cost_price=20.00, plan_stop=22.90, name="贵州茅台")

    ctx = assemble(
        datetime(2026, 8, 3, 10, 30),
        compute_symbols=lambda symbols: _facts(payload(last=23.16)),
    )
    decisions, source = from_context(ctx.blocks)

    assert source == "实时买卖点"
    assert decisions[0].side == SIDE_SELL
    assert decisions[0].holding["成本价"] == 20.00
