from __future__ import annotations

import json
from dataclasses import fields
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from ashare_decision_support.cli import _writer as build_cli_writer
from ashare_decision_support.factcheck import unverified_numbers
from ashare_decision_support.jobs.morning import (
    MorningConfig,
    Sources,
    _sample_across_sectors,
    run_morning,
)
from ashare_decision_support.news import NewsDoc
from ashare_decision_support.sectors import CONCEPT_LEVEL
from ashare_decision_support.sectors.base import SectorDefinition
from ashare_decision_support.writer import LlmBriefWriter, TemplateWriter, WriteRequest


def _run_morning(**kwargs):
    """把平铺的 kwargs 分派到 `Sources` / `MorningConfig` 两个配置对象。

    `run_morning` 的签名从 32 个参数收敛成两个配置对象之后，测试仍然按字段名
    调用——它们关心的是「给了这个输入会怎样」，不是参数怎么打包。字段归属直接
    从 dataclass 上读，所以两边加字段时这层不会悄悄漏掉。
    """
    config_fields = {f.name for f in fields(MorningConfig)}
    source_fields = {f.name for f in fields(Sources)}
    passthrough = {"trade_date", "progress"}

    kwargs = dict(kwargs)
    output_dir = kwargs.pop("output_dir")
    unknown = set(kwargs) - config_fields - source_fields - passthrough
    if unknown:
        raise TypeError(f"未知参数：{sorted(unknown)}")

    return run_morning(
        Sources(**{k: v for k, v in kwargs.items() if k in source_fields}),
        output_dir,
        config=MorningConfig(**{k: v for k, v in kwargs.items() if k in config_fields}),
        **{k: v for k, v in kwargs.items() if k in passthrough},
    )

# ---- numeric fidelity gate ----------------------------------------------


FACTS = {"symbol": "600519.SH", "score": 72.5, "last_close": 1361.8, "count": 3}
FACTS_TEXT = json.dumps(FACTS, ensure_ascii=False)


def test_gate_accepts_numbers_copied_from_the_facts() -> None:
    text = "600519.SH 评分 72.5，现价 1361.8，共 3 项依据。"
    assert unverified_numbers(text, FACTS_TEXT) == []


def test_gate_flags_an_invented_number() -> None:
    text = "600519.SH 评分 72.5，目标价 1500.0。"
    assert unverified_numbers(text, FACTS_TEXT) == ["1500.0"]


def test_gate_allows_small_ordinals_for_list_numbering() -> None:
    """Markdown list markers are the model's own, not a claim about the data."""
    assert unverified_numbers("1. 第一条\n2. 第二条", FACTS_TEXT) == []


def test_gate_rejects_a_large_bare_integer_not_in_the_facts() -> None:
    assert unverified_numbers("涨停家数 87 只", FACTS_TEXT) == ["87"]


def test_gate_accepts_a_substring_of_a_longer_fact_number() -> None:
    """Rounding 1361.8 to 1361 in prose is rephrasing, not fabrication."""
    assert unverified_numbers("现价约 1361 元", FACTS_TEXT) == []


def test_gate_accepts_a_trailing_zero_reformat() -> None:
    """Writing 72.5 as 72.50 is formatting; the gate must not fire on it."""
    assert unverified_numbers("评分 72.50", FACTS_TEXT) == []


def test_gate_accepts_rounding_of_a_long_float() -> None:
    """Float noise in the facts must not make faithful rounding look invented."""
    facts = json.dumps({"net_inflow": 12198154.379999977}, ensure_ascii=False)
    assert unverified_numbers("净流入 12198154.38 元", facts) == []


def test_gate_accepts_a_unit_rescale_with_a_chinese_suffix() -> None:
    facts = json.dumps({"net_inflow": 12198154.379999977}, ensure_ascii=False)
    assert unverified_numbers("净流入 0.12亿", facts) == []


def test_gate_accepts_a_ratio_written_as_a_percentage() -> None:
    facts = json.dumps({"breadth": 0.6667}, ensure_ascii=False)
    assert unverified_numbers("上涨占比 67%", facts) == []


def test_gate_still_fires_on_a_number_no_rescale_can_explain() -> None:
    facts = json.dumps({"net_inflow": 12198154.379999977}, ensure_ascii=False)
    assert unverified_numbers("净流入 5.55亿", facts) == ["5.55"]


def test_gate_reads_thousands_separators_as_one_number() -> None:
    """'12,198,154.38' is one figure, not 12 / 198 / 154.38."""
    facts = json.dumps({"net_inflow": 12198154.379999977}, ensure_ascii=False)
    assert unverified_numbers("净流入12,198,154.38元", facts) == []


def test_gate_still_fires_on_a_grouped_number_that_is_invented() -> None:
    facts = json.dumps({"net_inflow": 12198154.379999977}, ensure_ascii=False)
    assert unverified_numbers("净流入99,888,777.00元", facts) == ["99,888,777.00"]


def test_the_three_number_gates_share_one_definition_of_a_free_ordinal() -> None:
    """三道闸门严格程度不同是**有意**的，但「多小的整数算序号」必须是同一个数。

    上面几条钉的是行为，这条钉的是结构。收口前 `ORDINAL_CEILING = 20` 有两份
    定义，而 `advice/external.py` 从两个不同模块分别拿这道闸门的两个常量——两份
    20 漂成 20 和 15 不会让任何用例变红，只会让同一个「第 18 档」在简报里放行、
    在裁决里作废。
    """
    import ast
    import pathlib

    from ashare_decision_support import factcheck
    from ashare_decision_support.advice import adjudicate, external

    # 散文那道闸门 2026-08-15 从 `writer/llm.py` 搬进 `factcheck.py`（它有五个调用
    # 方，写简报那一层不该替另外四条链路保管它），所以这里不再有 `llm` 那一项——
    # 写简报的那一层现在根本不做数字扫描。
    assert adjudicate.ORDINAL_CEILING is external.ORDINAL_CEILING is factcheck.ORDINAL_CEILING

    src = pathlib.Path(factcheck.__file__).parent
    definitions = [
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name)
        and target.id in {"ORDINAL_CEILING", "NUMBER_PATTERN", "UNIT_SCALES"}
    ]
    assert sorted(definitions) == ["factcheck.py"] * 3


# ---- writers -------------------------------------------------------------


def make_request() -> WriteRequest:
    return WriteRequest(title="测试简报", facts=FACTS, fallback_markdown="# 模板正文\n评分 72.5")


def test_template_writer_returns_the_markdown_untouched() -> None:
    result = TemplateWriter().write(make_request())

    assert result.markdown == "# 模板正文\n评分 72.5"
    assert not result.used_fallback


def test_llm_writer_falls_back_without_a_key() -> None:
    writer = LlmBriefWriter(provider="qwen", api_key="")
    writer.api_key = None
    result = writer.write(make_request())

    assert result.used_fallback
    assert result.markdown == "# 模板正文\n评分 72.5"
    assert any("DASHSCOPE_API_KEY" in note for note in result.notes)


def test_llm_writer_keeps_faithful_output() -> None:
    writer = LlmBriefWriter(provider="qwen", api_key="test-key")
    writer._complete = lambda prompt: "600519.SH 当前评分 72.5，收盘 1361.8。"

    result = writer.write(make_request())

    assert not result.used_fallback
    assert "72.5" in result.markdown


def test_llm_writer_appends_compliance_text_the_model_dropped() -> None:
    """The model summarises the discipline block away; it gets re-attached."""
    writer = LlmBriefWriter(provider="qwen", api_key="test-key")
    writer._complete = lambda prompt: "600519.SH 当前评分 72.5。"
    request = WriteRequest(
        title="测试简报",
        facts=FACTS,
        fallback_markdown="# 模板正文",
        mandatory_suffix="## 纪律提醒\n- 不接受撤单",
    )

    result = writer.write(request)

    assert not result.used_fallback
    assert "## 纪律提醒" in result.markdown
    assert "不接受撤单" in result.markdown


def test_llm_writer_does_not_duplicate_compliance_text() -> None:
    writer = LlmBriefWriter(provider="qwen", api_key="test-key")
    writer._complete = lambda prompt: "评分 72.5。\n\n## 纪律提醒\n- 不接受撤单"
    request = WriteRequest(
        title="测试简报",
        facts=FACTS,
        fallback_markdown="# 模板正文",
        mandatory_suffix="## 纪律提醒\n- 不接受撤单",
    )

    assert writer.write(request).markdown.count("## 纪律提醒") == 1


def test_llm_writer_discards_output_that_invents_a_number() -> None:
    writer = LlmBriefWriter(provider="qwen", api_key="test-key")
    writer._complete = lambda prompt: "600519.SH 评分 72.5，预计上涨 15.6%，目标 1888.0。"

    result = writer.write(make_request())

    assert result.used_fallback
    assert result.markdown == "# 模板正文\n评分 72.5"
    assert any("不存在的数字" in note for note in result.notes)


def test_llm_writer_falls_back_when_the_call_raises() -> None:
    writer = LlmBriefWriter(provider="qwen", api_key="test-key")

    def boom(prompt):
        raise TimeoutError("upstream timeout")

    writer._complete = boom
    result = writer.write(make_request())

    assert result.used_fallback
    assert any("TimeoutError" in note for note in result.notes)


def test_llm_writer_falls_back_on_empty_output() -> None:
    writer = LlmBriefWriter(provider="qwen", api_key="test-key")
    writer._complete = lambda prompt: "   "

    assert writer.write(make_request()).used_fallback


# ---- constituent sampling ------------------------------------------------


def test_llm_writer_drops_a_duplicate_title_line() -> None:
    """The notifier prepends the title; a model-generated H1 renders it twice."""
    writer = LlmBriefWriter(provider="qwen", api_key="test-key")
    writer._complete = lambda prompt: "# 测试简报\n\n评分 72.5。"

    markdown = writer.write(make_request()).markdown

    assert not markdown.startswith("#")
    assert markdown.startswith("评分 72.5")


def test_llm_writer_keeps_a_leading_subheading() -> None:
    writer = LlmBriefWriter(provider="qwen", api_key="test-key")
    writer._complete = lambda prompt: "## 板块排序\n\n评分 72.5。"

    assert writer.write(make_request()).markdown.startswith("## 板块排序")


def test_cli_writer_auto_uses_template_without_a_key(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    writer = build_cli_writer(SimpleNamespace(writer="auto", llm_provider="qwen", llm_model=None))

    assert isinstance(writer, TemplateWriter)


def test_cli_writer_auto_prefers_llm_when_a_key_exists(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    writer = build_cli_writer(SimpleNamespace(writer="auto", llm_provider="qwen", llm_model=None))

    assert isinstance(writer, LlmBriefWriter)


def test_cli_template_writer_does_not_construct_an_llm(monkeypatch) -> None:
    def unexpected_llm(*args, **kwargs):
        raise AssertionError("template mode must not construct an LLM writer")

    monkeypatch.setattr("ashare_decision_support.cli.LlmBriefWriter", unexpected_llm)
    writer = build_cli_writer(SimpleNamespace(writer="template", llm_provider="qwen", llm_model=None))

    assert isinstance(writer, TemplateWriter)


def test_sampling_gives_every_sector_the_same_coverage_rate() -> None:
    big = SectorDefinition(name="大", level="SW1", symbols=tuple(f"B{i}" for i in range(400)))
    small = SectorDefinition(name="小", level="SW1", symbols=tuple(f"S{i}" for i in range(40)))

    picked = set(_sample_across_sectors([big, small], 220))
    big_rate = len([s for s in picked if s.startswith("B")]) / 400
    small_rate = len([s for s in picked if s.startswith("S")]) / 40

    assert abs(big_rate - small_rate) < 0.1


def test_sampling_returns_everything_when_the_budget_is_ample() -> None:
    sector = SectorDefinition(name="小", level="SW1", symbols=("A", "B", "C"))
    assert set(_sample_across_sectors([sector], 100)) == {"A", "B", "C"}


def test_sampling_keeps_at_least_two_names_from_a_tiny_sector() -> None:
    big = SectorDefinition(name="大", level="SW1", symbols=tuple(f"B{i}" for i in range(1000)))
    tiny = SectorDefinition(name="微", level="SW1", symbols=("T0", "T1", "T2"))

    picked = set(_sample_across_sectors([big, tiny], 50))
    assert len([s for s in picked if s.startswith("T")]) >= 2


# ---- end-to-end job ------------------------------------------------------


def trending_bars(
    count: int,
    drift: float,
    pullback_days: int = 0,
    pullback_drift: float = 0.0,
    pullback_volume: float = 1.0,
) -> list[dict]:
    """合成日线。尾部可选一段**缩量回调**。

    默认不带回调段是为了让那些只测板块动量的用例保持原样。带回调段的那一支是
    选股用例需要的：改造后候选必须命中一条打法（`strategy/playbook.py`），而
    一路单边上涨、成交量同步放大的序列**本来就不该**被选成回踩候选——旧版本
    会选它，那正是被修掉的缺陷。
    """
    price = 20.0
    rows: list[dict] = []
    day = date(2026, 1, 5)
    rising = count - pullback_days
    for index in range(count):
        step = drift if index < rising else pullback_drift
        price = max(price * (1 + step), 1.0)
        volume = 100000 + index * 50
        if index >= rising:
            volume = int(volume * pullback_volume)
        rows.append(
            {
                "timestamp": datetime.combine(day + timedelta(days=index), datetime.min.time()),
                "open": round(price * 0.99, 3),
                "high": round(price * 1.02, 3),
                "low": round(price * 0.97, 3),
                "close": round(price, 3),
                "volume": volume,
                "amount": volume * price * 100,
            }
        )
    return rows


def pullback_bars(count: int = 150) -> list[dict]:
    """上涨趋势 + 尾部 4 天缩量回踩，且收盘仍在 MA20 上方。

    这组参数是标定出来的，不是随手写的——它要同时落在 `_check_pullback` 的三个
    门槛内：回调档位 shallow（1.09 个日波幅）、缩量 0.61（≤0.90）、距 MA20
    +2.6%（≥0）。任何一个滑出去，这只票就不该进候选，用例也就测不到选股路径。
    """
    return trending_bars(count, 0.008, pullback_days=4, pullback_drift=-0.006, pullback_volume=0.55)


STRONG = tuple(f"60000{i}.SH" for i in range(8))
WEAK = tuple(f"00000{i}.SZ" for i in range(8))


class FakeUniverses:
    def list(self):
        return [
            {"id": "CN_Equity_SW1_1", "name": "SW1电子", "symbol_count": len(STRONG)},
            {"id": "CN_Equity_SW1_2", "name": "SW1钢铁", "symbol_count": len(WEAK)},
        ]

    def get(self, universe_id):
        symbols = STRONG if universe_id.endswith("_1") else WEAK
        return {"id": universe_id, "symbols": list(symbols)}


class FakeSectorAdapter:
    def __init__(self, names: dict[str, str] | None = None) -> None:
        self.client = type("C", (), {"universes": FakeUniverses()})()
        self.batch_calls = 0
        self.names = names or {}

    def get_historical_bars_batch(self, symbols, start, end):
        self.batch_calls += 1
        # 强势股带缩量回踩尾部，弱势股一路阴跌。前者命中「回踩」打法，后者两条
        # 门槛都过不去——选股用例要的正是这个对比。
        return {
            symbol: (pullback_bars(150) if symbol in STRONG else trending_bars(150, -0.004))
            for symbol in symbols
        }

    def get_historical_bars(self, symbols, start, end):
        return self.get_historical_bars_batch(symbols, start, end)

    def get_security_master(self, symbols):
        return {symbol: {"name": self.names.get(symbol, f"股票{symbol[:6]}")} for symbol in symbols}


class FakeStaticTaxonomy:
    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.notes = ["静态分类缓存命中"]
        profiles = {}
        for index, symbol in enumerate(STRONG):
            profiles[symbol] = SimpleNamespace(
                name=f"芯片{index}",
                industries=("电子", "半导体", "存储"),
                concepts=("AI芯片", "算力", "先进封装"),
            )
        for index, symbol in enumerate(WEAK):
            profiles[symbol] = SimpleNamespace(
                name=f"钢铁{index}",
                industries=("钢铁", "普钢", "长材"),
                concepts=("周期",),
            )
        self.snapshot = SimpleNamespace(profiles=profiles)

    def list_sectors(self):
        if self.broken:
            raise RuntimeError("boom")
        return [
            SectorDefinition(name="静态电子", level="SW1", symbols=STRONG),
            SectorDefinition(name="静态钢铁", level="SW1", symbols=WEAK),
        ]


class FakeConceptTaxonomy:
    def list_sectors(self):
        return [SectorDefinition(name="AI芯片", level=CONCEPT_LEVEL, symbols=STRONG)]


class FakeNewsSource:
    def __init__(self) -> None:
        self.announcements: list[dict] = []

    def fetch_market(self, trade_date, limit=200):
        return [NewsDoc(title="半导体行业订单超预期，芯片涨价", source="财联社")], []

    def fetch_dragon_tiger(self, start, end):
        return [{"symbol": STRONG[0].split(".")[0], "net_buy": 2.0e8}], []

    def fetch_announcements(self, trade_date):
        return self.announcements, []


class MacroNewsSource(FakeNewsSource):
    def fetch_market(self, trade_date, limit=200):
        return [
            NewsDoc(title="布伦特原油涨3%，中东局势升级", source="财联社"),
            NewsDoc(title="中国7月官方制造业PMI 50.4，高于预期 49.8", source="华尔街见闻"),
        ], []


def limit_pool(limit_ups: int, blowups: int, streak: int, limit_downs: int):
    """真实的 `LimitPoolSnapshot`，不是 stub。

    炸板率、连板高度这些都是快照自己算的派生属性，手搓一个假对象只会把闸门测成
    「读了我塞进去的那个数」，测不到判定逻辑。
    """
    from ashare_decision_support.adapters.limit_pool import LimitPoolSnapshot, LimitStock

    return LimitPoolSnapshot(
        trade_date=date(2026, 7, 31),
        ok=True,
        limit_up=[
            LimitStock(symbol=f"{i:06d}.SZ", name=f"涨{i}", streak=streak if i == 0 else 1)
            for i in range(limit_ups)
        ],
        blowups=[LimitStock(symbol=f"8{i:05d}.SZ", name=f"炸{i}") for i in range(blowups)],
        limit_down=[LimitStock(symbol=f"9{i:05d}.SZ", name=f"跌{i}") for i in range(limit_downs)],
    )


def pool_adapter(snapshot):
    return type("A", (), {"fetch_recent": lambda self, day: snapshot})()


def test_a_freezing_tape_produces_no_candidates_at_all(tmp_path) -> None:
    """冰点不是「压低仓位」，是**不出手**。

    改造前这一层不存在：2026-08-03 炸板率 51.94%（系统自己的定义里已是退潮），
    当天照常输出 60 条建议、仓位系数 0.68。
    """
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=2,
        notifiers=[],
        trade_date=date(2026, 8, 3),
        limit_pool_adapter=pool_adapter(limit_pool(40, 21, 3, 22)),
        use_sector_index=False,
        use_market_scan=False,
    )

    assert summary["candidates"] == 0
    assert summary["position_factor"] == 0.0
    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    assert any("冰点" in note and "不产出" in note for note in payload["data_notes"])


def test_an_ebb_tape_still_picks_but_flags_leaders_only(tmp_path) -> None:
    """退潮期允许低吸，但只做龙头，且这句话必须出现在输出里。"""
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=2,
        notifiers=[],
        trade_date=date(2026, 8, 3),
        limit_pool_adapter=pool_adapter(limit_pool(99, 107, 9, 0)),
        use_sector_index=False,
        use_market_scan=False,
    )

    assert summary["candidates"] == 2
    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    assert any("退潮" in note and "龙头" in note for note in payload["data_notes"])
    # 每条候选都要带上它走的是哪套打法，否则台账里事后分不开。
    assert all(item.get("playbook") == "回踩" for item in payload["candidates"])


def test_sampled_morning_is_marked_as_diagnostic_mode(tmp_path) -> None:
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=2,
        max_constituents=6,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
    )

    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))

    assert summary["diagnostic_mode"] is True
    assert summary["diagnostic"]["mode"] == "sampled"
    assert summary["diagnostic"]["max_constituents"] == 6
    assert payload["diagnostic"]["sampled_symbols"] == summary["funnel"]["universe"]
    assert any("诊断模式" in note and "不可视为正式全量结论" in note for note in summary["data_notes"])


def test_morning_ranks_sectors_then_picks_stocks_inside_them(tmp_path) -> None:
    adapter = FakeSectorAdapter()
    summary = _run_morning(
        adapter=adapter,
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=2,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        # 这几个用例只测板块排序与选股。涨停池与同花顺行业指数都是**要打网络**的
        # 旁路读数，不关掉的话单测会真的出网——既慢又让结果依赖当天的行情。
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
    )

    assert summary["sectors_selected"] == ["电子"]
    assert summary["candidates"] == 2
    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    # Every candidate must belong to a sector that was actually selected.
    assert {item["sector"] for item in payload["candidates"]} == {"电子"}


def test_morning_ranks_concepts_separately_without_double_counting_market(tmp_path) -> None:
    """概念有独立名额；重叠成分不重复进漏斗，也不污染全市场宽度。"""
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        concept_taxonomy=FakeConceptTaxonomy(),
        top_sectors=1,
        top_concepts=1,
        candidates_per_sector=2,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
    )

    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    assert summary["industry_sectors_selected"] == ["电子"]
    assert summary["concept_sectors_selected"] == ["AI芯片"]
    assert payload["market"]["sectors_total"] == 2  # 两个行业；概念不重复计算大盘
    assert summary["funnel"]["selected_sector_constituents"] == len(STRONG)
    assert {item["level"] for item in payload["sectors"]} == {"SW1", CONCEPT_LEVEL}


def test_morning_keeps_st_names_for_the_normal_candidate_flow(tmp_path) -> None:
    adapter = FakeSectorAdapter(names={STRONG[0]: "*ST测试", STRONG[1]: "ST样本"})
    summary = _run_morning(
        adapter=adapter,
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=10,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        # 这几个用例只测板块排序与选股。涨停池与同花顺行业指数都是**要打网络**的
        # 旁路读数，不关掉的话单测会真的出网——既慢又让结果依赖当天的行情。
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
    )

    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    picked = {item["symbol"] for item in payload["candidates"]}

    assert STRONG[0] in picked and STRONG[1] in picked
    assert not any("ST/退市/非可交易板块过滤" in note for note in summary["data_notes"])


def test_morning_blocks_symbols_with_fatal_announcements(tmp_path) -> None:
    news = FakeNewsSource()
    news.announcements = [
        {
            "symbol": STRONG[0].split(".")[0],
            "category": "其他",
            "title": "关于收到证监会立案调查通知书的公告",
        }
    ]
    adapter = FakeSectorAdapter()
    summary = _run_morning(
        adapter=adapter,
        output_dir=tmp_path,
        news_source=news,
        top_sectors=1,
        candidates_per_sector=10,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        # 这几个用例只测板块排序与选股。涨停池与同花顺行业指数都是**要打网络**的
        # 旁路读数，不关掉的话单测会真的出网——既慢又让结果依赖当天的行情。
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
    )

    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    assert STRONG[0] not in {item["symbol"] for item in payload["candidates"]}
    assert any("公告风险否决" in note for note in summary["data_notes"])


class FakeEarnings:
    """业绩快照桩。只实现 `fetch`，`run_morning` 只用得到这一个方法。"""

    def __init__(self, rows):
        self.rows = rows
        self.asked: list = []

    def fetch(self, trade_date, period=None):
        from ashare_decision_support.adapters.earnings import EarningsAdapter

        self.asked.append(trade_date)

        class _Ak:
            def __init__(self, rows):
                self.rows = rows

            def stock_yjyg_em(self, date):
                return self.rows

            def stock_yjkb_em(self, date):
                return []

        return EarningsAdapter(ak_module=_Ak(self.rows)).fetch(trade_date, period=period)


def earnings_row(symbol: str, kind: str, pct: float) -> dict:
    return {
        "股票代码": symbol.split(".", maxsplit=1)[0],
        "股票简称": f"票{symbol}",
        "预测指标": "归属于上市公司股东的净利润",
        "预告类型": kind,
        "业绩变动幅度": pct,
        "公告日期": "2026-07-30",
        "业绩变动原因": "原因",
    }


def test_morning_removes_earnings_landmines_and_tags_the_beats(tmp_path) -> None:
    """业绩层的双面不对称，在**整条链路上**验一次。

    单测 screen 只能证明裁决对，证明不了裁决**接进了主链路**——盘前链路里
    最容易出的错就是「算了但没接」，那种错不会报错，只会让一层白建。
    """
    earnings = FakeEarnings(
        [
            earnings_row(STRONG[0], "首亏", -180.0),
            earnings_row(STRONG[1], "预增", 1197.0),
        ]
    )
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=10,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
        earnings_adapter=earnings,
    )

    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    picked = {item["symbol"]: item for item in payload["candidates"]}

    # 否决那一侧：首亏被移除。
    assert STRONG[0] not in picked
    assert summary["earnings_blocked"] >= 1
    assert any("业绩" in note and "否决" in note for note in summary["data_notes"])

    # 标注那一侧：预增留在候选里，理由带上业绩催化，**但没有改分数**。
    assert STRONG[1] in picked
    beat = picked[STRONG[1]]
    assert beat["earnings"]["forecast"]["kind"] == "预增"
    assert beat["earnings"]["strong_catalyst"] is True
    assert any("业绩催化" in reason for reason in beat["reasons"])
    assert summary["earnings_period"] == "20260630"


def test_morning_survives_a_dead_earnings_feed_without_vetoing_anything(tmp_path) -> None:
    """拿不到业绩去否决标的，等于让数据缺失冒充风险判断。"""

    class DeadEarnings:
        def fetch(self, trade_date, period=None):
            raise ConnectionError("feed down")

    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=10,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
        earnings_adapter=DeadEarnings(),
    )

    assert summary["candidates"] > 0
    assert summary["earnings_blocked"] == 0
    assert summary["earnings_period"] is None
    assert any("业绩预告不可用" in note for note in summary["data_notes"])


def test_morning_reads_rotation_from_disk_and_stamps_how_stale_it_is(tmp_path, monkeypatch) -> None:
    """轮动读的是已落盘的序列，所以**不该被 `--no-sector-index` 连带关掉**——
    那个开关关的是网络取数，这里只是一次磁盘读。

    代价是序列可能是旧的，而把昨天的轮动当成今天的正是这套系统别处一直在防的
    那类错误，所以它必须报出自己截止到哪一天。
    """
    from ashare_decision_support.adapters.ths_sector_index import HISTORY_SCHEMA

    home = tmp_path / "state" / "sector_index"
    home.mkdir(parents=True, exist_ok=True)
    for index in range(30):
        bars = [
            {
                "date": f"202607{day + 1:02d}",
                "open": 100.0, "high": 100.0, "low": 100.0,
                "close": 100.0 * (1.0 + 0.001 * index) ** day,
                "volume": 1.0, "amount": 100.0,
            }
            for day in range(30)
        ]
        (home / f"88{1100 + index}.json").write_text(
            json.dumps(
                {"schema": HISTORY_SCHEMA, "code": f"88{1100 + index}",
                 "name": f"板块{index}", "bars": bars},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))

    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=5,
        notifiers=[],
        trade_date=date(2026, 8, 3),
        use_limit_pool=False,
        use_sector_index=False,   # 网络取数关掉，轮动仍然要出
        use_market_scan=False,
    )

    assert summary["rotation_switch"] is not None
    assert any("板块轮动读数截止 20260730" in note for note in summary["data_notes"])
    assert any("早于本次交易日 20260803" in note for note in summary["data_notes"])


def test_earnings_vetoes_are_recorded_so_the_gate_can_be_audited(tmp_path) -> None:
    """业绩闸门是这套系统里唯一**明知有代价还是加了**的一道。

    它会挡掉亏损但正当红的题材股。不把被否的标的落台账，这笔代价就永远停留在
    设计者的自述里——被否决的标的在打分之前就消失了，台账里连名字都没有。

    记了之后，`journal/stats.py:_veto_cost` 才能在样本够时回答「如果买了会怎样」。
    """
    from ashare_decision_support.journal import Journal
    from ashare_decision_support.journal.records import KIND_VETOED

    journal = Journal(root=tmp_path / "journal")
    earnings = FakeEarnings(
        [
            earnings_row(STRONG[0], "首亏", -180.0),
            earnings_row(STRONG[1], "预增", 1197.0),
        ]
    )
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=10,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
        earnings_adapter=earnings,
        journal=journal,
    )

    rows = [
        row for row in journal.rows()
        if row.get("kind") == KIND_VETOED
    ]
    assert rows, "被业绩否决的标的必须落台账，否则闸门不可验证"
    vetoed = rows[0]
    assert vetoed["symbol"] == STRONG[0]
    # 参考价是兑现流程唯一必需的字段——没有它这条记录核对不了，等于没记。
    assert vetoed["reference_price"] > 0
    assert vetoed["earnings"]["kind"] == "首亏"
    assert any("业绩杀" in text for text in vetoed["warnings"])
    assert any("否决组" in note for note in summary["data_notes"])

    # 入选组带业绩读数，否则「带业绩催化的那批兑现率高多少」同样答不了。
    picked = [row for row in journal.rows() if row.get("kind") == "recommendation"]
    beat = next(row for row in picked if row["symbol"] == STRONG[1])
    assert beat["earnings"]["kind"] == "预增"
    assert beat["earnings"]["is_fresh"] is True


def test_morning_honours_a_manual_position_factor(tmp_path) -> None:
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        position_factor=0.3,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        # 这几个用例只测板块排序与选股。涨停池与同花顺行业指数都是**要打网络**的
        # 旁路读数，不关掉的话单测会真的出网——既慢又让结果依赖当天的行情。
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
    )

    assert summary["position_factor"] == 0.3
    # The suggestion is still reported so the two can be compared.
    assert summary["suggested_position_factor"] != 0.3
    assert any("手工值" in note for note in summary["data_notes"])


def test_morning_runs_without_any_news_input(tmp_path) -> None:
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=None,
        top_sectors=1,
        use_news=False,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        # 这几个用例只测板块排序与选股。涨停池与同花顺行业指数都是**要打网络**的
        # 旁路读数，不关掉的话单测会真的出网——既慢又让结果依赖当天的行情。
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
    )

    assert summary["sectors_selected"] == ["电子"]
    assert any("禁用舆情" in note for note in summary["data_notes"])


def test_morning_facts_and_brief_include_macro_links(tmp_path) -> None:
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=MacroNewsSource(),
        top_sectors=1,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
        use_llm_news=False,
    )

    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    macro = payload["macro_links"]
    assert macro["item_count"] >= 2
    assert any(item["topic"] == "oil" for item in macro["links"])
    assert any(item["topic"] == "china_macro" for item in macro["links"])
    assert "## 宏观联动" in summary["content"]


def test_morning_records_macro_context_into_the_journal(tmp_path) -> None:
    from ashare_decision_support.journal import Journal

    journal = Journal(root=tmp_path / "journal")
    _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=MacroNewsSource(),
        top_sectors=1,
        candidates_per_sector=1,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
        use_llm_news=False,
        journal=journal,
    )

    picked = [row for row in journal.rows() if row.get("kind") == "recommendation"]
    assert picked, "晨报候选必须落台账"
    macro = picked[0]["macro_context"]
    assert macro["recorded"] is True
    assert "oil" in macro["topics"]
    assert "china_macro" in macro["topics"]
    assert "china_macro:beat" in macro["expectations"]


def test_morning_adds_sector_rotation_memory_after_the_second_run(tmp_path) -> None:
    kwargs = {
        "adapter": FakeSectorAdapter(),
        "output_dir": tmp_path,
        "news_source": FakeNewsSource(),
        "top_sectors": 1,
        "candidates_per_sector": 1,
        "notifiers": [],
        "use_limit_pool": False,
        "use_sector_index": False,
        "use_market_scan": False,
    }
    _run_morning(trade_date=date(2026, 7, 31), **kwargs)
    summary = _run_morning(trade_date=date(2026, 8, 3), **kwargs)

    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    rotation = payload["rotation"]

    assert rotation["regime"] == "主线延续"
    assert rotation["persistent"] == ["电子"]
    assert "## 轮动记忆" in summary["content"]
    assert "电子" in summary["content"]


# ---- 舆情语料库接线 --------------------------------------------------------


def test_morning_archives_the_news_it_collected(tmp_path) -> None:
    """采到的舆情要真的进语料库——否则 `news/store.py` 就是又一个"已具备但没接入"。

    盘前链路是语料库唯一的写入点；不接这里，first_seen 永远没有数据可记。
    """
    from ashare_decision_support.news.store import NewsStore

    corpus = NewsStore(tmp_path / "corpus.sqlite3")
    _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=1,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
        news_store=corpus,
    )

    stored = corpus.recent(within_hours=24 * 365, by="last_seen")
    assert [item.title for item in stored] == ["半导体行业订单超预期，芯片涨价"]


def test_morning_keeps_first_seen_across_two_runs(tmp_path) -> None:
    """同一个早上跑两次（改参数重跑很常见），first_seen 必须停在第一次。"""
    from ashare_decision_support.news.store import NewsStore

    corpus = NewsStore(tmp_path / "corpus.sqlite3")
    kwargs = {
        "adapter": FakeSectorAdapter(), "output_dir": tmp_path, "news_source": FakeNewsSource(),
        "top_sectors": 1, "candidates_per_sector": 1, "notifiers": [], "trade_date": date(2026, 8, 1),
        "use_limit_pool": False, "use_sector_index": False, "use_market_scan": False,
        "news_store": corpus,
    }
    _run_morning(**kwargs)
    first = corpus.recent(within_hours=24 * 365, by="last_seen")[0].first_seen_at
    _run_morning(**kwargs)
    again = corpus.recent(within_hours=24 * 365, by="last_seen")[0]

    assert again.first_seen_at == first
    assert again.seen_count == 2


def test_morning_without_news_touches_no_corpus(tmp_path) -> None:
    """--no-news 时不该凭空建一个库文件。"""
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        top_sectors=1,
        candidates_per_sector=1,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_news=False,
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
    )

    assert summary["candidates"] >= 0
    assert not (tmp_path / "corpus.sqlite3").exists()


def test_morning_prefers_injected_static_taxonomy_and_persists_symbol_metadata(tmp_path) -> None:
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=1,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
        industry_taxonomy=FakeStaticTaxonomy(),
    )

    assert summary["candidates"] == 1
    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    row = payload["candidates"][0]

    assert row["sector"] == "静态电子"
    assert row["name"] == "芯片0"
    assert row["industry_path"] == "电子·半导体·存储"
    assert row["concepts"] == ["AI芯片", "算力", "先进封装"]
    assert any("同花顺静态 taxonomy" in note for note in payload["data_notes"])


def test_morning_falls_back_to_tickflow_when_static_taxonomy_breaks(tmp_path) -> None:
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=1,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
        industry_taxonomy=FakeStaticTaxonomy(broken=True),
    )

    assert summary["candidates"] == 1
    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    assert payload["candidates"][0]["sector"] == "电子"
    assert any("回退 TickFlow" in note for note in payload["data_notes"])


def test_morning_emits_progress_updates(tmp_path) -> None:
    messages: list[str] = []
    _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=1,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
        progress=messages.append,
    )

    assert any("正在并行采集舆情" in message for message in messages)
    assert any("盘前简报已完成" in message for message in messages)


def test_advise_emits_progress_updates(tmp_path) -> None:
    from ashare_decision_support.jobs import run_advise

    def minute_bars(count: int, drift: float) -> list[dict]:
        price = 20.0
        rows: list[dict] = []
        day = date(2026, 1, 5)
        for index in range(count):
            price = max(price * (1 + drift), 1.0)
            rows.append(
                {
                    "timestamp": datetime.combine(day, datetime.min.time()) + timedelta(minutes=5 * index),
                    "open": round(price * 0.99, 3),
                    "high": round(price * 1.02, 3),
                    "low": round(price * 0.97, 3),
                    "close": round(price, 3),
                    "volume": 100000 + index * 50,
                    "amount": (100000 + index * 50) * price * 100,
                }
            )
        return rows

    class FakeAdviseAdapter:
        def get_historical_bars(self, symbols, start, end):
            return {symbol: trending_bars(120, 0.003) for symbol in symbols}

        def get_security_master(self, symbols):
            return {symbol: {"name": f"股票{symbol[:6]}"} for symbol in symbols}

    class FakeIntraday:
        def get_minute_bars(self, symbols, period="5", **kwargs):
            return {symbol: minute_bars(48, 0.001) for symbol in symbols}, []

    messages: list[str] = []
    run_advise(
        adapter=FakeAdviseAdapter(),
        symbols=["600000.SH"],
        intraday_adapter=FakeIntraday(),
        use_news=False,
        use_order_book=False,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        progress=messages.append,
        output_dir=tmp_path,
    )

    assert any("正在计算" in message for message in messages)
    assert any("个股分析已完成" in message for message in messages)


# ---- 逐笔资金旁证 ---------------------------------------------------------


class FakeTradeFlow:
    """按入参返回逐笔摘要。记录被问到的标的，用来验证取数范围。"""

    def __init__(self, payload: dict[str, dict]) -> None:
        self.payload = payload
        self.asked: list[str] = []

    def get_trade_flow(self, symbols):
        self.asked = list(symbols)
        return (
            {symbol: self.payload[symbol] for symbol in symbols if symbol in self.payload},
            ["逐笔说明一条"],
        )


def test_morning_attaches_trade_flow_only_to_the_final_candidates(tmp_path) -> None:
    """逐笔只挂在最终候选上，且**只对候选取数**。

    铺到粗排那几百只会把盘前链路拖垮——一只票要翻约 67 页明细。所以这里既断言
    挂上了，也断言问的就是候选那几只、没有多问。
    """
    flow = FakeTradeFlow(
        {
            STRONG[0]: {"ok": True, "buy_ratio": 0.61, "net_amount": 1.2e8, "large_net_amount": 9e7},
            STRONG[1]: {"ok": True, "buy_ratio": 0.48, "net_amount": -3e7, "large_net_amount": -2e7},
        }
    )
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=2,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
        trade_flow_adapter=flow,
    )

    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    picked = [item["symbol"] for item in payload["candidates"]]
    assert summary["candidates"] == 2
    # 问的就是候选那几只，一只不多。
    assert flow.asked == picked
    attached = [item for item in payload["candidates"] if item.get("trade_flow")]
    assert attached, "逐笔摘要没有挂到任何候选上"
    assert all(item["trade_flow"]["ok"] for item in attached)
    assert "逐笔说明一条" in payload["data_notes"]


def test_morning_without_trade_flow_says_so_instead_of_going_quiet(tmp_path) -> None:
    """关掉时必须留一句话。少了这一层的简报读起来完全正常，只是没有那几行——
    静默缺失正是这套系统在别处一直防的那类失败。"""
    _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=2,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
        use_trade_flow=False,
    )

    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    assert any("已关闭逐笔资金旁证" in note for note in payload["data_notes"])
    assert not any(item.get("trade_flow") for item in payload["candidates"])


# ---- 台账里的大盘快照 -----------------------------------------------------


def test_market_state_carries_the_unscored_layers(tmp_path) -> None:
    """只出读数、不进打分的那几层**必须**进台账。

    存进去是它们唯一能被验证的途径：不存的话 `review` 永远回答不了「高低切换的
    日子里这套筛选的兑现率有没有差别」，于是它们也永远谈不上要不要拿权重——
    项目自己那条「先进简报与台账，等 review 能回答再谈给分」就断在第二步。
    """
    from ashare_decision_support.jobs.morning_facts import market_state

    state = market_state(
        SimpleNamespace(direction="偏多", score=88.85, breadth=0.7613),
        0.66,
        None,
        None,
        rotation_price=SimpleNamespace(
            ok=True,
            switch_label="强者恒强",
            adjacent_label="高低切换",
            persistence=0.2,
            trade_date="20260803",
        ),
        turnover=SimpleNamespace(ok=True, total_change=-0.144),
        broad_index=SimpleNamespace(ok=True, median_ret=-0.0098, trade_date="2026-08-03"),
    )

    # 两个轮动标签都要存。它们窗口不同、2026-08-03 实测反号，只存一个的话事后
    # 分组会把两种截然不同的盘面混进同一组。
    assert state["rotation_switch"] == "强者恒强"
    assert state["rotation_adjacent"] == "高低切换"
    assert state["rotation_date"] == "20260803"
    assert state["turnover_total_change"] == -0.144
    # 等权 breadth 与市值加权中位数并存，才能事后把「等权强、加权弱」的日子拎出来。
    assert state["breadth"] == 0.7613
    assert state["index_median_ret"] == -0.0098


def test_market_state_records_absence_instead_of_a_neutral_number(tmp_path) -> None:
    """取不到时存 None，不存 0。

    0 会被事后统计当成「测出来是 0」，而那与「没测」是相反的两件事——同一条
    区分贯穿这套系统（见 `MarketOutlook` 的「数据不足」不给方向）。
    """
    from ashare_decision_support.jobs.morning_facts import market_state

    state = market_state(SimpleNamespace(direction="偏多", score=80.0, breadth=0.6), 0.5, None, None)

    for key in (
        "rotation_switch",
        "rotation_adjacent",
        "rotation_date",
        "turnover_total_change",
        "index_median_ret",
    ):
        assert state[key] is None, key


def test_market_state_ignores_layers_that_reported_not_ok(tmp_path) -> None:
    """`ok=False` 的读数一律当没有。半成品读数进了台账比没有更糟——
    它看起来是一个可分组的维度，实际是噪声。"""
    from ashare_decision_support.jobs.morning_facts import market_state

    state = market_state(
        SimpleNamespace(direction="偏多", score=80.0, breadth=0.6),
        0.5,
        None,
        None,
        rotation_price=SimpleNamespace(ok=False, switch_label="样本不足", adjacent_label="样本不足"),
        turnover=SimpleNamespace(ok=False, total_change=-0.9),
        broad_index=SimpleNamespace(ok=False, median_ret=-0.5, trade_date="2026-08-03"),
    )

    assert state["rotation_switch"] is None
    assert state["turnover_total_change"] is None
    assert state["index_median_ret"] is None


# ---- 简报里的成交额标题行 --------------------------------------------------


def _turnover_headline(turnover: dict) -> str:
    """跑一遍简报，把成交额那节的加粗标题行取出来。"""
    from ashare_decision_support.notify.render import render_sector_brief

    markdown = render_sector_brief(
        date(2026, 8, 7),
        [],
        [],
        SimpleNamespace(direction="偏多", score=60.0, breadth=0.5, reasons=[]),
        0.5,
        turnover=turnover,
    )
    return next(line for line in markdown.splitlines() if line.startswith("**全场日均成交额"))


def test_turnover_headline_carries_the_turn_when_the_two_reads_disagree() -> None:
    """块均值与窗口内走向反号时，两个数都要出现在标题行里。

    标题行是这一节唯一会被扫一眼就走的行。2026-08-07 实盘：块均值 -1.8%、窗口内
    +33.4%——只印前者，量能刚触底回升的那一周，最显眼的一行印的是「萎缩」。
    """
    headline = _turnover_headline(
        {
            "sectors": 90,
            "trade_date": "20260807",
            "total_change": -0.0176,
            "total_trend": 0.3343,
            "observations": ["近 5 日**内部**放量 33.4%"],
        }
    )

    assert "-1.8%" in headline
    assert "+33.4%" in headline


def test_turnover_headline_stays_short_when_the_two_reads_agree() -> None:
    """同号时不加那半句——没有拐点要提示，多印一个数只会挤掉块均值本身。"""
    headline = _turnover_headline(
        {
            "sectors": 90,
            "trade_date": "20260807",
            "total_change": -0.144,
            "total_trend": -0.21,
            "observations": ["近 5 日**内部**缩量 21.0%"],
        }
    )

    assert "-14.4%" in headline
    assert "窗口内" not in headline


# ---- 台账里的仓位决策过程 --------------------------------------------------


def _sizing_state(**kw):
    """跑一次真实的仓位决策，把结果原样喂给台账快照。

    刻意走 `resolve_position_sizing` 而不是手搓一个 SimpleNamespace：这条用例要
    钉的是「决策过程能不能被事后还原」，用假 plan 的话字段名一漂就测不出来。
    """
    from ashare_decision_support.jobs.morning_facts import market_state
    from ashare_decision_support.strategy.positioning import resolve_position_sizing

    plan = resolve_position_sizing(**kw)
    return market_state(
        SimpleNamespace(direction="偏多", score=70.0, breadth=0.6),
        plan.effective_factor,
        None,
        sizing=plan,
    )


def test_journal_can_tell_a_capped_suggestion_from_a_naturally_low_one() -> None:
    """两种决策的 `position_factor` **完全一样**，只有过程分得开。

    系统自己就建议 0.5，和系统建议 0.9 被周期上限压到 0.5，对「仓位上限这条规则
    有没有救过我们」是相反的证据。只存最终值的话，这个问题永远没有样本——而那是
    仓位这一层唯一能被 `review` 验证的方式。
    """
    capped = _sizing_state(cycle_label="退潮", cycle_cap=0.5, suggested_factor=0.9)
    naturally_low = _sizing_state(cycle_label="退潮", cycle_cap=0.5, suggested_factor=0.5)

    # 最终值相同——这正是问题所在。
    assert capped["position_factor"] == naturally_low["position_factor"] == 0.5

    # 过程不同，且台账分得开。
    assert capped["suggested_factor"] == 0.9
    assert capped["capped_suggested_factor"] == 0.5
    assert naturally_low["suggested_factor"] == 0.5
    assert naturally_low["capped_suggested_factor"] == 0.5


def test_journal_records_a_manual_override_as_such() -> None:
    """人接管过的那一天要留痕，否则事后会被算成规则的战绩（或锅）。"""
    state = _sizing_state(
        cycle_label="启动", cycle_cap=0.5, suggested_factor=0.4, manual_factor=0.8
    )

    assert state["position_factor"] == 0.8      # 手工值优先
    assert state["manual_factor"] == 0.8
    assert state["capped_suggested_factor"] == 0.4  # 系统本来想给的
    assert state["suggested_factor"] == 0.4


def test_journal_stores_none_when_no_sizing_plan_was_passed() -> None:
    """拿不到 plan 时存 None，不存 0——同这份快照里其他读数的处置。"""
    from ashare_decision_support.jobs.morning_facts import market_state

    state = market_state(SimpleNamespace(direction="偏多", score=70.0, breadth=0.6), 0.5, None)

    assert state["position_factor"] == 0.5
    assert state["suggested_factor"] is None
    assert state["manual_factor"] is None


def test_journal_keeps_both_turnover_readings() -> None:
    """块均值与窗口内走向都要存。

    只存块均值的话，事后分组会把「量能刚触底回升」的日子归进「存量博弈」——
    2026-08-07 实测这两个数是 -1.8% 与 +33.4%，反号。
    """
    from ashare_decision_support.jobs.morning_facts import market_state

    state = market_state(
        SimpleNamespace(direction="偏多", score=70.0, breadth=0.6),
        0.5,
        None,
        turnover=SimpleNamespace(ok=True, total_change=-0.0176, total_trend=0.3343),
    )

    assert state["turnover_total_change"] == -0.0176
    assert state["turnover_total_trend"] == 0.3343


# ---- 概念层的两处边界 ------------------------------------------------------


class ClashingConceptTaxonomy:
    """概念名与行业名撞车。实测 31 个 SW1 与 346 个概念只撞「家用电器」一个。"""

    def list_sectors(self):
        return [
            SectorDefinition(name="电子", level=CONCEPT_LEVEL, symbols=WEAK),
            SectorDefinition(name="AI芯片", level=CONCEPT_LEVEL, symbols=STRONG),
        ]


class BrokenConceptTaxonomy:
    def list_sectors(self):
        raise RuntimeError("static file unreachable")


def test_a_concept_named_like_an_industry_is_dropped_not_merged(tmp_path) -> None:
    """板块的各层读数全部按 `name` 索引，重名会让两层互相覆盖。

    覆盖的方向取决于字典写入顺序——那是一个没人声明过、也没法从代码里读出来的
    规则，而被覆盖的那一层会拿到另一层的动量与舆情。丢掉重名的那个概念，代价
    远小于让一层读数静默错位。
    """
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        concept_taxonomy=ClashingConceptTaxonomy(),
        top_sectors=1,
        top_concepts=2,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
    )

    # 「电子」只作为行业出现一次，不会同时出现在概念名额里。
    assert summary["concept_sectors_selected"] == ["AI芯片"]
    assert summary["industry_sectors_selected"] == ["电子"]
    assert any("重名已跳过" in note for note in summary["data_notes"])


def test_a_broken_concept_source_leaves_the_industry_ranking_alone(tmp_path) -> None:
    """概念是第二个维度，不是必需项——静态文件挂掉时盘前照样要出得来一份排序。

    对照 `_industry_taxonomy`：那条取不到是要抛的，因为没有行业分类这条链路
    根本无从开始。两者的失败处置刻意不同。
    """
    summary = _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        concept_taxonomy=BrokenConceptTaxonomy(),
        top_sectors=1,
        top_concepts=3,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
    )

    assert summary["industry_sectors_selected"] == ["电子"]
    assert summary["concept_sectors_selected"] == []
    assert any("概念板块不可用" in note for note in summary["data_notes"])


def test_the_daemon_ranks_concepts_too(tmp_path) -> None:
    """**daemon 才是这套系统真正在跑的那条路。**

    手工 `morning` 有概念维度、无人值守跑没有，是同一套分类下两条链路看到的板块
    集合不一样——`docs/INVENTORY.md` §1.1 记的正是这种分叉。结构守卫而非行为
    用例：真跑一次 daemon 要连全部外部源。
    """
    import ast
    import inspect

    from ashare_decision_support.jobs import daily_cycle

    tree = ast.parse(inspect.getsource(daily_cycle))
    morning = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "morning_task"
    )
    passed = {
        keyword.arg
        for node in ast.walk(morning)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    }
    assert {"industry_taxonomy", "concept_taxonomy"} <= passed

    # 两层必须共用一个 source，否则上游 08:00 刷新时行业按昨天算、概念按今天算。
    called = {
        node.func.id
        for node in ast.walk(morning)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "static_taxonomies" in called


def test_the_sector_news_memory_reaches_the_facts_of_a_real_morning_run(tmp_path) -> None:
    """跨日那一维要真的出现在盘前 facts 里。

    单测已经钉住 `_sector_news_memory` 自己的口径，但它接没接上是另一回事——
    `NewsStore.sector_heat` / `recent` 在此之前写了两个月、有测试、零生产调用方，
    正是「有测试不等于系统在用」的样子。这条从 `run_morning` 出发。
    """
    from ashare_decision_support.news.store import NewsStore

    corpus = NewsStore(tmp_path / "corpus.sqlite3")
    _run_morning(
        adapter=FakeSectorAdapter(),
        output_dir=tmp_path,
        news_source=FakeNewsSource(),
        top_sectors=1,
        candidates_per_sector=1,
        notifiers=[],
        trade_date=date(2026, 8, 1),
        use_limit_pool=False,
        use_sector_index=False,
        use_market_scan=False,
        news_store=corpus,
    )

    # 断言落盘的那一份：`ask` / `facts` / MCP 读到的正是它。
    payload = json.loads((tmp_path / "morning_latest.json").read_text(encoding="utf-8"))
    memory = payload["sector_news_memory"]
    assert [item["sector"] for item in memory] == ["电子"]
    block = memory[0]
    assert block["fresh_docs_total"] == 1
    assert block["still_developing"][0]["title"] == "半导体行业订单超预期，芯片涨价"
    # 两个时间戳原样给出，读的人自己减——落盘之后隔天再读，「3 小时前」就是假的。
    assert block["still_developing"][0]["first_seen"] <= block["still_developing"][0]["last_seen"]
