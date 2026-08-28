"""宏观与政策快讯：华尔街见闻电报。

**为什么要加这一路。** 2026-08-02 实测现有三路舆情（财联社电报 20 条、东财全球快讯
180 条、新闻联播）在全市场 200 条里的关键词命中：

| 关键词 | 命中 |
|---|---|
| 伊朗 / 中东 | 66 / 26 ✅ 地缘覆盖得很好 |
| **PMI / 统计局 / 制造业** | **0 / 0 / 0** |
| **美债 / 收益率 / 降准 / 关税** | **0 / 0 / 0 / 0** |

也就是说：**宏观数据发布对这个系统是完全不可见的**。同一天 07-31 09:30 公布的
「中国 7 月官方制造业 PMI 49.2（前值 50.3）、非制造业 49.0、综合 49.3」——三个全线
跌破荣枯线——链路里一个字都没有。这不是权重问题，是这类消息压根没进来。

对盘前研判来说这是个硬缺口：板块方向可以靠量价推，**风格切换不行**。PMI 破荣枯线
直接对应「宽松预期升温 → 红利/消费占优、顺周期走弱」，而这条推理链的输入端是空的。

**数据源。** 华尔街见闻 `api-one.wallstcn.com` 电报接口，本机可达（东财 `push2` 被
代理拒的那套限制不适用于这个 host）。两个频道：`global-channel`（宏观/海外/大宗）
与 `a-stock-channel`（A 股）。单页上限 100 条，用 `cursor` 往前翻。

**只做宏观补充，不替换任何现有源。** 财联社/东财/联播覆盖盘面与政策文件，这一路
补的是数据发布与海外流动性——两边重叠的部分由 `news/dedup.py` 按标题去重合并。
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from ashare_decision_support.fanout import gather
from ashare_decision_support.news.balance import retain_market_docs
from ashare_decision_support.news.base import NewsDoc, NewsSource
from ashare_decision_support.news.cache import NewsCache
from ashare_decision_support.news.feed_source import USER_AGENT
from ashare_decision_support.news.health import probe_feeds
from ashare_decision_support.schedule.timezone import BEIJING_TZ

BASE_URL = "https://api-one.wallstcn.com/apiv1/content/lives?channel={channel}&limit={limit}"
CURSOR_SUFFIX = "&cursor={cursor}"

CHANNEL_GLOBAL = "global-channel"
CHANNEL_A_SHARE = "a-stock-channel"

PAGE_LIMIT = 100          # 服务端硬上限

# 回溯**时间窗**，不是页数。两个频道的流量差一个数量级（global 一天上千条、
# a-stock 一天几百条），固定页数会让高流量频道只翻到几小时前——而 07-31 09:30
# 发布的 PMI 恰好落在那个盲区里。按时间窗翻页，两个频道才覆盖同一段历史。
#
# 72 小时：周一盘前要回看到**上周五开盘**（周五 09:00 → 周一 08:30 是 71.5 小时）。
# 取 60 小时差一点够不着周五早盘的数据发布——07-31 09:30 的 PMI 就正好落在窗外。
DEFAULT_LOOKBACK_HOURS = 72
MAX_PAGES = 16           # 失控保护：单频道最多翻 1200 条
TIMEOUT_SECONDS = 15

# 交易所时钟。**不在这里另立一份**：`schedule/timezone.py` 写着「时钟只有这一个
# 来源」，而这一行原来是它的第二份私有副本（第三份在
# `adapters/tickflow_adapter.py`）。固定 +08:00 与 `Asia/Shanghai` 在 1992 年以后
# 的每一天都相等，只有 1986-1991 那几年的夏令时不同——快讯时间戳到不了那里。


class WallstreetNewsSource(NewsSource):
    """宏观 / 政策 / 海外流动性快讯。"""

    name = "wallstreetcn"

    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
        timeout: int = TIMEOUT_SECONDS,
        channels: tuple[str, ...] = (CHANNEL_GLOBAL, CHANNEL_A_SHARE),
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
        max_pages: int = MAX_PAGES,
        cache: NewsCache | None = None,
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.channels = channels
        self.lookback_hours = lookback_hours
        self.max_pages = max_pages
        self.cache = cache
        self.last_market_stats: dict[str, object] = {}

    # ---- market-wide -----------------------------------------------------

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        docs: list[NewsDoc] = []
        notes: list[str] = []
        # 截止时间锚在 `trade_date` 当天 24:00，而不是 `now`——这样同一个交易日
        # 无论几点跑，取回的窗口都一样，盘前跑两次不会得到两份不同的输入。
        cutoff = int(
            (
                datetime.combine(trade_date, datetime.min.time(), tzinfo=BEIJING_TZ)
                + timedelta(days=1)
                - timedelta(hours=self.lookback_hours)
            ).timestamp()
        )

        # 两个频道各自要翻十几页，彼此没有依赖。并发之后这一段的墙钟时间由**翻页
        # 更深的那个频道**决定，而不是两个频道之和。
        fetched, unreachable = gather(
            self.channels, lambda channel: self._channel_docs(trade_date, channel, cutoff)
        )
        # `_channel` 自己不抛异常，翻第一页就失败时返回 ok=False；两种失败都要
        # 进同一条 note，否则「频道挂了」会读起来像「今天这个频道没消息」。
        partial = {channel for channel, (_, ok) in fetched if not ok}
        dead = {channel for channel, _exc in unreachable}
        failed = [channel for channel in self.channels if channel in partial or channel in dead]
        for _, (got, ok) in fetched:
            if ok:
                docs.extend(got)

        if failed:
            notes.append(
                f"华尔街见闻 {'/'.join(failed)} 频道不可用，本次宏观快讯不完整"
            )
        if not docs:
            notes.append("华尔街见闻快讯全部不可用，本次无宏观数据发布输入")
            return [], notes

        # 同一条快讯可能同时出现在两个频道，按标题去重（跨源去重由 news/dedup.py 做）。
        unique: dict[str, NewsDoc] = {}
        for doc in docs:
            unique.setdefault(doc.title, doc)
        result = sorted(unique.values(), key=lambda d: d.published_at or "", reverse=True)
        oldest = result[-1].published_at[:16] if result and result[-1].published_at else "?"
        notes.append(
            f"华尔街见闻快讯 {len(result)} 条（宏观数据发布与海外流动性），回溯至 {oldest}"
        )
        # 这一路的 `raw_by_source` 交给文档自报：两个频道混在一起去重之后，一条
        # 快讯来自哪个频道已经无从分辨，而它自报的 `source` 才是下游按来源配额
        # 分配时真正用的那个键。
        result, self.last_market_stats = retain_market_docs(result, limit)
        return result, notes

    def fetch_for_symbols(
        self, symbols: list[str], limit: int = 10
    ) -> tuple[dict[str, list[NewsDoc]], list[str]]:
        """个股舆情不走这一路——这是宏观源，按标的检索没有意义。"""
        return {}, []

    def fetch_announcements(self, trade_date: date) -> tuple[list[dict], list[str]]:
        return [], []

    def fetch_dragon_tiger(self, start: date, end: date) -> tuple[list[dict], list[str]]:
        return [], []

    def health_check(self) -> dict[str, Any]:
        # 只探第一页：这里问的是「频道还通不通」，翻满 16 页是取数的事。
        return probe_feeds(
            "wallstreetcn",
            self.channels,
            lambda channel: channel,
            lambda channel: (self._page(channel, None) or {}).get("items") or [],
        )

    # ---- internals -------------------------------------------------------

    def _channel_docs(
        self, trade_date: date, channel: str, cutoff: int | None
    ) -> tuple[list[NewsDoc], bool]:
        """带缓存的单频道取数。缓存命中直接算成功。

        这一路是四路源里最贵的：一个频道最多翻 16 页、每页 100 条，两个频道就是
        32 个往返。盘前重跑一次就再来一遍，而 `cutoff` 已经锚死在交易日 24:00，
        同一天翻回来的本来就该是同一批——那就没有理由再打一次。

        **只缓存翻完整的那次。** `_channel` 中途失败会返回已经拿到的半截加
        `ok=True`（有多少算多少），把半截写进缓存等于让后面每一次重跑都只看到
        半截，而且看不出来。半截值得用，不值得记住。
        """
        key = f"wallstreet_{channel}"
        if self.cache is not None:
            cached = self.cache.read_docs(trade_date, key)
            if cached is not None:
                return cached, True
        docs, ok, complete = self._channel(channel, cutoff)
        if ok and complete and self.cache is not None:
            self.cache.write_docs(trade_date, key, docs)
        return docs, ok

    def _channel(self, channel: str, cutoff: int | None) -> tuple[list[NewsDoc], bool, bool]:
        """返回 (文档, 这一路算不算取到, 是否翻完整)。

        第三个值只给缓存用。翻页中途挂掉时前两个值仍然是「有多少算多少」——那是
        这一路一贯的降级约定，不因为多了个缓存就改。
        """
        docs: list[NewsDoc] = []
        cursor: str | None = None
        for _ in range(max(self.max_pages, 1)):
            try:
                payload = self._page(channel, cursor)
            except Exception:
                return docs, bool(docs), False
            items = payload.get("items")
            if not isinstance(items, list) or not items:
                break
            docs.extend(doc for doc in (_to_doc(item) for item in items) if doc is not None)

            oldest = min(
                (int(i.get("display_time") or 0) for i in items if isinstance(i, dict)),
                default=0,
            )
            if cutoff is not None and oldest and oldest <= cutoff:
                break
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return docs, True, True

    def _page(self, channel: str, cursor: str | None) -> dict:
        url = BASE_URL.format(channel=channel, limit=PAGE_LIMIT)
        if cursor:
            url += CURSOR_SUFFIX.format(cursor=cursor)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with self._opener(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "ignore"))
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}


def _to_doc(item: Any) -> NewsDoc | None:
    if not isinstance(item, dict):
        return None
    # 快讯常常只有正文没有标题，此时正文就是标题——丢掉这些等于丢掉大部分数据发布，
    # 因为「中国7月官方制造业PMI 49.2，前值 50.3」正是以正文形式发出来的。
    title = str(item.get("title") or "").strip()
    text = str(item.get("content_text") or "").strip()
    if not title:
        title = text
    if not title:
        return None
    return NewsDoc(
        title=title[:200],
        summary=text[:500] if text and text != title else "",
        source="华尔街见闻",
        published_at=_stamp(item.get("display_time")),
        url=str(item.get("uri") or "") or None,
    )


def _stamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""
