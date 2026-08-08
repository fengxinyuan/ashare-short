"""`ask` 单入口：按时段自动装配上下文。

这一层没有算法，只有**装配决策**。所以测试全在装配上：时段边界对不对、
每一块读不出来时会不会拖垮其余、旧数据有没有被明确标成旧数据。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from ashare_decision_support.ask import (
    PHASE_INTRADAY,
    PHASE_OFF_HOURS,
    PHASE_POST_CLOSE,
    PHASE_PRE_OPEN,
    assemble,
    current_phase,
    run_ask,
)
from ashare_decision_support.cli import build_parser
from ashare_decision_support.facts import build_facts_bundle


class StubClient:
    def __init__(self, reply: str = "", configured: bool = True) -> None:
        self.reply = reply
        self.configured = configured
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        return self.reply


class BoomClient:
    configured = True

    def chat(self, messages: list[dict]) -> str:
        raise RuntimeError("provider down")


# ---- 时段 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (3, 0, PHASE_OFF_HOURS),
        (8, 20, PHASE_PRE_OPEN),
        (9, 14, PHASE_PRE_OPEN),
        # 09:15 集合竞价开始报价，从这一刻起问的就是当日的事。
        (9, 15, PHASE_INTRADAY),
        (14, 59, PHASE_INTRADAY),
        (15, 0, PHASE_POST_CLOSE),
        (22, 0, PHASE_POST_CLOSE),
    ],
)
def test_phase_boundaries(hour: int, minute: int, expected: str) -> None:
    assert current_phase(datetime(2026, 7, 31, hour, minute)) == expected


def test_a_non_trading_day_is_off_hours_whatever_the_clock_says() -> None:
    """周六上午十点问问题，要的不是「盘中」那套实时上下文。"""
    ctx = assemble(datetime(2026, 8, 1, 10, 0), is_trading_day=False)

    assert ctx.phase == PHASE_OFF_HOURS
    assert any("旧数据" in note for note in ctx.notes)


# ---- 装配 ----------------------------------------------------------------


def test_intraday_computes_live_levels_while_pre_open_does_not() -> None:
    """盘中的价位没有「已经算好的那一份」，按定义只能现算。

    盘前不算是因为开盘前根本没有当日分时——现算只会拿到昨天的数据，然后被
    当成今天的。
    """
    calls: list[list[str]] = []

    def compute(symbols: list[str]) -> dict:
        calls.append(list(symbols))
        return {"symbols": [{"symbol": symbols[0], "last_price": 20.0}]}

    from ashare_decision_support.state import Watchlist

    book = Watchlist()
    book.add("600519.SH")
    book.save()

    intraday = assemble(datetime(2026, 7, 31, 10, 30), compute_symbols=compute)
    assert calls == [["600519.SH"]]
    assert "实时买卖点" in intraday.blocks

    calls.clear()
    pre_open = assemble(datetime(2026, 7, 31, 8, 30), compute_symbols=compute)
    assert calls == []
    assert "实时买卖点" not in pre_open.blocks
    assert any("昨收口径" in note for note in pre_open.notes)


def test_live_computation_failure_costs_only_that_block() -> None:
    """实时价位算不出来，不该把板块排序、告警、台账一起带走。"""

    def boom(symbols: list[str]) -> dict:
        raise RuntimeError("adapter down")

    from ashare_decision_support.state import Watchlist

    book = Watchlist()
    book.add("600519.SH")
    book.save()

    ctx = assemble(datetime(2026, 7, 31, 10, 30), compute_symbols=boom)

    assert "实时买卖点" not in ctx.blocks
    assert "台账统计" in ctx.blocks
    assert "近期验证" in ctx.blocks
    assert "自选与候选" in ctx.blocks
    assert any("实时买卖点计算失败" in note for note in ctx.notes)


def test_a_missing_payload_file_is_a_note_not_a_crash(tmp_path) -> None:
    ctx = assemble(datetime(2026, 7, 31, 8, 30), output_dir=tmp_path)

    assert any("尚无落盘结果" in note for note in ctx.notes)


def test_strategy_screen_payload_is_available_to_ask_context(tmp_path) -> None:
    payload = {"schema": "ashare-strategy-screen-v1", "as_of": "2026-08-07", "candidates": []}
    (tmp_path / "strategy_screen_latest.json").write_text(json.dumps(payload), encoding="utf-8")
    ctx = assemble(datetime(2026, 8, 8, 8, 30), output_dir=tmp_path)
    assert ctx.blocks["参数化策略扫描"]["schema"] == "ashare-strategy-screen-v1"


def test_strategy_screen_parser_accepts_all_profiles() -> None:
    args = build_parser().parse_args(["strategy-screen", "--profile", "all", "--all-a", "--allow-st"])
    assert args.profile == "all"
    assert args.all_a is True
    assert args.allow_st is True
    assert args.exclude_st is False

    excluded = build_parser().parse_args(["strategy-screen", "--exclude-st"])
    assert excluded.exclude_st is True


def test_the_payloads_own_date_is_surfaced_so_stale_data_is_visible(tmp_path) -> None:
    """读到一份三天前的盘前结果而不说，等于把旧结论当成今天的。"""
    (tmp_path / "morning_latest.json").write_text(
        json.dumps({"trade_date": "2026-07-28", "sectors": []}), encoding="utf-8"
    )
    ctx = assemble(datetime(2026, 7, 31, 8, 30), output_dir=tmp_path)

    assert "盘前主链路" in ctx.blocks
    assert any("2026-07-28" in note for note in ctx.notes)


def test_post_close_carries_todays_alerts_and_the_journal() -> None:
    ctx = assemble(datetime(2026, 7, 31, 16, 0))

    assert ctx.phase == PHASE_POST_CLOSE
    assert "当日告警" in ctx.blocks
    assert "台账统计" in ctx.blocks


# ---- 问答 ----------------------------------------------------------------


def test_no_question_prints_the_context_without_calling_the_model() -> None:
    """核对模型有没有胡说的办法，就是先看不经模型的那一份。"""
    printed: list[str] = []
    client = StubClient("不该被调用")
    result = run_ask(
        question=None, now=datetime(2026, 7, 31, 10, 0), client=client, write=printed.append
    )

    assert client.calls == []
    assert result["answer"] is None
    assert printed and "【盘中】" in printed[0]


def test_without_a_key_the_facts_still_come_out() -> None:
    """没配 key 不是「功能不可用」，是「少了组织语言那一层」。"""
    printed: list[str] = []
    result = run_ask(
        question="今天该看什么",
        now=datetime(2026, 7, 31, 10, 0),
        client=StubClient(configured=False),
        write=printed.append,
    )

    assert result["answer"] is None
    assert any("未配置 LLM key" in note for note in result["notes"])
    assert printed and "【盘中】" in printed[0]


def test_without_a_client_the_default_path_is_facts_only() -> None:
    printed: list[str] = []
    result = run_ask(
        question="今天该看什么",
        now=datetime(2026, 7, 31, 10, 0),
        client=None,
        write=printed.append,
    )

    assert result["answer"] is None
    assert any("默认不开模型对话" in note for note in result["notes"])
    assert printed and "【盘中】" in printed[0]


def test_facts_bundle_is_ready_for_external_ai_consumers() -> None:
    def compute(symbols: list[str]) -> dict:
        return {"symbols": [{"symbol": symbols[0], "last_price": 20.0}]}

    from ashare_decision_support.state import Watchlist

    book = Watchlist()
    book.add("600519.SH")
    book.save()

    bundle = build_facts_bundle(
        moment=datetime(2026, 7, 31, 10, 30),
        compute_symbols=compute,
        focus=["600519.SH"],
    )

    payload = bundle.to_dict()
    assert payload["schema"] == "ashare-facts-v1"
    assert payload["contract"]["as_of_field"] == "as_of"
    assert payload["phase"] == PHASE_INTRADAY
    assert payload["facts"]["时段"] == "盘中"
    assert "实时买卖点" in payload["facts"]
    assert "近期验证" in payload["facts"]
    assert "【盘中】" in payload["rendered"]


def test_cli_parser_accepts_the_facts_command() -> None:
    args = build_parser().parse_args(["facts", "--format", "text"])

    assert args.command == "facts"
    assert args.format == "text"


def test_render_prefers_shortline_summary_over_raw_field_listing() -> None:
    from ashare_decision_support.state import Watchlist

    book = Watchlist()
    book.add("600519.SH")
    book.save()

    def compute(symbols: list[str]) -> dict:
        return {
            "symbols": [
                {
                    "symbol": symbols[0],
                    "name": "贵州茅台",
                    "last_price": 1350.6,
                    "entry_low": 1333.9,
                    "entry_high": 1337.5,
                    "stop": 1285.4,
                    "targets": [1355.7],
                    "shortline": {
                        "action": "wait_trigger",
                        "score": 2,
                        "summary": "结构尚可，优先看回踩承接，突破再加速",
                        "reasons": ["现价位于分时均价上方 0.32%", "换手率 2.36%"],
                    },
                    "trade_context": {
                        "turnover_ratio": 2.36,
                        "session_amount": 12686185800.0,
                    },
                }
            ]
        }

    rendered = assemble(
        datetime(2026, 7, 31, 10, 0),
        compute_symbols=compute,
    ).render()

    assert "结构尚可，优先看回踩承接，突破再加速" in rendered
    assert "换手率 2.36%" in rendered
    assert "成交额 126.86 亿" in rendered


def test_an_answer_with_an_invented_number_is_discarded_and_the_raw_data_shown() -> None:
    printed: list[str] = []
    result = run_ask(
        question="茅台现在多少钱",
        now=datetime(2026, 7, 31, 10, 0),
        client=StubClient("现价 1234.56 元，建议买入"),
        write=printed.append,
    )

    assert result["answer"] is None
    assert any("无法追溯的数字" in note for note in result["notes"])
    assert any("原始数据" in line for line in printed)


def test_a_dead_provider_degrades_to_the_raw_context() -> None:
    printed: list[str] = []
    result = run_ask(
        question="今天怎么样",
        now=datetime(2026, 7, 31, 10, 0),
        client=BoomClient(),
        write=printed.append,
    )

    assert result["answer"] is None
    assert any("LLM 调用失败" in note for note in result["notes"])
    assert printed


def test_the_phase_hint_reaches_the_model() -> None:
    """同一个问题在盘前和盘中要的是不同的答案，模型得知道现在是哪个。"""
    client = StubClient("好的")
    run_ask(
        question="这只怎么样",
        now=datetime(2026, 7, 31, 8, 30),
        client=client,
        write=lambda _: None,
    )

    system = client.calls[0][0]["content"]
    assert "开盘前" in system
    assert "昨收口径" in system


def test_the_answer_survives_when_every_number_traces_back() -> None:
    client = StubClient("上下文里没有价格数据。")
    result = run_ask(
        question="茅台多少钱",
        now=datetime(2026, 7, 31, 10, 0),
        client=client,
        write=lambda _: None,
    )

    assert result["answer"] == "上下文里没有价格数据。"
    assert result["phase"] == PHASE_INTRADAY


def test_ask_pulls_symbol_codes_from_the_question_itself() -> None:
    seen: list[list[str]] = []

    def compute(symbols: list[str]) -> dict:
        seen.append(list(symbols))
        return {"symbols": [{"symbol": symbols[0], "last_price": 10.0}]}

    result = run_ask(
        question="600519.SH 现在趋势怎么样",
        now=datetime(2026, 7, 31, 10, 0),
        compute_symbols=compute,
        client=StubClient("可以"),
        write=lambda _: None,
    )

    assert seen == [["600519.SH"]]
    assert result["answer"] == "可以"


def test_a_held_position_is_actually_counted() -> None:
    """持仓数是盘中问答里最能改变答案方向的一个数——看到 0，模型就不会往
    「该不该卖」上答。

    字段叫 `holding`。按别的名字 `getattr(entry, ..., False)` 会**静默**恒为
    False，而静默的 0 和真的空仓在装配好的上下文里长得一模一样：不会报错，
    不会有 note，只是答案默默偏了。
    """
    from ashare_decision_support.state import Watchlist

    book = Watchlist()
    book.add("600519.SH")
    book.add("000001.SZ")
    book.set_position("600519.SH", cost_price=1350.6)
    book.save()

    block = assemble(datetime(2026, 7, 31, 8, 30)).blocks["自选与候选"]

    assert block["持仓数"] == 1
    assert [item["symbol"] for item in block["持仓"]] == ["600519.SH"]


def test_candidate_pool_metadata_survives_into_the_assembled_context() -> None:
    from ashare_decision_support.state import CandidatePool, WatchEntry

    trade_day = date(2026, 8, 7)
    CandidatePool(today=trade_day).replace(
        [
            WatchEntry(
                symbol="300750.SZ",
                name="宁德时代",
                sector="电力设备",
                industries=["电力设备", "电池", "锂电池"],
                industry="电力设备",
                industry_leaf="锂电池",
                industry_path="电力设备·电池·锂电池",
                concepts=["固态电池", "储能"],
                source="sector-scan",
                score=88.0,
            )
        ],
        trade_day,
    )

    block = assemble(datetime(2026, 8, 7, 8, 30)).blocks["自选与候选"]
    row = block["候选池"][0]

    assert row["industry_path"] == "电力设备·电池·锂电池"
    assert row["concepts"] == ["固态电池", "储能"]


def test_the_cost_basis_survives_being_rendered() -> None:
    """成本价必须**出现在 `render()` 的文本里**，不只是在 blocks 里。

    没配 key 时 `render()` 就是全部输出，而它按 160 字截断每一条。持仓那一条
    原来复用 `WatchEntry.to_dict()`（`asdict` 顺序），成本价、计划止损、计划目标
    正好排在最后——一只名字和备注稍长的票就会把这三个数截掉。断言渲染后的文本，
    是因为断言 blocks 通不过这个洞：blocks 里它们一直都在。
    """
    from ashare_decision_support.state import Watchlist

    book = Watchlist()
    book.add("600519.SH", name="贵州茅台", sector="食品饮料", note="低吸 · 回调到年线附近接")
    book.set_position("600519.SH", cost_price=1350.6, plan_stop=1300.0, plan_target=1420.0)
    book.save()

    rendered = assemble(datetime(2026, 7, 31, 8, 30)).render()

    assert "1350.6" in rendered
    assert "1300.0" in rendered
    assert "1420.0" in rendered
