"""AkShare-backed news collection.

Every feed here is free and needs no key. They were picked by what actually
answered on this host — the東方财富 `push2` bulk endpoints (spot quotes, board
lists) are unreachable here, so nothing in the sentiment path depends on them.

Each fetch is independently guarded: one dead feed costs you its documents and
a note in the brief, never the whole run.

**为什么是六路而不是三路。** 覆盖面互补，不是冗余：实测同一时刻，财联社与东财
偏 A 股盘面，同花顺偏产业与公司事件，新浪全球快讯偏海外宏观，而**财经早餐是唯一
一份结构化的隔夜汇总**——06:00 发布，正好喂 08:30 的盘前跑批，一条里就带齐
「央行货币政策 + 个股停牌核查 + 海外谈判」。缺了它，隔夜发生的事要靠从几百条
零散快讯里拼，而拼不全的部分不会报错，只会让盘前少一块背景。

**刻意没接富途快讯。** 它可达（实测 50 条 / 0.46s），但标题字段为空且内容与
华尔街见闻同属海外宏观赛道。再加一路只会让海外消息在板块归因里被重复计权——
去重层按标题归并，措辞不同的同一件事它合不掉。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

from ashare_decision_support.fanout import gather
from ashare_decision_support.news.balance import retain_market_docs
from ashare_decision_support.news.base import NewsDoc, NewsSource
from ashare_decision_support.news.cache import NewsCache
from ashare_decision_support.news.health import probe_feeds
from ashare_decision_support.numeric import first_finite
from ashare_decision_support.tabular import records, text

# Per-stock news is one call per symbol. Matching `sina_intraday.py`'s pool size
# keeps the concurrent footprint against free endpoints predictable.
DEFAULT_SYMBOL_WORKERS = 8

# 公告回溯天数。要盖住「周一盘前回看上周五」这段最长的自然日间隔（含三天小长假
# 时是 4 天），再多留一天余量。回溯得再远就不是「昨晚的公告」了，风险否决用陈年
# 公告没有意义。
ANNOUNCEMENT_LOOKBACK_DAYS = 5

# 新闻联播回溯天数。盘前跑时今晚的还没播，取昨天的即可；留 3 天盖住节假日停播。
CCTV_LOOKBACK_DAYS = 3

# 财经早餐单源取多少条。它返回的是**跨月历史归档**（实测 400 条），而每天只有
# 一份。不限量的话这一路会占掉 `fetch_market` 整个 limit 预算的三分之二，把
# 真正实时的几路快讯挤出去——而被挤掉的恰恰是时效最好的那批。
#
# 时效折减在 `news/dedup.py` 里做（超 72 小时整条丢弃），所以这里只要留够
# 「一周份」就行，多出来的会在下游被丢掉，白占预算。
CJZC_LIMIT = 8


class AkshareNewsSource(NewsSource):
    """Collects market-wide and per-stock documents from AkShare."""

    name = "akshare"

    def __init__(self, ak_module: Any | None = None, cache: NewsCache | None = None) -> None:
        if ak_module is None:
            try:
                import akshare as ak_module  # type: ignore
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError(
                    "akshare is not installed. Install it with `pip install -e .[akshare]`."
                ) from exc
        self.ak = ak_module
        self.cache = cache
        self.last_market_stats: dict[str, Any] = {}

    # ---- market-wide -----------------------------------------------------

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        docs: list[NewsDoc] = []
        notes: list[str] = []
        raw_by_source: dict[str, int] = {}

        feeds = (
            (
                "cls_telegraph",
                "财联社电报",
                lambda: self.ak.stock_info_global_cls(symbol="全部"),
                lambda row: NewsDoc(
                    title=text(row, "标题"),
                    summary=text(row, "内容"),
                    source="财联社",
                    published_at=f"{text(row, '发布日期')} {text(row, '发布时间')}".strip(),
                ),
                None,
            ),
            (
                "em_global",
                "东财全球快讯",
                lambda: self.ak.stock_info_global_em(),
                lambda row: NewsDoc(
                    title=text(row, "标题"),
                    summary=text(row, "摘要"),
                    source="东方财富",
                    published_at=text(row, "发布时间"),
                    url=text(row, "链接"),
                ),
                None,
            ),
            (
                "ths_global",
                "同花顺快讯",
                lambda: self.ak.stock_info_global_ths(),
                lambda row: NewsDoc(
                    title=text(row, "标题"),
                    summary=text(row, "内容"),
                    source="同花顺",
                    published_at=text(row, "发布时间"),
                    url=text(row, "链接"),
                ),
                None,
            ),
            (
                "sina_global",
                "新浪全球快讯",
                lambda: self.ak.stock_info_global_sina(),
                # 这一路**没有标题字段**，只有「时间 + 内容」，而内容本身就是一条
                # 完整的快讯。所以拿它当标题：`fetch_market` 末尾按 `doc.title`
                # 过滤，留空会让整路源静默消失——不是少几条，是一条都不剩。
                lambda row: NewsDoc(
                    title=text(row, "内容"),
                    source="新浪财经",
                    published_at=text(row, "时间"),
                ),
                None,
            ),
            (
                "cjzc",
                "东财财经早餐",
                lambda: self.ak.stock_info_cjzc_em(),
                lambda row: NewsDoc(
                    title=text(row, "标题"),
                    summary=text(row, "摘要"),
                    source="财经早餐",
                    published_at=text(row, "发布时间"),
                    url=text(row, "链接"),
                ),
                CJZC_LIMIT,
            ),
            (
                "cctv_news",
                "新闻联播",
                # 盘前 08:30 跑时今晚的联播还没播，稿子也还没上网——和公告完全
                # 同一类问题。硬取「今天」的结果是**政策主线源每天早上静默缺席**，
                # 而它是这条链路里唯一的政策输入。回退到最近一期有稿的日子。
                lambda: self._latest_cctv(trade_date),
                lambda row: NewsDoc(
                    title=text(row, "title"),
                    summary=text(row, "content")[:500],
                    source="新闻联播",
                    published_at=text(row, "date"),
                ),
                None,
            ),
        )

        # 六路快讯是六个独立的 HTTP 往返，此前串行——盘前链路上这一段就是六次
        # 延迟相加，而其中「新闻联播」那一路自己还要往前找最近一期有稿的日子。
        # 并发之后由最慢的一路决定，缓存命中的几路几乎不占时间。
        fetched, _ = gather(
            feeds,
            lambda feed: self._cached_rows(trade_date, feed[0], feed[1], feed[2]),
        )
        # 保序遍历：`balance_by_source` 在同一来源内取列表前缀，完成顺序若能影响
        # 结果，盘前跑两次就会拿到两批不同的文档。`gather` 保序，这里也按序展开。
        for (_key, label, _loader, mapper, cap), (rows, note) in fetched:
            if note:
                notes.append(note)
            mapped = [mapper(row) for row in (rows[:cap] if cap else rows)]
            mapped = [doc for doc in mapped if doc.title]
            raw_by_source[label] = len(mapped)
            docs.extend(mapped)

        docs, self.last_market_stats = retain_market_docs(docs, limit, raw_by_source)
        if not docs:
            notes.append("所有市场级舆情源均不可用，本次板块打分不含舆情项")
        return docs, notes

    # ---- per-stock -------------------------------------------------------

    def _latest_cctv(self, trade_date: date) -> Any:
        """最近一期有稿的新闻联播。空表和抛异常都算「这天没有」。"""
        last_error: Exception | None = None
        for offset in range(CCTV_LOOKBACK_DAYS + 1):
            day = trade_date - timedelta(days=offset)
            try:
                rows = self.ak.news_cctv(date=day.strftime("%Y%m%d"))
            except Exception as exc:
                last_error = exc
                continue
            if len(rows) if rows is not None else 0:
                return rows
        if last_error is not None:
            raise last_error
        return []

    def fetch_for_symbols(
        self,
        symbols: list[str],
        limit: int = 10,
        max_workers: int = DEFAULT_SYMBOL_WORKERS,
    ) -> tuple[dict[str, list[NewsDoc]], list[str]]:
        """Per-stock news, one HTTP call per symbol, fetched concurrently.

        Serial this is the slowest thing in the pre-open chain — the shortlist
        is dozens of names and each call is a round trip. Same reasoning (and
        same shape) as `adapters/sina_intraday.py`: the calls are independent
        and IO-bound, so a small pool turns N round trips into N/workers.
        """
        if not symbols:
            return {}, []

        def fetch(symbol: str) -> tuple[str, list[NewsDoc] | None]:
            try:
                table = self.ak.stock_news_em(symbol=_bare_code(symbol))
            except Exception:
                return symbol, None
            return symbol, [
                NewsDoc(
                    title=text(row, "新闻标题"),
                    summary=text(row, "新闻内容")[:500],
                    source=text(row, "文章来源"),
                    published_at=text(row, "发布时间"),
                    url=text(row, "新闻链接"),
                    symbols=(symbol,),
                )
                for row in records(table)[:limit]
                if text(row, "新闻标题")
            ]

        workers = max(1, min(int(max_workers), len(symbols)))
        if workers == 1:
            pairs = [fetch(symbol) for symbol in symbols]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pairs = list(pool.map(fetch, symbols))

        result = {symbol: docs for symbol, docs in pairs if docs is not None}
        failed = [symbol for symbol, docs in pairs if docs is None]
        notes = [f"以下标的个股新闻拉取失败：{'、'.join(failed[:8])}"] if failed else []
        return result, notes

    # ---- announcements ---------------------------------------------------

    def fetch_announcements(self, trade_date: date) -> tuple[list[dict], list[str]]:
        """Whole-market announcements, walking back to the last day that has any.

        The category is the useful part: it is an exchange-assigned label, so
        screening on it does not depend on parsing free text.

        Two reasons the requested day is routinely empty, and both matter for a
        **pre-open** run — which is the only time this screen is consulted:

        - 盘前 08:30 时「今天」的公告还没发，风险公告是昨晚披露的；
        - 周末与节假日没有公告，而 akshare 在空结果上直接 ``KeyError: '代码'``，
          不是返回空表。

        所以既不能只查当天，也不能把异常当成「今天没有风险公告」——那会让否决层
        在每个周一盘前静默消失。回退到最近一个有公告的日子，并把实际用的日期写进
        note；这和涨停池适配器对服务端日期回退的处理是同一套约定。
        """
        for offset in range(ANNOUNCEMENT_LOOKBACK_DAYS + 1):
            day = trade_date - timedelta(days=offset)
            rows, note = self._cached_rows(
                day,
                "notices",
                "沪深公告",
                lambda day=day: self.ak.stock_notice_report(
                    symbol="全部", date=day.strftime("%Y%m%d")
                ),
            )
            parsed = [
                {
                    "symbol": text(row, "代码"),
                    "name": text(row, "名称"),
                    "title": text(row, "公告标题"),
                    "category": text(row, "公告类型"),
                    "published_at": text(row, "公告日期"),
                    "url": text(row, "网址"),
                }
                for row in rows
                if text(row, "代码")
            ]
            if parsed:
                notes = [note] if note else []
                if offset:
                    notes.append(
                        f"{trade_date} 无公告数据，公告风险否决改用 {day} 的 {len(parsed)} 条"
                        "（盘前跑时当日公告尚未披露，这是预期行为）"
                    )
                return parsed, notes

        return [], [
            (f"回溯 {ANNOUNCEMENT_LOOKBACK_DAYS} 天内没有取到任何公告，"
            "本次**没有公告风险否决**，候选未经该层过滤")
        ]

    # ---- capital flow ----------------------------------------------------

    def fetch_dragon_tiger(self, start: date, end: date) -> tuple[list[dict], list[str]]:
        """Dragon-tiger board rows, used as the money-confirmation input.

        Sentiment says what people are talking about; this says where money
        actually went. A sector that scores high on the first and zero on the
        second is a story, not a flow.
        """
        rows, note = self._cached_rows(
            end,
            f"lhb_{start:%Y%m%d}_{end:%Y%m%d}",
            "龙虎榜",
            lambda: self.ak.stock_lhb_detail_em(
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
            ),
        )
        parsed: list[dict] = []
        for row in rows:
            code = text(row, "代码")
            if not code:
                continue
            parsed.append(
                {
                    "symbol": code,
                    "name": text(row, "名称"),
                    "net_buy": first_finite(row, "龙虎榜净买额"),
                    "change_pct": first_finite(row, "涨跌幅"),
                    "trade_date": text(row, "上榜日"),
                }
            )
        return parsed, ([note] if note else [])

    def health_check(self) -> dict[str, Any]:
        """逐路探 akshare 快讯接口，不走缓存。

        探的是**接口本身**，不是当天有没有内容——`stock_info_global_*` 这几路是
        滚动快讯，任何时候都该有东西；返回空表基本等于上游改了字段而不是没新闻。
        新闻联播那一路不在这里探，它按日期取、盘前当天本来就该是空的。
        """
        probes = (
            ("财联社电报", lambda: self.ak.stock_info_global_cls(symbol="全部")),
            ("东财全球快讯", lambda: self.ak.stock_info_global_em()),
            ("同花顺快讯", lambda: self.ak.stock_info_global_ths()),
            ("新浪全球快讯", lambda: self.ak.stock_info_global_sina()),
            ("东财财经早餐", lambda: self.ak.stock_info_cjzc_em()),
        )
        return probe_feeds(
            "akshare", probes, lambda probe: probe[0], lambda probe: records(probe[1]())
        )

    # ---- internals -------------------------------------------------------

    def _cached_rows(
        self,
        trade_date: date,
        key: str,
        label: str,
        loader,
    ) -> tuple[list[dict], str | None]:
        if self.cache is not None:
            cached = self.cache.read(trade_date, key)
            if isinstance(cached, list):
                return cached, None
        try:
            rows = records(loader())
        except Exception as exc:
            return [], f"{label}数据源不可用（{type(exc).__name__}），本次跳过"
        if self.cache is not None:
            self.cache.write(trade_date, key, rows)
        return rows, None




def _bare_code(symbol: str) -> str:
    return symbol.split(".", maxsplit=1)[0].strip()
