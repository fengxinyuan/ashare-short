"""采集与计算 agent。每个包一个现有模块，给它统一的失败语义与并行能力。

这里没有新算法——`news/`、`sectors/`、`signals/` 里已经有了。这一层做的是让它们
能并行跑、能单独失败、输出同一种结构，这样盘前主链路里舆情采集和成分股日线可以
完全重叠，而不是一个等另一个。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ashare_decision_support.adapters.base import fetch_daily_bars
from ashare_decision_support.adapters.snapshot import default_snapshot_adapter
from ashare_decision_support.agents.base import AgentContext
from ashare_decision_support.news.balance import source_counts
from ashare_decision_support.news.base import NewsDoc, SentimentAnalyzer, SymbolSentiment
from ashare_decision_support.news.dedup import DEFAULT_HALF_LIFE_HOURS, freshest, weigh_docs
from ashare_decision_support.news.discovery import matching_docs
from ashare_decision_support.news.events import EventEvidence, detect_events
from ashare_decision_support.news.relevance import filter_company_news
from ashare_decision_support.news.sentiment import LexiconSentiment
from ashare_decision_support.schedule.timezone import beijing_naive_now
from ashare_decision_support.sectors import SectorMomentum
from ashare_decision_support.signals import DailyTechnicalAnalyzer, WyckoffAnalyzer
from ashare_decision_support.signals.intraday import IntradayAnalyzer
from ashare_decision_support.signals.orderflow import read_many
from ashare_decision_support.signals.relative_strength import benchmark_for
from ashare_decision_support.symbols import bare_code

METRIC_LOOKBACK_DAYS = 60
DAILY_LOOKBACK_DAYS = 240
DRAGON_TIGER_LOOKBACK_DAYS = 5

# Per-stock news documents kept per symbol. Ten headlines is roughly two weeks
# for a quiet name and one session for a busy one; beyond that the tail is
# almost entirely routine filings the lexicon drops anyway.
SYMBOL_NEWS_LIMIT = 10
SECTOR_SUPPLEMENT_LIMIT = 1600


class NewsAgent:
    """舆情、公告、龙虎榜。每个源独立守卫，挂一个不影响其余。

    采到之后先过 `news/dedup.py`：跨源重复的合并、超龄的丢弃、其余按半衰期打权重。
    下游拿到的 `docs` 因此已经是「还算数」的那批，而不是原始堆。
    """

    name = "news"

    def __init__(
        self,
        now: datetime | None = None,
        half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
        store: Any = None,
        market_limit: int | None = None,
    ) -> None:
        self.now = now
        self.half_life_hours = half_life_hours
        # 语料库。默认关着：`morning` 等长期跑的链路显式传一个进来，测试与
        # 一次性脚本保持原样，不会因为多了这一层而需要准备 SQLite 文件。
        self.store = store
        # 每路源取多少条市场快讯。None 表示用源自己的默认值（200），保持既有行为。
        # `morning` 传一个更大的值，理由见那边的 `WIDE_MARKET_LIMIT`。
        self.market_limit = market_limit

    def run(self, ctx: AgentContext) -> tuple[dict, list[str]]:
        source = ctx.news_source
        if source is None:
            return {"docs": [], "announcements": [], "net_inflow": {}, "as_of": None}, [
                "舆情源不可用，本次板块排序仅依据量价动量"
            ]

        notes: list[str] = []
        fetch_market = (
            (lambda: source.fetch_market(ctx.trade_date, limit=self.market_limit))
            if self.market_limit
            else (lambda: source.fetch_market(ctx.trade_date))
        )
        docs, doc_notes = _guard(fetch_market, ([], []), notes, "快讯")
        notes.extend(doc_notes)

        announcements, notice_notes = _guard(
            lambda: source.fetch_announcements(ctx.trade_date), ([], []), notes, "公告"
        )
        notes.extend(notice_notes)

        flows, flow_notes = _guard(
            lambda: source.fetch_dragon_tiger(
                ctx.trade_date - timedelta(days=DRAGON_TIGER_LOOKBACK_DAYS), ctx.trade_date
            ),
            ([], []),
            notes,
            "龙虎榜",
        )
        notes.extend(flow_notes)

        net_inflow: dict[str, float] = {}
        for row in flows:
            value = row.get("net_buy")
            if isinstance(value, (int, float)):
                net_inflow[row["symbol"]] = net_inflow.get(row["symbol"], 0.0) + float(value)

        as_of = freshest(docs)
        weighted, weigh_notes = weigh_docs(
            docs, self.now or beijing_naive_now(), self.half_life_hours
        )
        notes.extend(weigh_notes)
        stats = getattr(source, "last_market_stats", {})
        raw_by_source = stats.get("raw_by_source", {}) if isinstance(stats, dict) else {}
        retained_by_source = (
            stats.get("retained_by_source", {}) if isinstance(stats, dict) else {}
        )
        if raw_by_source:
            notes.append(
                "市场舆情来源覆盖："
                + "、".join(
                    f"{name} {retained_by_source.get(name, 0)}/{count}"
                    for name, count in sorted(raw_by_source.items())
                )
                + "（保留/采到）"
            )

        # 入库在**去重之后**：批内重复的合并掉再入，库里的 seen_count 才是
        # 「这条被采集到过几轮」，而不是「这一轮有几个源转了它」——后者由
        # doc_sources 单独数。入库失败只记 note，不影响本轮任何读数。
        notes.extend(self._archive(weighted, ctx))

        return (
            {
                "docs": weighted,
                "raw_doc_count": len(docs),
                "market_source_counts": {
                    "raw": dict(raw_by_source),
                    "retained": dict(retained_by_source),
                    "weighted": source_counts(weighted),
                },
                "announcements": announcements,
                "net_inflow": net_inflow,
                "as_of": as_of.isoformat(timespec="minutes") if as_of else None,
            },
            notes,
        )

    def supplement_market(
        self,
        ctx: AgentContext,
        targets: list[Any],
        existing_docs: list[NewsDoc] | None = None,
    ) -> tuple[dict, list[str]]:
        """Fetch a wider pool and keep documents matching strong sectors."""
        source = ctx.news_source
        if source is None or not targets:
            return {"docs": [], "raw_doc_count": 0}, []

        try:
            docs, fetch_notes = source.fetch_market(
                ctx.trade_date, limit=SECTOR_SUPPLEMENT_LIMIT
            )
        except Exception as exc:
            return {}, [f"板块定向舆情补抓失败（{type(exc).__name__}），保留首轮结果"]

        selected = matching_docs(docs, targets)
        existing_keys = {_doc_key(doc) for doc in (existing_docs or [])}
        selected = [doc for doc in selected if _doc_key(doc) not in existing_keys]
        weighted, weigh_notes = weigh_docs(
            selected, self.now or beijing_naive_now(), self.half_life_hours
        )
        notes = list(fetch_notes) + list(weigh_notes)
        notes.append(
            f"板块定向舆情补抓：扫描 {len(docs)} 条，命中 {len(selected)} 条新行业消息，"
            f"保留 {len(weighted)} 条"
        )
        notes.extend(self._archive(weighted, ctx))
        return {
            "docs": weighted,
            "raw_doc_count": len(selected),
            "targets": [target.to_dict() for target in targets],
        }, notes

    def _archive(self, docs: list[NewsDoc], ctx: AgentContext) -> list[str]:
        """写进持久语料库，并报告这批里有多少是真的新出现的。

        `store=None` 时整段跳过——测试与一次性脚本不该因为多了一个语料库而
        需要准备一个 SQLite 文件。
        """
        if self.store is None or not docs:
            return []
        stamp = self.now or beijing_naive_now()
        result = self.store.ingest(docs, ctx.trade_date, now=stamp)
        _, fresh_notes = self.store.annotate_freshness(docs, now=stamp)
        return result.notes + fresh_notes


class SymbolNewsAgent:
    """个股舆情：个股新闻 + 当日公告，逐票聚合成一个有界的情绪读数。

    这一层存在的理由很直接：在它之前，舆情只影响板块排序，**完全不影响选出哪只票**。
    一只票带着减持公告、和同板块另一只不带，两者在打分里一模一样。

    公告和新闻合在一起打分，因为公告标题带交易所指定的类型，是最硬的个股舆情——
    但只并入**属于这只票**的公告，全市场那 1500 多条与它无关。
    """

    name = "symbol_news"

    def __init__(
        self,
        symbols: list[str] | None = None,
        announcements: list[dict] | None = None,
        analyzer: SentimentAnalyzer | None = None,
        limit: int = SYMBOL_NEWS_LIMIT,
        now: datetime | None = None,
        half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
        names: dict[str, str | None] | None = None,
    ) -> None:
        self.symbols = symbols
        # `None` means this caller has not collected announcements yet (the
        # on-demand advise path).  An explicit empty list means the caller did
        # collect them and found none, so do not issue the same request again.
        self.announcements = announcements
        self.analyzer = analyzer or LexiconSentiment()
        self.limit = limit
        self.now = now
        self.half_life_hours = half_life_hours
        # 证券简称，用于把「顺带提到这只票」的综述与榜单挡在个股舆情之外。
        # 给 None 就整段跳过过滤，行为与加这层之前完全一致——见 `news/relevance.py`。
        self.names = names

    def run(self, ctx: AgentContext) -> tuple[dict, list[str]]:
        symbols = list(self.symbols if self.symbols is not None else ctx.symbols)
        source = ctx.news_source
        if not symbols:
            return {"sentiment": {}, "events": {}}, []
        if source is None or not hasattr(source, "fetch_for_symbols"):
            return {"sentiment": {}, "events": {}}, ["个股舆情源不可用，本次打分不含个股舆情项"]

        notes: list[str] = []
        docs_by_symbol, fetch_notes = _guard(
            lambda: source.fetch_for_symbols(symbols, limit=self.limit), ({}, []), notes, "个股新闻"
        )
        notes.extend(fetch_notes)

        announcements = self.announcements
        if announcements is None:
            fetch_announcements = getattr(source, "fetch_announcements", None)
            if callable(fetch_announcements):
                announcements, announcement_notes = _guard(
                    lambda: fetch_announcements(ctx.trade_date), ([], []), notes, "个股公告"
                )
                notes.extend(announcement_notes)
            else:
                announcements = []
        notices = _announcement_docs(announcements or [], symbols)
        moment = self.now or beijing_naive_now()

        sentiment: dict[str, SymbolSentiment] = {}
        events: dict[str, EventEvidence] = {}
        scored = 0
        roundups = 0
        for symbol in symbols:
            feed = list(docs_by_symbol.get(symbol, []))
            if self.names is not None:
                # 行业综述与资金流榜单只在正文里列到这只票，标题从不点名。词典对
                # 整段文本打分，于是别家公司的利好利空被记到了这只票头上——实测
                # 大智慧因为「湘财证券、国盛证券被证监会立案」被判偏空并扣了分。
                # 公告不过这道闸门：它本来就是按代码挂上来的。
                feed, mentions = filter_company_news(feed, symbol, self.names.get(symbol))
                roundups += len(mentions)
            docs = feed + notices.get(bare_code(symbol), [])
            if not docs:
                continue
            weighted, _ = weigh_docs(docs, moment, self.half_life_hours)
            if not weighted:
                continue
            as_of = freshest(weighted)
            events[symbol] = detect_events(doc.text for doc in weighted)
            reading = self.analyzer.score_symbol(
                symbol, weighted, as_of.isoformat(timespec="minutes") if as_of else None
            )
            sentiment[symbol] = reading
            if not reading.is_neutral:
                scored += 1

        notes.append(
            f"个股舆情：{len(sentiment)}/{len(symbols)} 只有文档，其中 {scored} 只命中情绪词"
        )
        if roundups:
            notes.append(
                f"个股舆情剔除 {roundups} 条只在正文提及本股的行业综述/资金流榜单"
                "（标题不点名的不算这只票自己的消息）"
            )
        return {"sentiment": sentiment, "events": events}, notes


class SectorMetricsAgent:
    """申万分类 + 成分股日线聚合成板块动量。链路里最慢的一环，所以要和舆情并行。"""

    name = "sector_metrics"

    def __init__(self, definitions: list, symbols: list[str]) -> None:
        self.definitions = definitions
        self.symbols = symbols

    def run(self, ctx: AgentContext) -> tuple[dict, list[str]]:
        start = ctx.trade_date - timedelta(days=METRIC_LOOKBACK_DAYS)
        bars = fetch_daily_bars(ctx.adapter, self.symbols, start, ctx.trade_date)
        metrics = SectorMomentum().measure(self.definitions, bars)
        covered = sum(1 for rows in bars.values() if rows)
        return (
            {"metrics": metrics, "bars": bars},
            [f"板块动量样本：{covered}/{len(self.symbols)} 只成分股有可用日线"],
        )


class SymbolDailyAgent:
    """个股日线结构：趋势、支撑压力、威科夫阶段。"""

    name = "symbol_daily"

    def run(self, ctx: AgentContext) -> tuple[dict, list[str]]:
        if not ctx.symbols:
            return {"snapshots": {}, "wyckoff": {}, "bars": {}, "prev_closes": {}}, []

        start = ctx.trade_date - timedelta(days=DAILY_LOOKBACK_DAYS)
        bars = fetch_daily_bars(ctx.adapter, ctx.symbols, start, ctx.trade_date)
        usable = {symbol: rows for symbol, rows in bars.items() if rows}
        missing = [symbol for symbol in ctx.symbols if symbol not in usable]
        notes = [f"无日线数据的标的：{'、'.join(missing[:8])}"] if missing else []

        return (
            {
                "snapshots": DailyTechnicalAnalyzer().analyze_many(usable),
                "wyckoff": {s.symbol: s for s in WyckoffAnalyzer().analyze_many(usable)},
                "bars": usable,
                "prev_closes": {
                    symbol: rows[-2]["close"]
                    for symbol, rows in usable.items()
                    if len(rows) >= 2 and rows[-2].get("close")
                },
            },
            notes,
        )


class BenchmarkDailyAgent:
    """Fetch the daily benchmark series needed for relative-strength facts.

    This deliberately collects only the distinct benchmarks represented by the
    requested symbols.  A batch of twenty Shenzhen names should not turn into
    twenty identical index requests.
    """

    name = "benchmark_daily"

    def __init__(self, symbols: list[str]) -> None:
        self.benchmarks = list(
            dict.fromkeys(
                resolved[0] for symbol in symbols if (resolved := benchmark_for(symbol)) is not None
            )
        )

    def run(self, ctx: AgentContext) -> tuple[dict, list[str]]:
        if not self.benchmarks:
            return {"bars": {}}, []

        start = ctx.trade_date - timedelta(days=DAILY_LOOKBACK_DAYS)
        bars = fetch_daily_bars(ctx.adapter, self.benchmarks, start, ctx.trade_date)
        usable = {symbol: rows for symbol, rows in bars.items() if rows}
        missing = [symbol for symbol in self.benchmarks if symbol not in usable]
        notes = [f"相对强度缺少宽基日线：{'、'.join(missing)}"] if missing else []
        return {"bars": usable}, notes


class SymbolIntradayAgent:
    """分时结构。价格为不复权实际成交价——所有挂单价位的唯一来源。"""

    name = "symbol_intraday"

    def __init__(self, prev_closes: dict[str, float] | None = None) -> None:
        self.prev_closes = prev_closes or {}

    def run(self, ctx: AgentContext) -> tuple[dict, list[str]]:
        source = ctx.intraday_adapter
        if source is None or not ctx.symbols:
            return {"snapshots": {}, "bars": {}}, (
                ["分钟线源不可用，价位仅为前复权口径、不可直接挂单"] if ctx.symbols else []
            )

        bars, notes = source.get_minute_bars(ctx.symbols, period=ctx.period)
        notes = [*list(notes), f"分钟线粒度 {ctx.period} 分钟，价格为**不复权**实际成交价"]
        snapshots = IntradayAnalyzer(today=ctx.trade_date).analyze_many(bars, self.prev_closes)
        return {"snapshots": snapshots, "bars": bars}, notes


class OrderBookAgent:
    """五档盘口。一次批量请求拿全所有标的，不是每标的一次。

    这是它值得和分钟线并行的原因：分钟线是每标的一次 HTTP（实测 8 线程 0.19
    秒/只），盘口是**整批一次**（实测单只 161 毫秒，几百只仍在一秒内）。两者
    延迟量级完全不同，串起来跑等于让快的那个白等。

    默认走腾讯：它同一份响应里既有五档也有真实涨跌停价。走新浪要打两次，而
    涨跌停价是这里最有价值的字段之一——「涨了 9.5% 就算涨停」这个近似分不出
    10% 板和 20% 创业板/科创板，而封板与否直接决定这只票现在买不买得进。

    取不到就整体缺失，不降级成猜测：`snapshot.py` 在拿不到涨跌停价时
    `at_limit_up` 返回 None 而不是 False。
    """

    name = "order_book"

    def __init__(self, adapter: Any = None, symbols: list[str] | None = None) -> None:
        self.adapter = adapter
        self.symbols = symbols

    def run(self, ctx: AgentContext) -> tuple[dict, list[str]]:
        symbols = self.symbols if self.symbols is not None else ctx.symbols
        if not symbols:
            return {"readings": {}, "snapshots": {}}, []

        adapter = self.adapter or ctx.extras.get("snapshot_adapter") or default_snapshot_adapter()
        if adapter is None:
            return {"readings": {}, "snapshots": {}}, ["五档快照源不可用，本轮候选集不含挂单墙价位"]

        snapshots, notes = adapter.get_snapshots(list(symbols))
        readings = read_many(snapshots)
        notes = list(notes)
        if not readings:
            notes.append("五档全部为空，本轮候选集不含挂单墙价位")
        else:
            halted = [code for code, item in readings.items() if not item.is_tradable_now]
            if halted:
                notes.append(f"封板/单边不可成交：{'、'.join(halted[:8])}")
        return {"readings": readings, "snapshots": snapshots}, notes



def _announcement_docs(announcements: list[dict], symbols: list[str]) -> dict[str, list[NewsDoc]]:
    """当日公告按标的归并。公告类型并进标题——它是交易所给的标签，比正文可靠。"""
    wanted = {bare_code(symbol) for symbol in symbols}
    if not wanted:
        return {}
    result: dict[str, list[NewsDoc]] = {}
    for row in announcements:
        code = bare_code(str(row.get("symbol") or ""))
        if code not in wanted:
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        category = str(row.get("category") or "").strip()
        result.setdefault(code, []).append(
            NewsDoc(
                title=f"{category}｜{title}" if category else title,
                source="公告",
                published_at=str(row.get("published_at") or ""),
                url=str(row.get("url") or ""),
            )
        )
    return result


def _guard(call, fallback, notes: list[str], label: str):
    """单个源失败只记一条 note。整轮不因为一个源挂掉而失败。"""
    try:
        return call()
    except Exception as exc:
        notes.append(f"{label}源不可用（{type(exc).__name__}），本次缺该项输入")
        return fallback



def _doc_key(doc: NewsDoc) -> str:
    from ashare_decision_support.news.dedup import dedup_key

    return dedup_key(doc)
