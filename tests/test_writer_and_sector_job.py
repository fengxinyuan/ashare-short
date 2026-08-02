from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from ashare_decision_support.jobs.morning import _sample_across_sectors, run_morning
from ashare_decision_support.news import NewsDoc
from ashare_decision_support.sectors.base import SectorDefinition
from ashare_decision_support.writer import (
    LlmBriefWriter,
    TemplateWriter,
    WriteRequest,
    unverified_numbers,
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


def trending_bars(count: int, drift: float) -> list[dict]:
    price = 20.0
    rows: list[dict] = []
    day = date(2026, 1, 5)
    for index in range(count):
        price = max(price * (1 + drift), 1.0)
        rows.append(
            {
                "timestamp": datetime.combine(day + timedelta(days=index), datetime.min.time()),
                "open": round(price * 0.99, 3),
                "high": round(price * 1.02, 3),
                "low": round(price * 0.97, 3),
                "close": round(price, 3),
                "volume": 100000 + index * 50,
                "amount": (100000 + index * 50) * price * 100,
            }
        )
    return rows


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
        return {
            symbol: trending_bars(150, 0.004 if symbol in STRONG else -0.004)
            for symbol in symbols
        }

    def get_historical_bars(self, symbols, start, end):
        return self.get_historical_bars_batch(symbols, start, end)

    def get_security_master(self, symbols):
        return {symbol: {"name": self.names.get(symbol, f"股票{symbol[:6]}")} for symbol in symbols}


class FakeNewsSource:
    def __init__(self) -> None:
        self.announcements: list[dict] = []

    def fetch_market(self, trade_date, limit=200):
        return [NewsDoc(title="半导体行业订单超预期，芯片涨价", source="财联社")], []

    def fetch_dragon_tiger(self, start, end):
        return [{"symbol": STRONG[0].split(".")[0], "net_buy": 2.0e8}], []

    def fetch_announcements(self, trade_date):
        return self.announcements, []


def test_morning_ranks_sectors_then_picks_stocks_inside_them(tmp_path) -> None:
    adapter = FakeSectorAdapter()
    summary = run_morning(
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


def test_morning_drops_st_names_before_scoring(tmp_path) -> None:
    adapter = FakeSectorAdapter(names={STRONG[0]: "*ST测试", STRONG[1]: "ST样本"})
    summary = run_morning(
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

    assert STRONG[0] not in picked and STRONG[1] not in picked
    assert any("ST" in note for note in summary["data_notes"])


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
    summary = run_morning(
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


def test_morning_honours_a_manual_position_factor(tmp_path) -> None:
    summary = run_morning(
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
    summary = run_morning(
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


# ---- 舆情语料库接线 --------------------------------------------------------


def test_morning_archives_the_news_it_collected(tmp_path) -> None:
    """采到的舆情要真的进语料库——否则 `news/store.py` 就是又一个"已具备但没接入"。

    盘前链路是语料库唯一的写入点；不接这里，first_seen 永远没有数据可记。
    """
    from ashare_decision_support.news.store import NewsStore

    corpus = NewsStore(tmp_path / "corpus.sqlite3")
    run_morning(
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
    kwargs = dict(
        adapter=FakeSectorAdapter(), output_dir=tmp_path, news_source=FakeNewsSource(),
        top_sectors=1, candidates_per_sector=1, notifiers=[], trade_date=date(2026, 8, 1),
        use_limit_pool=False, use_sector_index=False, use_market_scan=False,
        news_store=corpus,
    )
    run_morning(**kwargs)
    first = corpus.recent(within_hours=24 * 365, by="last_seen")[0].first_seen_at
    run_morning(**kwargs)
    again = corpus.recent(within_hours=24 * 365, by="last_seen")[0]

    assert again.first_seen_at == first
    assert again.seen_count == 2


def test_morning_without_news_touches_no_corpus(tmp_path) -> None:
    """--no-news 时不该凭空建一个库文件。"""
    summary = run_morning(
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
