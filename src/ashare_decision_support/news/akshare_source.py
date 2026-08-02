"""AkShare-backed news collection.

Every feed here is free and needs no key. They were picked by what actually
answered on this host — the東方财富 `push2` bulk endpoints (spot quotes, board
lists) are unreachable here, so nothing in the sentiment path depends on them.

Each fetch is independently guarded: one dead feed costs you its documents and
a note in the brief, never the whole run.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

from ashare_decision_support.news.base import NewsDoc, NewsSource
from ashare_decision_support.news.cache import NewsCache

# Per-stock news is one call per symbol. Matching `sina_intraday.py`'s pool size
# keeps the concurrent footprint against free endpoints predictable.
DEFAULT_SYMBOL_WORKERS = 8

# 公告回溯天数。要盖住「周一盘前回看上周五」这段最长的自然日间隔（含三天小长假
# 时是 4 天），再多留一天余量。回溯得再远就不是「昨晚的公告」了，风险否决用陈年
# 公告没有意义。
ANNOUNCEMENT_LOOKBACK_DAYS = 5

# 新闻联播回溯天数。盘前跑时今晚的还没播，取昨天的即可；留 3 天盖住节假日停播。
CCTV_LOOKBACK_DAYS = 3


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

    # ---- market-wide -----------------------------------------------------

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        docs: list[NewsDoc] = []
        notes: list[str] = []

        for key, label, loader, mapper in (
            (
                "cls_telegraph",
                "财联社电报",
                lambda: self.ak.stock_info_global_cls(symbol="全部"),
                lambda row: NewsDoc(
                    title=_text(row, "标题"),
                    summary=_text(row, "内容"),
                    source="财联社",
                    published_at=f"{_text(row, '发布日期')} {_text(row, '发布时间')}".strip(),
                ),
            ),
            (
                "em_global",
                "东财全球快讯",
                lambda: self.ak.stock_info_global_em(),
                lambda row: NewsDoc(
                    title=_text(row, "标题"),
                    summary=_text(row, "摘要"),
                    source="东方财富",
                    published_at=_text(row, "发布时间"),
                    url=_text(row, "链接"),
                ),
            ),
            (
                "cctv_news",
                "新闻联播",
                # 盘前 08:30 跑时今晚的联播还没播，稿子也还没上网——和公告完全
                # 同一类问题。硬取「今天」的结果是**政策主线源每天早上静默缺席**，
                # 而它是这条链路里唯一的政策输入。回退到最近一期有稿的日子。
                lambda: self._latest_cctv(trade_date),
                lambda row: NewsDoc(
                    title=_text(row, "title"),
                    summary=_text(row, "content")[:500],
                    source="新闻联播",
                    published_at=_text(row, "date"),
                ),
            ),
        ):
            rows, note = self._cached_rows(trade_date, key, label, loader)
            if note:
                notes.append(note)
            docs.extend(mapper(row) for row in rows)

        docs = [doc for doc in docs if doc.title]
        if not docs:
            notes.append("所有市场级舆情源均不可用，本次板块打分不含舆情项")
        return docs[:limit], notes

    # ---- per-stock -------------------------------------------------------

    def _latest_cctv(self, trade_date: date) -> Any:
        """最近一期有稿的新闻联播。空表和抛异常都算「这天没有」。"""
        last_error: Exception | None = None
        for offset in range(CCTV_LOOKBACK_DAYS + 1):
            day = trade_date - timedelta(days=offset)
            try:
                rows = self.ak.news_cctv(date=day.strftime("%Y%m%d"))
            except Exception as exc:  # noqa: BLE001 - 逐日试探，最后一个错误再上报
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
                    title=_text(row, "新闻标题"),
                    summary=_text(row, "新闻内容")[:500],
                    source=_text(row, "文章来源"),
                    published_at=_text(row, "发布时间"),
                    url=_text(row, "新闻链接"),
                    symbols=(symbol,),
                )
                for row in _rows(table)[:limit]
                if _text(row, "新闻标题")
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
                    "symbol": _text(row, "代码"),
                    "name": _text(row, "名称"),
                    "title": _text(row, "公告标题"),
                    "category": _text(row, "公告类型"),
                    "published_at": _text(row, "公告日期"),
                    "url": _text(row, "网址"),
                }
                for row in rows
                if _text(row, "代码")
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
            f"回溯 {ANNOUNCEMENT_LOOKBACK_DAYS} 天内没有取到任何公告，"
            "本次**没有公告风险否决**，候选未经该层过滤"
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
            code = _text(row, "代码")
            if not code:
                continue
            parsed.append(
                {
                    "symbol": code,
                    "name": _text(row, "名称"),
                    "net_buy": _float(row, "龙虎榜净买额"),
                    "change_pct": _float(row, "涨跌幅"),
                    "trade_date": _text(row, "上榜日"),
                }
            )
        return parsed, ([note] if note else [])

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
            rows = _rows(loader())
        except Exception as exc:
            return [], f"{label}数据源不可用（{type(exc).__name__}），本次跳过"
        if self.cache is not None:
            self.cache.write(trade_date, key, rows)
        return rows, None


def _rows(table: Any) -> list[dict]:
    if table is None:
        return []
    if isinstance(table, list):
        return [row for row in table if isinstance(row, dict)]
    records = getattr(table, "to_dict", None)
    if callable(records):
        return list(table.to_dict(orient="records"))
    return []


def _text(row: dict, key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def _float(row: dict, key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # drop NaN


def _bare_code(symbol: str) -> str:
    return symbol.split(".")[0].strip()
