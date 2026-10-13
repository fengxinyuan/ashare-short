"""三层裁决进台账、进统计、进主链路。

单独测「模型能选价」是不够的——那只证明了那一层能跑。真正要钉的是：**它选的
东西有没有被记下来**，以及记下来之后能不能回答「模型比规则准吗」。答不了的话，
这一层就只是多了一次网络调用。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from ashare_decision_support.advice import AdviceEngine, LevelAdjudicator
from ashare_decision_support.journal.records import Recommendation
from ashare_decision_support.journal.stats import summarize
from tests.test_advice_layers import StubClient, daily_for, intraday_for


def advice_with(client=None):
    engine = AdviceEngine(adjudicator=LevelAdjudicator(client=client) if client else None)
    return engine.build(
        "600519.SH", daily_for(10.48, supports=[9.5], resistances=[11.0]), intraday_for()
    )


# 台账


def test_the_journal_keeps_both_conclusions_not_just_the_final_one() -> None:
    """**只存最终结论的话，「模型裁决准不准」这个问题永远答不了。**

    那正是加这一层时唯一没法在设计阶段回答的问题——它只能靠样本回答，而样本
    只有现在开始记才会有。
    """
    base = advice_with()
    alternative = next(item for item in base.candidates.entries if item.price != base.entry_low)
    reply = json.dumps(
        {
            "action": "select",
            "entry": alternative.key(),
            "stop": base.candidates.default("stop").key(),
            "reason": "更低的位置",
        }
    )
    advice = advice_with(StubClient(reply))
    record = Recommendation.from_advice(advice, date(2026, 7, 31), source="advise")

    assert record is not None
    verdict = record.adjudication
    assert verdict["chosen_entry"]["price"] == alternative.price
    assert verdict["rule_entry"]["price"] == base.entry_low
    assert verdict["overrode_rule"] is True
    assert verdict["used_fallback"] is False


def test_the_journal_records_the_basis_not_only_the_price() -> None:
    """按**依据类别**分组统计，是给每个候选带 basis 的全部用途。

    只存价格的话，「分时均价回踩和五档买盘墙哪个更靠谱」这个问题在事后是
    无法重建的——两者在台账里长得一模一样。
    """
    record = Recommendation.from_advice(advice_with(), date(2026, 7, 31), source="advise")

    assert record is not None
    assert record.adjudication["chosen_entry"]["basis"] == "分时均价回踩"


def test_an_advice_without_any_adjudication_records_an_empty_dict_not_a_crash() -> None:
    """老记录、或者没有分时数据的记录，都不带裁决。"""
    from ashare_decision_support.advice.engine import StockAdvice

    record = Recommendation.from_advice(
        StockAdvice(symbol="600519.SH", last_price=10.0, entry_low=9.8, stop=9.5),
        date(2026, 7, 31),
        source="advise",
    )

    assert record is not None
    assert record.adjudication == {}


def test_the_journal_keeps_deterministic_score_components() -> None:
    """复盘必须能知道建议当时的总分来自哪些确定性因子。"""
    from ashare_decision_support.advice.engine import StockAdvice

    record = Recommendation.from_advice(
        StockAdvice(
            symbol="600519.SH",
            last_price=10.0,
            score_components={"technical": 58.2, "capital_flow": 1.5},
        ),
        date(2026, 7, 31),
        source="advise",
    )

    assert record is not None
    assert record.score_components == {"technical": 58.2, "capital_flow": 1.5}


# 统计


def outcome(overrode: bool, close_pct: float, abstained: bool = False) -> dict:
    return {
        "kind": "outcome",
        "symbol": "600519.SH",
        "trade_date": "2026-08-17",
        "source": "advise",
        "sessions": 5,
        "mfe_pct": close_pct + 0.01,
        "mae_pct": -0.01,
        "close_pct": close_pct,
        "next_close_pct": close_pct,
        "entered": True,
        "first_hit": "target" if close_pct > 0 else "stop",
        "adjudication": {
            "abstained": abstained,
            "used_fallback": not overrode and not abstained,
            "overrode_rule": overrode,
            "chosen_entry": {"price": 10.0, "basis": "五档买盘墙" if overrode else "分时均价回踩"},
        },
    }


def test_stats_can_answer_whether_the_model_beat_the_rule() -> None:
    stats = summarize(
        [outcome(overrode=True, close_pct=0.03) for _ in range(5)]
        + [outcome(overrode=False, close_pct=0.01) for _ in range(5)]
    )

    edge = stats["adjudication_edge"]
    assert edge["available"] is True
    assert edge["close_gap"] == 0.02
    assert edge["overrode_samples"] == 5
    # 样本远不够，统计量照给但标 trusted=False——藏起来只会让人误读那个数。
    assert edge["trusted"] is False


def test_agreeing_with_the_rule_is_kept_out_of_both_groups() -> None:
    """模型选了和规则一样的候选，两组都不进。

    算进模型那一组，会掺进大量「模型没做决定」的样本把差值稀释向零；算进规则
    那一组，会让规则组里混入模型的认可。它自成一档。
    """
    agreed = outcome(overrode=False, close_pct=0.02)
    agreed["adjudication"]["used_fallback"] = False

    stats = summarize(
        [outcome(overrode=True, close_pct=0.03), agreed, outcome(overrode=False, close_pct=0.01)]
    )

    edge = stats["adjudication_edge"]
    assert edge["overrode_samples"] == 1
    assert edge["default_samples"] == 1
    buckets = {row["bucket"]: row["samples"] for row in stats["by_decider"]}
    assert buckets == {"模型推翻规则": 1, "模型认可规则": 1, "规则默认": 1}


def test_abstentions_are_counted_but_kept_out_of_the_comparison() -> None:
    """弃权的记录没有买入价，兑现流程里根本不算入场，和有价位的记录不可比。"""
    stats = summarize(
        [outcome(overrode=True, close_pct=0.03)]
        + [outcome(overrode=False, close_pct=0.01)]
        + [outcome(overrode=False, close_pct=0.0, abstained=True) for _ in range(3)]
    )

    edge = stats["adjudication_edge"]
    assert edge["abstained_samples"] == 3
    assert edge["overrode_samples"] == 1
    assert edge["default_samples"] == 1


def test_entry_basis_grouping_separates_the_sources_of_a_price() -> None:
    stats = summarize(
        [outcome(overrode=True, close_pct=0.03) for _ in range(2)]
        + [outcome(overrode=False, close_pct=0.01) for _ in range(3)]
    )

    buckets = {row["bucket"]: row["samples"] for row in stats["by_entry_basis"]}
    assert buckets == {"五档买盘墙": 2, "分时均价回踩": 3}


def test_old_records_without_an_adjudication_are_a_bucket_of_their_own() -> None:
    """标成「未记录」而不是塞进「规则默认」——它们确实是规则默认，但把它们混
    进去会让「加这一层之前」和「加这一层之后」的样本不可分。"""
    stats = summarize([{"kind": "outcome", "close_pct": 0.01, "mfe_pct": 0.02, "mae_pct": -0.01}])

    buckets = {row["bucket"] for row in stats["by_decider"]}
    assert buckets == {"未记录"}


# 主链路


def test_advise_puts_candidates_and_the_book_into_the_facts() -> None:
    """数字闸门比对的就是这份 facts。候选价不进去，模型引用一个**合法**候选
    反而会被判成编造——闸门会在正确行为上报警。"""
    from ashare_decision_support.jobs import run_advise
    from tests.test_automation import RISE, SESSION, StubDaily, minute_bars

    class StubIntraday:
        def get_minute_bars(self, symbols, period="5", **kwargs):
            return {symbol: minute_bars(RISE) for symbol in symbols}, []

    summary = run_advise(
        adapter=StubDaily(),
        symbols=["600519.SH"],
        intraday_adapter=StubIntraday(),
        use_news=False,
        use_order_book=False,
        notifiers=[],
        trade_date=SESSION,
    )

    entry = summary["facts"]["symbols"][0]
    assert entry["candidates"] is not None
    assert entry["candidates"]["entries"]
    assert entry["adjudication"]["used_fallback"] is True
    assert entry["rrr_basis"] == "entry_high_to_nearest_target"
    assert entry["transaction_costs_included"] is False
    assert any("已关闭个股舆情" in note for note in summary["data_notes"])
    assert not any("个股舆情源不可用" in note for note in summary["data_notes"])

    # 引擎报的价必须出现在 facts 文本里，闸门才认得它。
    facts_text = json.dumps(summary["facts"], ensure_ascii=False, default=str)
    assert str(entry["entry_low"]) in facts_text


def test_the_order_book_agent_reaches_the_advice_when_a_stub_is_injected() -> None:
    from ashare_decision_support.jobs import run_advise
    from tests.test_automation import RISE, SESSION, StubDaily, minute_bars
    from tests.test_orderflow import snapshot as book_snapshot

    class StubIntraday:
        def get_minute_bars(self, symbols, period="5", **kwargs):
            return {symbol: minute_bars(RISE) for symbol in symbols}, []

    class StubSnapshots:
        def get_snapshots(self, symbols):
            return {
                symbol: book_snapshot(
                    bids=[(100.5, 800000.0), (100.49, 1000.0)],
                    asks=[(102.6, 5000.0)],
                    last=101.95,
                    limit_up=112.0,
                    limit_down=91.7,
                )
                for symbol in symbols
            }, []

    summary = run_advise(
        adapter=StubDaily(),
        symbols=["600519.SH"],
        intraday_adapter=StubIntraday(),
        use_news=False,
        snapshot_adapter=StubSnapshots(),
        notifiers=[],
        trade_date=SESSION,
    )

    entry = summary["facts"]["symbols"][0]
    assert entry["order_book"] is not None
    assert entry["order_book"]["bid_wall_price"] == 100.5
    bases = [item["basis"] for item in entry["candidates"]["entries"]]
    assert "五档买盘墙" in bases


# 主链路：落台账

# 上面两节钉的是「记录格式对不对」和「统计算得对不对」，两半都绿——而**中间那根
# 线一直是断的**：`advise` 从头到尾没引用过 `journal`，于是 `adjudication` 在生产
# 里永远是空，`adjudication_edge` 永远返回 `available: False`。两半各自有测试、
# 各自通过，正是这个缺口能活下来的原因。所以这一节测的是**接缝**，不是两端。


def _run_advise(journal=None, symbols=("600519.SH",)):
    from ashare_decision_support.jobs import run_advise
    from tests.test_automation import RISE, SESSION, StubDaily, minute_bars

    class StubIntraday:
        def get_minute_bars(self, symbols, period="5", **kwargs):
            return {symbol: minute_bars(RISE) for symbol in symbols}, []

    return run_advise(
        adapter=StubDaily(),
        symbols=list(symbols),
        intraday_adapter=StubIntraday(),
        use_news=False,
        use_order_book=False,
        notifiers=[],
        trade_date=SESSION,
        journal=journal,
    )


def test_advice_summary_does_not_equate_levels_with_a_buy_signal() -> None:
    summary = _run_advise()
    assert summary["analyzed"] == 1
    assert summary["with_levels"] == summary["advised"]
    assert summary["risk_qualified_plans"] <= summary["with_levels"]
    assert "不是即刻买点" in summary["count_note"]


class _ForwardBars:
    """建议日那根，加上其后 7 根一路走高的前复权日线。"""

    def __init__(self, anchor: date, base: float = 100.0) -> None:
        self.anchor = anchor
        self.base = base

    def get_historical_bars(self, symbols, start, end):
        rows = []
        for offset in range(8):
            close = self.base * (1.0 + 0.01 * offset)
            rows.append(
                {
                    "date": self.anchor + timedelta(days=offset),
                    "open": close,
                    "high": close * 1.02,
                    "low": close * 0.99,
                    "close": close,
                }
            )
        return {symbol: list(rows) for symbol in symbols}


def test_advise_writes_its_verdict_into_the_journal(tmp_path) -> None:
    """`advise` 是**唯一**经过裁决层的链路——`morning` 跑在盘前，那时没有分时。

    它不落台账，这个字段在生产里就永远是空的。
    """
    from ashare_decision_support.journal import Journal

    journal = Journal(root=tmp_path / "journal")
    summary = _run_advise(journal=journal)

    assert summary["journal_records"] == 1
    rows = journal.recommendations()
    assert [row["source"] for row in rows] == ["advise"]

    # 统计只读这几个键（`journal/stats.py:_adjudication_edge`）。写入侧与读取侧
    # 一旦对不上，两边的单测照样全绿，而那个数永远算不出来。
    verdict = rows[0]["adjudication"]
    assert verdict["used_fallback"] is True
    assert verdict["overrode_rule"] is False
    assert verdict["chosen_entry"]["basis"]
    assert verdict["rule_entry"]["price"] is not None


def test_running_advise_twice_in_a_day_leaves_one_row(tmp_path) -> None:
    """`record_many` 是无脑追加，而 `advise` 是随时能再跑的命令。

    不挡一道，一天跑五次就是五条同源记录，`stats` 会当成五个独立样本。
    """
    from ashare_decision_support.journal import Journal

    journal = Journal(root=tmp_path / "journal")
    first = _run_advise(journal=journal)
    second = _run_advise(journal=journal)

    assert (first["journal_records"], second["journal_records"]) == (1, 0)
    assert len(journal.recommendations()) == 1


def test_the_lookup_paths_record_nothing(tmp_path) -> None:
    """`ask` / `facts` / `chat` 也调 `run_advise`，但它们是**查**，不是发建议。

    把一次随口问到的标的记成「系统当天的建议」，事后统计就分不清哪些是系统
    主动给的了。
    """
    from ashare_decision_support.journal import Journal

    journal = Journal(root=tmp_path / "journal")
    summary = _run_advise(journal=None)

    assert summary["journal_records"] == 0
    assert journal.recommendations() == []


def test_a_recorded_verdict_reaches_the_stats_as_rule_default(tmp_path) -> None:
    """整条链走一遍：建议 → 核对 → 统计。

    这一条是前面三条的理由。`by_decider` 从前只可能得到「未记录」——不是因为
    裁决层没跑，而是因为它的结论没有出口。
    """
    from ashare_decision_support.journal import Journal
    from ashare_decision_support.journal.stats import summarize
    from ashare_decision_support.journal.verify import verify_pending
    from tests.test_automation import SESSION

    journal = Journal(root=tmp_path / "journal")
    _run_advise(journal=journal)

    verify_pending(journal, _ForwardBars(SESSION), today=SESSION + timedelta(days=8))
    stats = summarize(journal.outcomes())

    assert {row["bucket"] for row in stats["by_decider"]} == {"规则默认"}
    assert stats["adjudication_edge"]["default_samples"] == 1


def test_hand_asked_symbols_stay_out_of_the_selection_comparison() -> None:
    """`advise` 的标的是**人手点名**的，不能算进「这套筛选有没有用」的分子。

    `_selection_edge` 与 `_veto_cost` 的差值都建立在「同日、同筛选流程、只差
    最后一刀排序」之上。按 `rec_kind` 取入选组会把 `advise` 记的行一并算进去，
    而那些票从来没进过板块排序和业绩闸门——差值的分子里于是混进了一批不是这套
    筛选选出来的票，`picked_samples` 也会虚高。

    分组照旧按 `kind`（入选/对照/否决是三个**组**），围栏按 `source`
    （记录由**哪条链路**产生）。两者是不同的维度。
    """
    from ashare_decision_support.journal.stats import summarize

    def row(source: str, rec_kind: str, ret: float) -> dict:
        return {
            "kind": "outcome",
            "source": source,
            "rec_kind": rec_kind,
            "mfe_pct": ret,
            "close_pct": ret,
            "next_open_pct": ret,
            "next_close_pct": ret,
        }

    screened = [row("morning", "recommendation", 0.02), row("morning", "control", 0.01)]
    # 手点的这批全给一个夸张的收益：混进去的话 picked 的中位数会被它们改写。
    hand_asked = [row("advise", "recommendation", 0.50) for _ in range(5)]

    baseline = summarize(screened)["selection_edge"]
    with_advise = summarize(screened + hand_asked)["selection_edge"]

    assert with_advise["picked_samples"] == baseline["picked_samples"] == 1
    assert with_advise["close_gap"] == baseline["close_gap"]

    # 但它们仍然进总体统计与按打法分组——围栏只挡这两个**对照差值**。
    assert summarize(screened + hand_asked)["samples"] == 7


# 业绩层在 advise 上的语义：提示，不移除

# `morning` 是系统主动选票，业绩亏损直接否决；`advise` 是你点名问一只票，答案
# 必须是「它怎么样」而不是把它藏起来。同一份裁决在两条链路上处置强度不同，
# 这不是不一致，是「谁在提问」不同。


class StubEarnings:
    def __init__(self, rows):
        self.rows = rows

    def fetch(self, trade_date, period=None):
        from ashare_decision_support.adapters.earnings import EarningsAdapter

        rows = self.rows

        class _Ak:
            def stock_yjyg_em(self, date):
                return rows

            def stock_yjkb_em(self, date):
                return []

        return EarningsAdapter(ak_module=_Ak()).fetch(trade_date, period=period)


def _row(code: str, kind: str, pct: float) -> dict:
    return {
        "股票代码": code,
        "股票简称": "茅台",
        "预测指标": "归属于上市公司股东的净利润",
        "预告类型": kind,
        "业绩变动幅度": pct,
        "公告日期": "2026-07-30",
        "业绩变动原因": "原因",
    }


def _advise(rows):
    from ashare_decision_support.jobs import run_advise
    from tests.test_automation import RISE, SESSION, StubDaily, minute_bars

    class StubIntraday:
        def get_minute_bars(self, symbols, period="5", **kwargs):
            return {symbol: minute_bars(RISE) for symbol in symbols}, []

    return run_advise(
        adapter=StubDaily(),
        symbols=["600519.SH"],
        intraday_adapter=StubIntraday(),
        use_news=False,
        use_order_book=False,
        notifiers=[],
        trade_date=SESSION,
        earnings_adapter=StubEarnings(rows),
    )


def test_advise_warns_about_an_earnings_landmine_instead_of_hiding_it() -> None:
    summary = _advise([_row("600519", "首亏", -180.0)])
    entry = summary["facts"]["symbols"][0]

    # 标的仍然被回答——「这只票我不给你看」是最没用的回答。
    assert entry["symbol"] == "600519.SH"
    warnings = entry["warnings"]
    assert any("业绩否决" in text for text in warnings)
    # 置顶：排在第七条风险后面的「中报首亏」等于没说。
    assert "业绩否决" in warnings[0]


def test_advise_surfaces_an_earnings_beat_as_a_reason() -> None:
    summary = _advise([_row("600519", "预增", 1197.0)])
    entry = summary["facts"]["symbols"][0]

    assert any("业绩催化" in text for text in entry["reasons"])
    assert not any("业绩否决" in text for text in entry["warnings"])
