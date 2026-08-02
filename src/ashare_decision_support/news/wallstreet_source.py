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
from datetime import date, datetime, timezone, timedelta
from typing import Any, Callable

from ashare_decision_support.news.base import NewsDoc, NewsSource

BASE_URL = "https://api-one.wallstcn.com/apiv1/content/lives?channel={channel}&limit={limit}"
CURSOR_SUFFIX = "&cursor={cursor}"
USER_AGENT = "Mozilla/5.0"

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

CN_TZ = timezone(timedelta(hours=8))


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
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.channels = channels
        self.lookback_hours = lookback_hours
        self.max_pages = max_pages

    # ---- market-wide -----------------------------------------------------

    def fetch_market(self, trade_date: date, limit: int = 200) -> tuple[list[NewsDoc], list[str]]:
        docs: list[NewsDoc] = []
        notes: list[str] = []
        failed: list[str] = []

        # 截止时间锚在 `trade_date` 当天 24:00，而不是 `now`——这样同一个交易日
        # 无论几点跑，取回的窗口都一样，盘前跑两次不会得到两份不同的输入。
        cutoff = int(
            (
                datetime.combine(trade_date, datetime.min.time(), tzinfo=CN_TZ)
                + timedelta(days=1)
                - timedelta(hours=self.lookback_hours)
            ).timestamp()
        )

        for channel in self.channels:
            got, ok = self._channel(channel, cutoff)
            if not ok:
                failed.append(channel)
                continue
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
        return result[:limit], notes

    def fetch_for_symbols(
        self, symbols: list[str], limit: int = 10
    ) -> tuple[dict[str, list[NewsDoc]], list[str]]:
        """个股舆情不走这一路——这是宏观源，按标的检索没有意义。"""
        return {}, []

    def fetch_announcements(self, trade_date: date) -> tuple[list[dict], list[str]]:
        return [], []

    def fetch_dragon_tiger(self, start: date, end: date) -> tuple[list[dict], list[str]]:
        return [], []

    # ---- internals -------------------------------------------------------

    def _channel(self, channel: str, cutoff: int | None) -> tuple[list[NewsDoc], bool]:
        docs: list[NewsDoc] = []
        cursor: str | None = None
        for _ in range(max(self.max_pages, 1)):
            try:
                payload = self._page(channel, cursor)
            except Exception:
                return docs, bool(docs)
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
        return docs, True

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
        return datetime.fromtimestamp(int(value), CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""
