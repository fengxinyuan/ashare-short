"""L2 候选层与 L3 裁决层。

这两层的价值全在**约束**上，所以测试也全在约束上：候选集的默认答案必须和改造
前逐位相同；模型必须造不出候选之外的价；越界必须整条丢弃而不是打补丁。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime

from ashare_decision_support.advice import AdviceEngine, LevelAdjudicator
from ashare_decision_support.advice.adjudicate import offending_numbers, rule_default
from ashare_decision_support.advice.candidates import (
    BASIS_BID_WALL,
    BASIS_BREAKOUT_TRIGGER,
    BASIS_VWAP,
    SIDE_ENTRY,
    SIDE_STOP,
    build_candidates,
)
from ashare_decision_support.advice.engine import StockAdvice
from ashare_decision_support.jobs.advise import _risk_qualified_plan
from ashare_decision_support.models.enums import Playbook, TrendState
from ashare_decision_support.models.schemas import DailyTechnicalSnapshot, PriceLevel
from ashare_decision_support.notify.render import render_advice_brief
from ashare_decision_support.signals.intraday import IntradayAnalyzer
from ashare_decision_support.signals.orderflow import read_order_book
from ashare_decision_support.strategy.playbook import PlaybookMatch, SetupVerdict
from tests.test_intraday_advice_chat import bars_from, minute_rows
from tests.test_orderflow import snapshot as book_snapshot

# fixtures


def intraday_for(rows=None, today=date(2026, 7, 31)):
    return IntradayAnalyzer(today=today).analyze(
        "600519.SH", bars_from(rows if rows is not None else minute_rows("2026-07-31", 48))
    )


def daily_for(
    last_close: float,
    supports: list[float],
    resistances: list[float],
    *,
    trend_state: TrendState = TrendState.UPTREND,
    ma5: float | None = None,
    ma10: float | None = None,
    ma20: float | None = None,
    ma60: float | None = None,
    volume_ratio_20d: float | None = None,
):
    def level(side: str, price: float) -> PriceLevel:
        return PriceLevel(
            side=side, price=price, touches=2, strength=0.6, distance_pct=0.05, label=f"{side}"
        )

    return DailyTechnicalSnapshot(
        symbol="600519.SH",
        bar_count=120,
        last_close=last_close,
        last_open=last_close * 0.99,
        last_high=last_close * 1.01,
        last_low=last_close * 0.98,
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        ma60=ma60,
        volume_ratio_20d=volume_ratio_20d,
        trend_state=trend_state,
        supports=[level("support", price) for price in supports],
        resistances=[level("resistance", price) for price in resistances],
        explanation="测试",
        source_ts=datetime(2026, 7, 31, 15, 0),
    )


class StubClient:
    """按顺序回放固定回复的假 LLM。"""

    def __init__(self, *replies: str, configured: bool = True) -> None:
        self.replies = list(replies)
        self.configured = configured
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else ""


class BoomClient:
    configured = True

    def chat(self, messages: list[dict]) -> str:
        raise RuntimeError("provider down")


# L2 候选集


def test_rank_zero_reproduces_the_rule_engine_exactly() -> None:
    """**这是整套改造的兼容性契约。**

    候选集的默认答案必须和改造前逐位相同。差一分钱，台账里改造前后的记录就
    不再可比——而那份可比性是这套持续学习机制唯一的资产，重跑不回来。
    """
    daily = daily_for(10.48, supports=[9.5], resistances=[11.0])
    advice = AdviceEngine().build("600519.SH", daily, intraday_for())

    assert advice.candidates is not None
    assert advice.candidates.default(SIDE_ENTRY).price == advice.entry_low
    assert advice.candidates.default(SIDE_STOP).price == advice.stop
    assert advice.candidates.default(SIDE_ENTRY).basis == BASIS_VWAP


def test_the_engines_own_levels_are_always_inside_its_own_candidate_set() -> None:
    """扫一批不同形态的行情，钉死同一条不变量。

    单个例子过不了这一关：距离护栏（现价下方 6% / 9%）本来会把规则自己算出来
    的止损筛掉，而那只在跌得够深的那些日子里发生——恰好是最需要止损位的时候。
    第一版就是这么错的，被单元测试逮到之前它在温和行情里一切正常。

    不变量一旦破了，后果是连锁的：L3 的闸门会把引擎自己报的价判成越界，台账
    里的 `rule_stop` 会和 `stop_price` 对不上，两处都不会报错。
    """
    scenarios = [
        ("平稳上行", minute_rows("2026-07-31", 48, 10.0, 0.01)),
        ("单边下跌", minute_rows("2026-07-31", 48, 12.0, -0.03)),
        ("急跌", minute_rows("2026-07-31", 48, 20.0, -0.12)),
        ("横盘", minute_rows("2026-07-31", 48, 8.0, 0.0)),
        ("急涨", minute_rows("2026-07-31", 48, 5.0, 0.05)),
    ]
    for label, rows in scenarios:
        for supports in ([], [9.5], [5.0], [11.9]):
            advice = AdviceEngine().build(
                "600519.SH",
                daily_for(rows[-1]["close"], supports=supports, resistances=[99.0]),
                intraday_for(rows),
            )
            if advice.candidates is None:
                continue
            prices = advice.candidates.prices()
            assert advice.entry_low in prices, f"{label}/{supports} 的买入价不在候选集里"
            assert advice.stop in prices, f"{label}/{supports} 的止损价不在候选集里"
            assert advice.candidates.default(SIDE_ENTRY).price == advice.entry_low
            assert advice.candidates.default(SIDE_STOP).price == advice.stop


def test_every_candidate_carries_where_the_price_came_from() -> None:
    """没有出处的价位事后无法归因：报出来的 22.95 到底是均价还是支撑，说不清。"""
    candidates = build_candidates(
        symbol="600519.SH",
        last_price=20.0,
        vwap=19.8,
        session_low=19.5,
        session_high=20.2,
        span=0.7,
        daily_supports=[19.0],
        daily_resistances=[21.0],
        entry_band=19.7,
        rule_stop=19.4,
    )

    assert candidates.entries and candidates.stops and candidates.targets
    for candidate in candidates.all():
        assert candidate.basis
        assert candidate.key().startswith(candidate.side)


def test_the_order_book_contributes_the_only_candidate_that_exists_right_now() -> None:
    """其余候选都是从历史推出来的位置，只有挂单墙是**当下**的事实。"""
    book = read_order_book(
        book_snapshot(
            bids=[(19.60, 500000.0), (19.59, 1000.0)],
            asks=[(20.01, 10000.0)],
            last=20.0,
        )
    )
    candidates = build_candidates(
        symbol="600519.SH",
        last_price=20.0,
        vwap=19.8,
        session_low=19.5,
        session_high=20.2,
        span=0.7,
        book=book,
        entry_band=19.7,
        rule_stop=19.4,
    )

    wall = next(item for item in candidates.entries if item.basis == BASIS_BID_WALL)
    assert wall.price == 19.60
    # 出处自带告诫。挂单可撤，这个候选不能被当成承接证据。
    assert "可撤" in wall.evidence["caveat"]


def test_a_price_above_the_last_is_never_an_extra_buy_candidate() -> None:
    """低吸口径下，高于现价的「买入价」是追价，不是买入价。

    唯一的例外是规则自己的默认值——它必须无条件进候选集（见
    `test_rank_zero_reproduces_the_rule_engine_exactly`），但会被显式标注成
    「已跌穿回踩带」而不是伪装成一个正常的低吸位。
    """
    candidates = build_candidates(
        symbol="600519.SH",
        last_price=20.0,
        vwap=20.5,
        session_low=19.9,
        session_high=21.0,
        span=1.1,
        daily_supports=[19.5],
        entry_band=20.6,
        rule_stop=19.8,
    )

    extras = [item for item in candidates.entries if item.default_rank > 0]
    assert extras and all(item.price <= 20.0 for item in extras)
    assert candidates.entries[0].price == 20.6
    assert any("已跌穿回踩区间" in note for note in candidates.notes)


def test_breakout_uses_the_nearby_session_high_as_its_default_trigger() -> None:
    """突破入场等确认越过触发位，不能沿用低吸的均价回踩带。"""
    daily = daily_for(10.48, supports=[9.5], resistances=[11.0])
    intraday = intraday_for()
    match = PlaybookMatch(
        trend=TrendState.UPTREND,
        matched=[SetupVerdict(Playbook.BREAKOUT, True, ["突破确认"])],
    )

    advice = AdviceEngine().build("600519.SH", daily, intraday, match=match)

    assert advice.playbook == Playbook.BREAKOUT.value
    assert advice.entry_low == intraday.session_high
    assert advice.entry_high == intraday.session_high
    assert advice.candidates is not None
    assert advice.candidates.default(SIDE_ENTRY).basis == BASIS_BREAKOUT_TRIGGER
    assert advice.candidates.default(SIDE_ENTRY).price == intraday.session_high
    assert any(item.basis == BASIS_VWAP for item in advice.candidates.entries[1:])


def test_breakout_high_is_not_also_a_profit_target() -> None:
    """突破触发价等于首目标时没有正向收益空间，不能再写「可跟随」。"""
    daily = daily_for(
        10.48,
        supports=[9.5],
        resistances=[],
        ma5=10.2,
        ma10=10.1,
        ma20=10.0,
        ma60=9.9,
        volume_ratio_20d=1.5,
    )
    intraday = replace(intraday_for(), volume_ratio=1.2)
    match = PlaybookMatch(
        trend=TrendState.UPTREND,
        matched=[SetupVerdict(Playbook.BREAKOUT, True, ["突破确认"])],
    )

    advice = AdviceEngine().build("600519.SH", daily, intraday, match=match)

    assert advice.entry_low == intraday.session_high
    assert advice.targets == []
    assert advice.rrr is None
    assert advice.shortline.action == "wait_trigger"
    assert "暂不构成买点" in advice.shortline.summary
    assert any("没有有效目标位" in warning for warning in advice.warnings)


def test_advice_count_separates_levels_from_risk_qualified_plans() -> None:
    item = StockAdvice(
        symbol="600519.SH",
        entry_low=10.0,
        stop=9.0,
        targets=[10.1],
        rrr=0.1,
        playbook="突破",
        is_current_session=True,
    )
    assert item.has_actionable_levels
    assert not _risk_qualified_plan(item)
    item.rrr = 1.8
    assert not _risk_qualified_plan(item), "汇总计数必须复核目标与止损，不能只相信缓存的比值"
    item.targets = [12.0]
    assert _risk_qualified_plan(item)
    item.book = {"at_limit_up": True}
    assert not _risk_qualified_plan(item)


def test_candidates_far_from_the_last_price_are_dropped() -> None:
    """现价下方 6% 以外的「买入位」不是今天的价位，是下周的愿望。"""
    candidates = build_candidates(
        symbol="600519.SH",
        last_price=20.0,
        vwap=19.8,
        session_low=19.5,
        session_high=20.2,
        span=0.7,
        daily_supports=[15.0],  # -25%
        entry_band=19.7,
        rule_stop=19.4,
    )

    assert all(item.price >= 20.0 * 0.94 for item in candidates.entries)


# L3 数字闸门


def test_the_price_gate_rejects_a_neighbouring_price_that_rounding_would_accept() -> None:
    """**这是买卖点闸门比简报闸门严的全部理由。**

    简报那道闸门接受「按你写的精度四舍五入之后相等」，因为改写散文本来就在重排
    数字。价位不行：22.95 和 22.96 在任何带容差的比对里都能过，而它们是两个不同
    的挂单价。这里只接受数值相等。
    """
    candidates = build_candidates(
        symbol="600519.SH",
        last_price=23.0,
        vwap=22.95,
        session_low=22.9,
        session_high=23.2,
        span=0.3,
        entry_band=22.95,
        rule_stop=22.85,
    )
    facts = json.dumps(candidates.to_dict(), ensure_ascii=False)

    assert offending_numbers("挂在 22.95", candidates, facts) == []
    assert offending_numbers("挂在 22.96", candidates, facts) == ["22.96"]


def test_the_price_gate_lets_list_ordinals_through() -> None:
    candidates = build_candidates(
        symbol="600519.SH",
        last_price=23.0,
        vwap=22.95,
        session_low=22.9,
        session_high=23.2,
        span=0.3,
        entry_band=22.95,
        rule_stop=22.85,
    )
    facts = json.dumps(candidates.to_dict(), ensure_ascii=False)

    assert offending_numbers("第 3 档买盘最厚", candidates, facts) == []


# L3 裁决


def advice_with(client, rows=None, book=None):
    engine = AdviceEngine(adjudicator=LevelAdjudicator(client=client))
    daily = daily_for(10.48, supports=[9.5], resistances=[11.0])
    return engine.build("600519.SH", daily, intraday_for(rows), book=book)


def test_the_model_can_override_the_rules_ordering_within_the_candidate_set() -> None:
    base = AdviceEngine().build(
        "600519.SH", daily_for(10.48, supports=[9.5], resistances=[11.0]), intraday_for()
    )
    assert base.candidates is not None
    alternative = next(item for item in base.candidates.entries if item.price != base.entry_low)
    stop_key = base.candidates.default("stop").key()

    reply = json.dumps(
        {
            "action": "select",
            "entry": alternative.key(),
            "stop": stop_key,
            "target": None,
            "reason": "选更低的那个位置",
        }
    )
    advice = advice_with(StubClient(reply))

    assert advice.entry_low == alternative.price
    assert advice.adjudication is not None
    assert advice.adjudication.overrode_rule is True
    assert advice.adjudication.used_fallback is False
    # 规则那一版一并留着：哪个更准是实证问题，两套都得记下来才能回答。
    assert advice.adjudication.rule_entry.price == base.entry_low


def test_a_price_that_is_not_in_the_candidate_set_cannot_be_selected() -> None:
    """模型只有「指一个已有的数」的通道，没有「写一个数」的通道。"""
    reply = json.dumps(
        {
            "action": "select",
            "entry": "entry:凭空捏造:19.995",
            "stop": "stop:凭空捏造:19.0",
            "reason": "我觉得这个价好",
        }
    )
    advice = advice_with(StubClient(reply))

    assert advice.adjudication.used_fallback is True
    assert any("未能指认有效" in note for note in advice.warnings)


def test_a_reason_containing_an_untraceable_number_discards_the_whole_verdict() -> None:
    """不打补丁，整条丢。一段只有一半可信的理由，读的人分辨不出该信哪一半。"""
    base = AdviceEngine().build(
        "600519.SH", daily_for(10.48, supports=[9.5], resistances=[11.0]), intraday_for()
    )
    reply = json.dumps(
        {
            "action": "select",
            "entry": base.candidates.default("entry").key(),
            "stop": base.candidates.default("stop").key(),
            "reason": "支撑在 33.33 附近",
        }
    )
    advice = advice_with(StubClient(reply))

    assert advice.adjudication.used_fallback is True
    assert advice.entry_low == base.entry_low
    assert any("无法追溯的数字" in note for note in advice.warnings)


def test_abstention_clears_the_entry_but_keeps_the_stop() -> None:
    """「今天不该买」和「今天不该拿」是两个问题，模型回答的是前一个。

    已持仓的人仍然需要止损位——把它一起清掉等于用一个买入侧的结论把持仓侧的
    信息也删了。
    """
    base = AdviceEngine().build(
        "600519.SH", daily_for(10.48, supports=[9.5], resistances=[11.0]), intraday_for()
    )
    reply = json.dumps({"action": "abstain", "reason": "分时结构还没走稳"})
    advice = advice_with(StubClient(reply))

    assert advice.abstained is True
    assert advice.entry_low is None
    assert advice.rrr is None
    assert advice.stop == base.stop
    assert advice.has_actionable_levels is False
    assert any("今天不该买入" in warning for warning in advice.warnings)


def test_a_stop_above_the_entry_is_rejected_as_incoherent() -> None:
    base = AdviceEngine().build(
        "600519.SH", daily_for(10.48, supports=[9.5], resistances=[11.0]), intraday_for()
    )
    # 故意把止损位填进 entry、把买入位填进 stop。
    reply = json.dumps(
        {
            "action": "select",
            "entry": base.candidates.default("entry").key(),
            "stop": base.candidates.default("entry").key(),
            "reason": "换个顺序",
        }
    )
    advice = advice_with(StubClient(reply))

    assert advice.adjudication.used_fallback is True


def test_a_dead_provider_falls_back_to_the_rule_default() -> None:
    """LLM 不在关键路径上。挂了就退回改造前那套行为，链路照跑。"""
    base = AdviceEngine().build(
        "600519.SH", daily_for(10.48, supports=[9.5], resistances=[11.0]), intraday_for()
    )
    advice = advice_with(BoomClient())

    assert advice.entry_low == base.entry_low
    assert advice.stop == base.stop
    assert advice.adjudication.used_fallback is True
    assert any("裁决调用失败" in note for note in advice.warnings)


def test_no_api_key_means_the_rule_ordering_with_a_note_not_a_failure() -> None:
    advice = advice_with(StubClient(configured=False))

    assert advice.entry_low is not None
    assert advice.adjudication.used_fallback is True


def test_garbage_output_is_not_mistaken_for_an_abstention() -> None:
    """解析不了 ≠ 弃权。把它读成弃权，会让一次故障变成一条「今天不该买」。"""
    advice = advice_with(StubClient("模型今天心情不好"))

    assert advice.abstained is False
    assert advice.adjudication.used_fallback is True
    assert advice.entry_low is not None


def test_agreeing_with_the_rule_is_not_recorded_as_an_override() -> None:
    """模型选了和规则同一个候选，不算「模型做了决定」。

    统计里把它算进模型那一组，会掺进大量「模型没做决定」的样本，把差值稀释
    向零——那正好是最容易得出「加了也没变化」这种错误结论的方式。
    """
    base = AdviceEngine().build(
        "600519.SH", daily_for(10.48, supports=[9.5], resistances=[11.0]), intraday_for()
    )
    reply = json.dumps(
        {
            "action": "select",
            "entry": base.candidates.default("entry").key(),
            "stop": base.candidates.default("stop").key(),
            "target": base.candidates.default("target").key(),
            "reason": "规则给的位置就是最好的",
        }
    )
    advice = advice_with(StubClient(reply))

    assert advice.adjudication.used_fallback is False
    assert advice.adjudication.overrode_rule is False


def test_rule_default_needs_no_network_and_no_key() -> None:
    candidates = build_candidates(
        symbol="600519.SH",
        last_price=20.0,
        vwap=19.8,
        session_low=19.5,
        session_high=20.2,
        span=0.7,
        entry_band=19.7,
        rule_stop=19.4,
    )
    verdict = rule_default(candidates)

    assert verdict.used_fallback is True
    assert verdict.abstained is False
    assert verdict.entry.price == 19.7
    assert verdict.overrode_rule is False


def test_the_monitor_never_gets_an_adjudicator_by_default() -> None:
    """盘中告警要秒级时效。每标的一次 LLM 调用会把一轮 20 只变成几十秒。

    见 `docs/ARCHITECTURE.md` 的“输出闸门”：越紧急的输出，越不经过 LLM。
    """
    from ashare_decision_support.monitor.session import MonitorSession

    session = MonitorSession(adapter=None, intraday_adapter=None, session_date=date(2026, 7, 31))
    assert session.advice_engine.adjudicator is None


# 出处进简报

# basis 存进了 facts 和台账，但读简报的人两样都看不到。不印出来的话，
# 「22.95 是从分时均价来的还是从买盘墙来的」在唯一会被真人读到的那份产物里
# 仍然是缺的——而那正是把买卖点改成候选集的初衷。


def test_the_brief_prints_where_each_price_came_from() -> None:
    advice = AdviceEngine().build(
        "600519.SH", daily_for(10.48, supports=[9.5], resistances=[11.0]), intraday_for()
    )

    brief = render_advice_brief(date(2026, 8, 3), [advice])

    line = next(row for row in brief.splitlines() if row.startswith("- 价位出处"))
    assert BASIS_VWAP in line
    assert f"{advice.entry_low:.2f}" in line
    # 没挂裁决器时选择权全在规则手上，标签必须这么说——否则读的人会以为
    # 这组价是模型挑的。
    assert "规则默认排序" in line


def test_the_brief_lists_the_candidates_that_were_not_chosen() -> None:
    """「为什么不是那个价」只有列出备选才问得出口，也才复盘得了。"""
    advice = AdviceEngine().build(
        "600519.SH", daily_for(10.48, supports=[9.5], resistances=[11.0]), intraday_for()
    )
    assert advice.candidates is not None and len(advice.candidates.entries) > 1

    brief = render_advice_brief(date(2026, 8, 3), [advice])

    line = next(row for row in brief.splitlines() if row.startswith("- 未选的买入候选"))
    other = next(item for item in advice.candidates.entries if item.price != advice.entry_low)
    assert f"{other.price:.2f}" in line


def test_the_brief_says_when_the_model_overrode_the_rules_ordering() -> None:
    """规则默认和模型裁决在简报上必须长得不一样。两者兑现率哪个高是个实证
    问题，而实证的前提是读的人知道自己看的是哪一套。"""
    base = AdviceEngine().build(
        "600519.SH", daily_for(10.48, supports=[9.5], resistances=[11.0]), intraday_for()
    )
    alternative = next(item for item in base.candidates.entries if item.price != base.entry_low)
    reply = json.dumps(
        {
            "action": "select",
            "entry": alternative.key(),
            "stop": base.candidates.default(SIDE_STOP).key(),
            "target": None,
            "reason": "选更低的那个位置",
        }
    )

    brief = render_advice_brief(date(2026, 8, 3), [advice_with(StubClient(reply))])

    assert "模型推翻规则" in brief


def test_an_abstention_reaches_the_brief_as_an_abstention() -> None:
    """弃权是 L3 相对规则层最有价值的增量。它在简报里不能只表现为一个空的
    买入区间——那和「算不出来」长得一模一样。"""
    reply = json.dumps({"action": "abstain", "reason": "分时结构还没走稳"})

    brief = render_advice_brief(date(2026, 8, 3), [advice_with(StubClient(reply))])

    assert "模型弃权" in brief
    assert "今天不该买入" in brief


# 盘口进简报

# 监控那一侧早就会说「封涨停：此刻买不进」（`OrderBookReading.describe`），
# 买卖点简报却只在 warnings 里间接提一句委比退化。同一个事实在两份产物里，
# 一份是操作结论、一份是统计脚注——而简报恰恰是你照着去挂单的那一份。


def test_the_brief_states_that_a_sealed_board_cannot_be_filled() -> None:
    """封死涨停时，简报必须**明说买不进**，而不是照常给一个买入价。

    这条盯的是「到位了」和「动得了」的区分：价位可以是对的，动作依然做不成。
    只在 warnings 里写一句「委比恒为 +1.0」不解决问题——那说的是读数怎么理解，
    不是这笔单挂不挂得进。
    """
    sealed = read_order_book(
        book_snapshot(bids=[(10.60, 500000.0)], asks=[], last=10.60, limit_up=10.60)
    )
    advice = AdviceEngine().build(
        "600519.SH",
        daily_for(10.48, supports=[9.5], resistances=[11.0]),
        intraday_for(),
        book=sealed,
    )

    brief = render_advice_brief(date(2026, 8, 3), [advice])

    assert "封涨停" in brief
    assert "买不进" in brief


def test_the_brief_reuses_the_monitors_wording_for_the_book() -> None:
    """两处措辞必须同源。各写一份的下场是「监控说买不进、简报说可以挂单」——
    而这种自相矛盾恰恰在最需要相信系统的那一刻出现。
    """
    sealed = read_order_book(
        book_snapshot(bids=[(10.60, 500000.0)], asks=[], last=10.60, limit_up=10.60)
    )
    advice = AdviceEngine().build(
        "600519.SH",
        daily_for(10.48, supports=[9.5], resistances=[11.0]),
        intraday_for(),
        book=sealed,
    )

    assert advice.book_lines == sealed.describe()
    brief = render_advice_brief(date(2026, 8, 3), [advice])
    for line in sealed.describe():
        assert line in brief


def test_no_book_means_no_book_lines_rather_than_an_empty_bullet() -> None:
    """五档取不到是常态（免费档、非交易时段）。它只能让简报少两行，
    不能变成一个空的「- 」项。"""
    advice = AdviceEngine().build(
        "600519.SH", daily_for(10.48, supports=[9.5], resistances=[11.0]), intraday_for()
    )

    assert advice.book_lines == []
    assert "- \n" not in render_advice_brief(date(2026, 8, 3), [advice])


def test_trade_context_computes_turnover_from_float_shares() -> None:
    advice = AdviceEngine().build(
        "600519.SH",
        daily_for(
            10.48,
            supports=[9.5],
            resistances=[11.0],
            ma5=10.30,
            ma10=10.20,
            ma20=10.00,
            ma60=9.80,
            volume_ratio_20d=1.3,
        ),
        intraday_for(),
        instrument={"ext": {"float_shares": 100000.0}},
    )

    assert advice.trade_context is not None
    assert advice.trade_context.turnover_ratio == 48.0
    assert advice.trade_context.session_amount is not None
    assert advice.shortline is not None
    assert any("换手率 48.00%" in reason for reason in advice.shortline.reasons)


def test_a_weak_tape_turns_into_wait_for_reclaim_instead_of_plain_listing() -> None:
    rows = minute_rows("2026-07-31", 48, start_price=10.8, step=-0.04)
    advice = AdviceEngine().build(
        "600519.SH",
        daily_for(
            rows[-1]["close"],
            supports=[8.6],
            resistances=[10.8],
            trend_state=TrendState.DOWNTREND,
            ma5=9.7,
            ma10=9.9,
            ma20=10.2,
            ma60=10.6,
            volume_ratio_20d=0.7,
        ),
        intraday_for(rows),
    )

    assert advice.shortline is not None
    assert advice.shortline.action in {"wait_reclaim", "reduce_risk"}
    assert "先等" in advice.shortline.summary or "优先防守" in advice.shortline.summary


def test_the_brief_surfaces_shortline_judgement_and_trade_context() -> None:
    advice = AdviceEngine().build(
        "600519.SH",
        daily_for(
            10.48,
            supports=[9.5],
            resistances=[11.0],
            ma5=10.30,
            ma10=10.20,
            ma20=10.00,
            ma60=9.80,
            volume_ratio_20d=1.3,
        ),
        intraday_for(),
        instrument={"ext": {"float_shares": 100000.0}},
    )

    brief = render_advice_brief(date(2026, 8, 3), [advice])

    assert "短线判断：" in brief
    assert "交易数据：" in brief
    assert "短线依据：" in brief
