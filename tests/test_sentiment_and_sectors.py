from __future__ import annotations

from datetime import date, datetime, timedelta

from ashare_decision_support.news import LexiconSentiment, NewsCache, NewsDoc
from ashare_decision_support.news.akshare_source import CJZC_LIMIT, AkshareNewsSource
from ashare_decision_support.screens import AnnouncementScreen
from ashare_decision_support.sectors import (
    SectorMetrics,
    SectorMomentum,
    SectorPredictor,
    SectorRotationStore,
    TickFlowSwTaxonomy,
)
from ashare_decision_support.sectors.base import SectorDefinition

# ---- sentiment -----------------------------------------------------------


def test_lexicon_separates_bullish_from_bearish() -> None:
    analyzer = LexiconSentiment()
    good = analyzer.score_text("公司中标大额订单，业绩预增超预期")
    bad = analyzer.score_text("公司被立案调查，控股股东清仓式减持")

    assert good.polarity > 0.5
    assert bad.polarity < -0.5
    assert "中标" in good.bullish_hits
    assert "立案调查" in bad.bearish_hits


def test_lexicon_flips_polarity_on_negation() -> None:
    analyzer = LexiconSentiment()
    plain = analyzer.score_text("公司中标某项目")
    negated = analyzer.score_text("公司未中标该项目")

    assert plain.polarity > 0
    assert negated.polarity < 0


def test_lexicon_prefers_the_longest_matching_term() -> None:
    """'限售股解禁' must not also bill the shorter '解禁' for the same span."""
    analyzer = LexiconSentiment()
    result = analyzer.score_text("下周限售股解禁")

    assert result.bearish_hits == ("限售股解禁",)


def test_lexicon_drops_boilerplate_filings() -> None:
    analyzer = LexiconSentiment()
    assert analyzer.score_text("某公司第四届董事会第八次会议决议公告").is_neutral


def test_lexicon_amplifier_increases_magnitude() -> None:
    analyzer = LexiconSentiment()
    plain = analyzer.score_text("公司亏损")
    amplified = analyzer.score_text("公司大幅亏损")

    assert amplified.intensity > plain.intensity


def test_score_docs_weights_by_intensity_not_by_count() -> None:
    analyzer = LexiconSentiment()
    docs = [
        NewsDoc(title="公司被立案调查涉嫌财务造假，股票退市风险警示"),
        NewsDoc(title="行业景气"),
    ]
    assert analyzer.score_docs(docs).polarity < 0


def test_announcement_screen_separates_blocking_from_warning() -> None:
    verdicts = AnnouncementScreen().evaluate(
        [
            {"symbol": "000001", "category": "其它风险提示公告", "title": "关于股票被实施其他风险警示的公告"},
            {"symbol": "000002", "category": "其他", "title": "关于收到证监会立案调查通知书的公告"},
            {"symbol": "000003", "category": "股票交易异常波动", "title": "股票交易异常波动公告"},
            {"symbol": "000004", "category": "董事会决议公告", "title": "第四届董事会决议公告"},
        ]
    )

    assert verdicts["000001"].is_blocked
    assert verdicts["000002"].is_blocked
    assert not verdicts["000003"].is_blocked and verdicts["000003"].warnings
    assert "000004" not in verdicts


# ---- sector momentum -----------------------------------------------------


def bars_with_returns(returns: list[float], base_volume: float = 1000.0) -> list[dict]:
    price = 10.0
    rows: list[dict] = []
    day = date(2026, 1, 5)
    for index, ret in enumerate(returns):
        price *= 1 + ret
        rows.append(
            {
                "timestamp": datetime.combine(day + timedelta(days=index), datetime.min.time()),
                "open": price,
                "high": price,
                "low": price,
                "close": round(price, 4),
                "volume": base_volume,
            }
        )
    return rows


def test_sector_momentum_uses_median_not_mean() -> None:
    """One runaway constituent must not define the sector's reading."""
    flat = [0.0] * 25
    definition = SectorDefinition(name="测试", level="SW1", symbols=("A", "B", "C"))
    bars = {
        "A": bars_with_returns([*flat, 0.001]),
        "B": bars_with_returns([*flat, 0.002]),
        "C": bars_with_returns([*flat, 0.5]),
    }

    metrics = SectorMomentum().measure([definition], bars)["测试"]

    assert metrics.covered == 3
    assert metrics.ret_1d is not None and metrics.ret_1d < 0.01
    assert metrics.strong_count == 1
    assert metrics.breadth == 1.0


def test_sector_momentum_reports_partial_coverage() -> None:
    definition = SectorDefinition(name="测试", level="SW1", symbols=("A", "B", "C", "D"))
    metrics = SectorMomentum().measure([definition], {"A": bars_with_returns([0.0] * 30)})["测试"]

    assert metrics.constituents == 4
    assert metrics.covered == 1
    assert metrics.coverage_ratio == 0.25


# ---- sector ranking ------------------------------------------------------


def build_metrics(definitions, bars):
    return SectorMomentum().measure(definitions, bars)


def test_predictor_ranks_the_stronger_sector_first() -> None:
    strong = SectorDefinition(name="电子", level="SW1", symbols=tuple(f"S{i}" for i in range(6)))
    weak = SectorDefinition(name="钢铁", level="SW1", symbols=tuple(f"W{i}" for i in range(6)))
    bars: dict[str, list[dict]] = {}
    for symbol in strong.symbols:
        bars[symbol] = bars_with_returns([0.01] * 25)
    for symbol in weak.symbols:
        bars[symbol] = bars_with_returns([-0.01] * 25)

    definitions = [strong, weak]
    outlooks = SectorPredictor(LexiconSentiment()).predict(
        definitions, build_metrics(definitions, bars)
    )

    assert [item.name for item in outlooks] == ["电子", "钢铁"]
    assert outlooks[0].score > outlooks[1].score


def test_predictor_attributes_news_by_expanded_keywords() -> None:
    """'光伏' must reach 电力设备 even though the sector name never appears."""
    definition = SectorDefinition(name="电力设备", level="SW1", symbols=tuple(f"S{i}" for i in range(6)))
    bars = {symbol: bars_with_returns([0.0] * 25) for symbol in definition.symbols}
    docs = [NewsDoc(title="光伏组件价格上涨，龙头企业订单超预期")]

    outlook = SectorPredictor(LexiconSentiment()).predict(
        [definition], build_metrics([definition], bars), docs
    )[0]

    assert outlook.sentiment_polarity > 0
    assert outlook.headlines == ["光伏组件价格上涨，龙头企业订单超预期"]


def test_predictor_ignores_untrusted_coverage_in_momentum() -> None:
    thin = SectorDefinition(name="综合", level="SW1", symbols=tuple(f"T{i}" for i in range(20)))
    bars = {"T0": bars_with_returns([0.09] * 25)}

    outlook = SectorPredictor(LexiconSentiment()).predict([thin], build_metrics([thin], bars))[0]

    assert outlook.score == 50.0
    assert any("不可信" in warning for warning in outlook.warnings)


def test_market_sentiment_weights_breadth_by_coverage() -> None:
    """A 1-of-20 sector must not outvote a fully covered one."""
    wide = SectorDefinition(name="电子", level="SW1", symbols=tuple(f"S{i}" for i in range(10)))
    thin = SectorDefinition(name="综合", level="SW1", symbols=tuple(f"T{i}" for i in range(20)))
    bars = {symbol: bars_with_returns([-0.01] * 25) for symbol in wide.symbols}
    bars["T0"] = bars_with_returns([0.05] * 25)

    definitions = [wide, thin]
    market = SectorPredictor(LexiconSentiment()).market_sentiment(
        [], build_metrics(definitions, bars)
    )

    # Every trusted constituent fell, so the suggestion must sit at the floor.
    assert market.suggested_position_factor <= 0.3
    assert any("建议值" in reason for reason in market.reasons)


def test_market_sentiment_falls_back_to_neutral_without_coverage() -> None:
    thin = SectorDefinition(name="综合", level="SW1", symbols=tuple(f"T{i}" for i in range(20)))
    market = SectorPredictor(LexiconSentiment()).market_sentiment(
        [], build_metrics([thin], {"T0": bars_with_returns([0.0] * 25)})
    )

    assert market.suggested_position_factor == 0.5


def test_market_sentiment_never_suggests_a_full_book() -> None:
    """Everything up, every day — still capped. No unbacktested reading gets 1.0."""
    sector = SectorDefinition(name="电子", level="SW1", symbols=tuple(f"S{i}" for i in range(10)))
    bars = {symbol: bars_with_returns([0.02] * 25) for symbol in sector.symbols}

    market = SectorPredictor(LexiconSentiment()).market_sentiment(
        [], build_metrics([sector], bars)
    )

    assert market.suggested_position_factor <= 0.8


def test_market_sentiment_cuts_the_suggestion_when_the_move_is_extended() -> None:
    """A week of gains with everything green is a late entry, not a green light."""
    sector = SectorDefinition(name="电子", level="SW1", symbols=tuple(f"S{i}" for i in range(10)))
    extended = {symbol: bars_with_returns([0.025] * 25) for symbol in sector.symbols}
    fresh = {symbol: bars_with_returns([0.0] * 20 + [0.001] * 5) for symbol in sector.symbols}

    predictor = SectorPredictor(LexiconSentiment())
    hot = predictor.market_sentiment([], build_metrics([sector], extended))
    calm = predictor.market_sentiment([], build_metrics([sector], fresh))

    assert hot.suggested_position_factor < calm.suggested_position_factor
    assert any("普涨末段" in reason for reason in hot.reasons)


def test_sector_rotation_memory_marks_persistent_and_fresh_leaders(tmp_path) -> None:
    def outlook(name: str, score: float) -> object:
        metrics = SectorMetrics(name=name, constituents=10, covered=10, ret_1d=0.01, ret_5d=0.03)
        from ashare_decision_support.sectors.base import SectorOutlook

        return SectorOutlook(name=name, score=score, metrics=metrics)

    store = SectorRotationStore(tmp_path / "rotation.json")
    store.record(date(2026, 7, 30), "SW1", [
        outlook("电子", 80.0),
        outlook("电力设备", 78.0),
        outlook("券商", 72.0),
    ])
    store.record(date(2026, 7, 31), "SW1", [
        outlook("电力设备", 81.0),
        outlook("创新药", 77.0),
        outlook("电子", 74.0),
    ])
    summary = store.remember(date(2026, 8, 3), "SW1", [
        outlook("电力设备", 82.0),
        outlook("创新药", 79.0),
        outlook("家用电器", 75.0),
    ], lookback_days=5, leader_count=3)

    assert summary.regime == "主线延续"
    assert summary.persistent == ("电力设备", "创新药")
    assert summary.new_entries == ("家用电器",)
    assert any("昨天靠前" in text for text in summary.observations)


# ---- taxonomy ------------------------------------------------------------


class FakeUniverses:
    """Mimics TickFlow's sharded universe listing."""

    def __init__(self) -> None:
        self.get_calls = 0
        self._detail = {
            "CN_Equity_SW1_1": ["600000.SH", "600001.SH", "600002.SH"],
            "CN_Equity_SW1_2": ["600002.SH", "600003.SH", "600004.SH"],
            "CN_Equity_SW2_9": ["600009.SH"],
        }

    def list(self):
        return [
            {"id": "CN_Equity_SW1_1", "name": "SW1电子", "symbol_count": 3},
            {"id": "CN_Equity_SW1_2", "name": "SW1电子", "symbol_count": 3},
            {"id": "CN_Equity_SW2_9", "name": "SW2半导体", "symbol_count": 1},
        ]

    def get(self, universe_id):
        self.get_calls += 1
        return {"id": universe_id, "symbols": self._detail.get(universe_id, [])}


class FakeClient:
    def __init__(self) -> None:
        self.universes = FakeUniverses()


def test_taxonomy_merges_shards_and_dedupes_constituents() -> None:
    client = FakeClient()
    sectors = TickFlowSwTaxonomy(client, level="SW1", min_constituents=1).list_sectors()

    assert len(sectors) == 1
    assert sectors[0].name == "电子"
    # 600002.SH appears in both shards and must be counted once.
    assert sectors[0].symbols == ("600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH")


def test_taxonomy_caches_to_disk_and_skips_refetch(tmp_path) -> None:
    client = FakeClient()
    first = TickFlowSwTaxonomy(client, level="SW1", cache_dir=tmp_path, min_constituents=1)
    first.list_sectors()
    calls_after_first = client.universes.get_calls

    second = TickFlowSwTaxonomy(client, level="SW1", cache_dir=tmp_path, min_constituents=1)
    sectors = second.list_sectors()

    assert client.universes.get_calls == calls_after_first
    assert sectors[0].name == "电子"


# ---- news collection -----------------------------------------------------


class FakeAk:
    """AkShare stub where individual feeds can be made to fail."""

    def __init__(self, broken: set[str] | None = None) -> None:
        self.broken = broken or set()
        self.calls: list[str] = []

    def _guard(self, name: str):
        self.calls.append(name)
        if name in self.broken:
            raise ConnectionError("feed down")

    def stock_info_global_cls(self, symbol):
        self._guard("cls")
        return [{"标题": "央行降准", "内容": "释放流动性", "发布日期": "2026-08-01", "发布时间": "09:00"}]

    def stock_info_global_em(self):
        self._guard("em")
        return [{"标题": "光伏涨价", "摘要": "组件提价", "发布时间": "2026-08-01", "链接": "http://x"}]

    def stock_info_global_ths(self):
        self._guard("ths")
        return [{"标题": "储能中标", "内容": "正文", "发布时间": "2026-08-01 08:30:00",
                 "链接": "http://t"}]

    def stock_info_global_sina(self):
        self._guard("sina")
        # 这一路真的没有「标题」列，只有时间与内容——测试必须照实模拟，
        # 否则「内容当标题」那条兜底永远走不到。
        return [{"时间": "2026-08-01 08:40:00", "内容": "统计局公布 7 月 PMI 为 50.2"}]

    def stock_info_cjzc_em(self):
        self._guard("cjzc")
        return [
            {"标题": f"财经早餐 {day}", "摘要": "汇总", "发布时间": f"2026-07-{day:02d} 06:00:00",
             "链接": "http://c"}
            for day in range(1, 21)
        ]

    def news_cctv(self, date):
        self._guard("cctv")
        return [{"title": "政策支持制造业", "content": "内容", "date": date}]

    def stock_news_em(self, symbol):
        self._guard(f"stock:{symbol}")
        return [{"新闻标题": f"{symbol} 中标", "新闻内容": "正文", "发布时间": "2026-08-01",
                 "文章来源": "东财", "新闻链接": "http://y"}]

    def stock_notice_report(self, symbol, date):
        self._guard("notice")
        return [{"代码": "000001", "名称": "平安银行", "公告标题": "年报", "公告类型": "其他",
                 "公告日期": date, "网址": "http://z"}]

    def stock_lhb_detail_em(self, start_date, end_date):
        self._guard("lhb")
        return [{"代码": "000001", "名称": "平安银行", "龙虎榜净买额": 1.2e8, "涨跌幅": 5.0,
                 "上榜日": end_date}]


def test_news_source_collects_every_reachable_feed() -> None:
    source = AkshareNewsSource(ak_module=FakeAk())
    docs, notes = source.fetch_market(date(2026, 8, 1))

    assert {doc.source for doc in docs} == {
        "财联社", "东方财富", "同花顺", "新浪财经", "财经早餐", "新闻联播",
    }
    assert notes == []


def test_sina_flash_survives_having_no_title_column() -> None:
    """新浪那一路只有「时间 + 内容」。

    `fetch_market` 末尾按 `doc.title` 过滤，所以标题留空不是「少了摘要」，
    是**整路源静默消失**——一条都不剩，而且不会有任何报错。
    """
    source = AkshareNewsSource(ak_module=FakeAk())
    docs, _ = source.fetch_market(date(2026, 8, 1))

    sina = [doc for doc in docs if doc.source == "新浪财经"]
    assert len(sina) == 1
    assert "PMI" in sina[0].title


def test_daily_digest_cannot_crowd_out_the_realtime_feeds() -> None:
    """财经早餐返回的是跨月归档（实测 400 条），每天只有一份有意义。

    不限量的话这一路会吃掉 `limit` 预算的大头，把时效最好的几路挤出去——
    而挤掉的正是超短线最需要的那批。
    """
    source = AkshareNewsSource(ak_module=FakeAk())
    docs, _ = source.fetch_market(date(2026, 8, 1))

    digests = [doc for doc in docs if doc.source == "财经早餐"]
    assert len(digests) == CJZC_LIMIT < 20


def test_news_source_degrades_one_feed_at_a_time() -> None:
    source = AkshareNewsSource(ak_module=FakeAk(broken={"em"}))
    docs, notes = source.fetch_market(date(2026, 8, 1))

    # 断言「死掉的那路不见了、其余全在」，而不是断言一个总条数：后者会在每次
    # 新增一路源时失败一次，而失败的原因和这个测试要守的性质毫无关系。
    sources = {doc.source for doc in docs}
    assert "东方财富" not in sources
    assert {"财联社", "同花顺", "新浪财经", "财经早餐", "新闻联播"} <= sources
    assert any("东财全球快讯" in note for note in notes)


def test_news_source_notes_when_everything_is_down() -> None:
    source = AkshareNewsSource(
        ak_module=FakeAk(broken={"cls", "em", "ths", "sina", "cjzc", "cctv"})
    )
    docs, notes = source.fetch_market(date(2026, 8, 1))

    assert docs == []
    assert any("均不可用" in note for note in notes)


def test_news_source_reports_failed_symbols_without_losing_the_rest() -> None:
    source = AkshareNewsSource(ak_module=FakeAk(broken={"stock:000002"}))
    docs, notes = source.fetch_for_symbols(["000001.SZ", "000002.SZ"])

    assert list(docs) == ["000001.SZ"]
    assert any("000002.SZ" in note for note in notes)


# ---- announcements -------------------------------------------------------
#
# 公告是**风险否决**层。盘前 08:30 跑的时候当日公告还没披露，周末与节假日更是
# 一条都没有，而 akshare 在空结果上抛 `KeyError: '代码'`（2026-08-02 实测）。
# 把这个异常当成「今天没有风险公告」，等于让否决层在每个周一盘前静默消失。


class TradingDayAk(FakeAk):
    """只有指定日期有公告，其余日期照 akshare 的真实行为抛 KeyError。"""

    def __init__(self, available: set[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.available = available
        self.asked: list[str] = []

    def stock_notice_report(self, symbol, date):
        self.asked.append(date)
        if date not in self.available:
            raise KeyError("代码")
        return [{"代码": "000001", "名称": "平安银行", "公告标题": "年报", "公告类型": "其他",
                 "公告日期": date, "网址": "http://z"}]


def test_announcements_use_the_requested_day_when_it_has_data() -> None:
    ak = TradingDayAk(available={"20260731"})
    rows, notes = AkshareNewsSource(ak_module=ak).fetch_announcements(date(2026, 7, 31))

    assert len(rows) == 1
    assert ak.asked == ["20260731"]
    assert notes == []


def test_announcements_walk_back_to_the_last_day_that_has_any() -> None:
    """周日盘前问「今天」，要拿到的是上周五的公告，不是空手而归。"""
    ak = TradingDayAk(available={"20260731"})
    rows, notes = AkshareNewsSource(ak_module=ak).fetch_announcements(date(2026, 8, 2))

    assert len(rows) == 1
    assert ak.asked == ["20260802", "20260801", "20260731"]
    assert any("2026-07-31" in note for note in notes)


def test_announcements_say_so_loudly_when_the_screen_is_absent() -> None:
    """取不到就必须说「没有经过否决层」，不能静默返回空表。"""
    ak = TradingDayAk(available=set())
    rows, notes = AkshareNewsSource(ak_module=ak).fetch_announcements(date(2026, 8, 2))

    assert rows == []
    assert any("没有公告风险否决" in note for note in notes)


# ---- cache ---------------------------------------------------------------


def test_cache_round_trips_and_expires(tmp_path) -> None:
    cache = NewsCache(tmp_path, ttl_seconds=3600)
    assert cache.write(date(2026, 8, 1), "cls", [{"a": 1}])
    assert cache.read(date(2026, 8, 1), "cls") == [{"a": 1}]

    expired = NewsCache(tmp_path, ttl_seconds=0)
    assert expired.read(date(2026, 8, 1), "cls") is None


def test_cache_survives_non_json_native_values(tmp_path) -> None:
    """news_cctv hands back date objects; the cache must not raise on them."""
    cache = NewsCache(tmp_path)
    assert cache.write(date(2026, 8, 1), "cctv", [{"date": date(2026, 8, 1)}])
    assert cache.read(date(2026, 8, 1), "cctv") == [{"date": "2026-08-01"}]


def test_cache_read_tolerates_corrupt_files(tmp_path) -> None:
    cache = NewsCache(tmp_path)
    path = cache.path_for(date(2026, 8, 1), "cls")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert cache.read(date(2026, 8, 1), "cls") is None
