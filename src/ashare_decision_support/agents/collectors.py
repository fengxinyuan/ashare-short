"""采集与计算 agent。每个包一个现有模块，给它统一的失败语义与并行能力。

这里没有新算法——`news/`、`sectors/`、`signals/` 里已经有了。这一层做的是让它们
能并行跑、能单独失败、输出同一种结构，这样盘前主链路里舆情采集和成分股日线可以
完全重叠，而不是一个等另一个。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ashare_decision_support.agents.base import AgentContext
from ashare_decision_support.news.base import NewsDoc, SentimentAnalyzer, SymbolSentiment
from ashare_decision_support.news.dedup import DEFAULT_HALF_LIFE_HOURS, freshest, weigh_docs
from ashare_decision_support.news.sentiment import LexiconSentiment
from ashare_decision_support.sectors import SectorMomentum
from ashare_decision_support.signals import DailyTechnicalAnalyzer, WyckoffAnalyzer
from ashare_decision_support.signals.intraday import IntradayAnalyzer
from ashare_decision_support.symbols import bare_code

METRIC_LOOKBACK_DAYS = 60
DAILY_LOOKBACK_DAYS = 240
DRAGON_TIGER_LOOKBACK_DAYS = 5

# Per-stock news documents kept per symbol. Ten headlines is roughly two weeks
# for a quiet name and one session for a busy one; beyond that the tail is
# almost entirely routine filings the lexicon drops anyway.
SYMBOL_NEWS_LIMIT = 10


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
    ) -> None:
        self.now = now
        self.half_life_hours = half_life_hours
        # 语料库。默认关着：`morning` 等长期跑的链路显式传一个进来，测试与
        # 一次性脚本保持原样，不会因为多了这一层而需要准备 SQLite 文件。
        self.store = store

    def run(self, ctx: AgentContext) -> tuple[dict, list[str]]:
        source = ctx.news_source
        if source is None:
            return {"docs": [], "announcements": [], "net_inflow": {}, "as_of": None}, [
                "舆情源不可用，本次板块排序仅依据量价动量"
            ]

        notes: list[str] = []
        docs, doc_notes = _guard(lambda: source.fetch_market(ctx.trade_date), ([], []), notes, "快讯")
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
            docs, self.now or datetime.now(), self.half_life_hours
        )
        notes.extend(weigh_notes)

        # 入库在**去重之后**：批内重复的合并掉再入，库里的 seen_count 才是
        # 「这条被采集到过几轮」，而不是「这一轮有几个源转了它」——后者由
        # doc_sources 单独数。入库失败只记 note，不影响本轮任何读数。
        notes.extend(self._archive(weighted, ctx))

        return (
            {
                "docs": weighted,
                "raw_doc_count": len(docs),
                "announcements": announcements,
                "net_inflow": net_inflow,
                "as_of": as_of.isoformat(timespec="minutes") if as_of else None,
            },
            notes,
        )

    def _archive(self, docs: list[NewsDoc], ctx: AgentContext) -> list[str]:
        """写进持久语料库，并报告这批里有多少是真的新出现的。

        `store=None` 时整段跳过——测试与一次性脚本不该因为多了一个语料库而
        需要准备一个 SQLite 文件。
        """
        if self.store is None or not docs:
            return []
        stamp = self.now or datetime.now()
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
    ) -> None:
        self.symbols = symbols
        self.announcements = announcements or []
        self.analyzer = analyzer or LexiconSentiment()
        self.limit = limit
        self.now = now
        self.half_life_hours = half_life_hours

    def run(self, ctx: AgentContext) -> tuple[dict, list[str]]:
        symbols = list(self.symbols if self.symbols is not None else ctx.symbols)
        source = ctx.news_source
        if not symbols:
            return {"sentiment": {}}, []
        if source is None or not hasattr(source, "fetch_for_symbols"):
            return {"sentiment": {}}, ["个股舆情源不可用，本次打分不含个股舆情项"]

        notes: list[str] = []
        docs_by_symbol, fetch_notes = _guard(
            lambda: source.fetch_for_symbols(symbols, limit=self.limit), ({}, []), notes, "个股新闻"
        )
        notes.extend(fetch_notes)

        notices = _announcement_docs(self.announcements, symbols)
        moment = self.now or datetime.now()

        sentiment: dict[str, SymbolSentiment] = {}
        scored = 0
        for symbol in symbols:
            docs = list(docs_by_symbol.get(symbol, [])) + notices.get(bare_code(symbol), [])
            if not docs:
                continue
            weighted, _ = weigh_docs(docs, moment, self.half_life_hours)
            if not weighted:
                continue
            as_of = freshest(weighted)
            reading = self.analyzer.score_symbol(
                symbol, weighted, as_of.isoformat(timespec="minutes") if as_of else None
            )
            sentiment[symbol] = reading
            if not reading.is_neutral:
                scored += 1

        notes.append(
            f"个股舆情：{len(sentiment)}/{len(symbols)} 只有文档，其中 {scored} 只命中情绪词"
        )
        return {"sentiment": sentiment}, notes


class SectorMetricsAgent:
    """申万分类 + 成分股日线聚合成板块动量。链路里最慢的一环，所以要和舆情并行。"""

    name = "sector_metrics"

    def __init__(self, definitions: list, symbols: list[str]) -> None:
        self.definitions = definitions
        self.symbols = symbols

    def run(self, ctx: AgentContext) -> tuple[dict, list[str]]:
        start = ctx.trade_date - timedelta(days=METRIC_LOOKBACK_DAYS)
        bars = _fetch_daily(ctx.adapter, self.symbols, start, ctx.trade_date)
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
        bars = _fetch_daily(ctx.adapter, ctx.symbols, start, ctx.trade_date)
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
        notes = list(notes) + [f"分钟线粒度 {ctx.period} 分钟，价格为**不复权**实际成交价"]
        snapshots = IntradayAnalyzer(today=ctx.trade_date).analyze_many(bars, self.prev_closes)
        return {"snapshots": snapshots, "bars": bars}, notes


# ---- helpers -------------------------------------------------------------


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


def _fetch_daily(adapter: Any, symbols: list[str], start, end) -> dict[str, list[dict]]:
    batch = getattr(adapter, "get_historical_bars_batch", None)
    if callable(batch):
        return batch(symbols, start, end)
    return adapter.get_historical_bars(symbols, start, end)
