"""Day-scoped JSON cache for news feeds.

The pre-open job may be run several times while you are reading the brief, and
every run would otherwise re-hit the same public endpoints. Caching per trading
day per feed keeps repeat runs free and, more importantly, makes the morning
reproducible: two runs at 07:40 and 08:20 read the same documents.

**这条承诺曾经只对四路源里的一路成立。** 只有 `AkshareNewsSource` 收 cache，
而华尔街见闻（两个频道各翻十几页）、国内官方栏目、海外 RSS——恰好是最慢的三路
——每次跑都重新打网络。症状不是变慢那么简单：07:40 和 08:20 两次盘前，快讯流
本来就在动，拿到的是**两批不同的文档**，于是板块舆情分也是两个数，而简报上看不
出来是哪一次。`read_docs` / `write_docs` 就是为了让那三路也能进同一套缓存。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.news.base import NewsDoc

CACHE_DIRNAME = "news_cache"


class NewsCache:
    def __init__(self, root: str | Path, ttl_seconds: int = 3600) -> None:
        self.root = Path(root) / CACHE_DIRNAME
        self.ttl_seconds = ttl_seconds

    def path_for(self, trade_date: date, key: str) -> Path:
        safe_key = "".join(char if char.isalnum() or char in "-_" else "_" for char in key)
        return self.root / str(trade_date) / f"{safe_key}.json"

    def read(self, trade_date: date, key: str) -> Any | None:
        path = self.path_for(trade_date, key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        stamp = payload.get("cached_at")
        if not isinstance(stamp, str):
            return None
        try:
            cached_at = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        age = (datetime.now(UTC) - cached_at).total_seconds()
        if age > self.ttl_seconds:
            return None
        return payload.get("data")

    def write(self, trade_date: date, key: str, data: Any) -> bool:
        path = self.path_for(trade_date, key)
        payload = {
            "cached_at": datetime.now(UTC).isoformat(),
            "trade_date": str(trade_date),
            "key": key,
            "data": data,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # `default=str` because feeds hand back native date/Timestamp values
            # (news_cctv does) that json cannot encode. Stringifying is correct
            # here: the cache round-trips into text-scoring code either way.
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        except (OSError, TypeError, ValueError):
            # A cache that cannot be written is a performance problem, never a
            # correctness one — the caller already has the live data.
            return False
        return True

    # ---- NewsDoc 直存 ------------------------------------------------------
    #
    # akshare 那一路缓存的是**上游原始行**，因为它的取数和映射是分开的两步。
    # 另外三路的解析（HTML 列表页、RSS/Atom、见闻分页 JSON）和映射是同一步，
    # 缓存原始页面等于把解析也留到下次重做，还得连页面编码一起存。存已经归一
    # 好的 `NewsDoc` 更省事，也让缓存里的东西是可读的。

    def read_docs(self, trade_date: date, key: str) -> list[NewsDoc] | None:
        """取回缓存的文档；没有、过期或格式不对都返回 None（＝没有缓存）。"""
        cached = self.read(trade_date, key)
        if not isinstance(cached, list):
            return None
        try:
            return [_doc(row) for row in cached if isinstance(row, dict)]
        except (TypeError, ValueError):
            # 缓存格式对不上就当没有——`NewsDoc` 加过字段的旧缓存会落到这里，
            # 而重新打一次网络永远是安全的退路。
            return None

    def write_docs(self, trade_date: date, key: str, docs: list[NewsDoc]) -> bool:
        return self.write(trade_date, key, [asdict(doc) for doc in docs])


def _doc(row: dict[str, Any]) -> NewsDoc:
    # `symbols` 存进 JSON 变成 list，取回来必须还原成 tuple——`NewsDoc` 是 frozen
    # 且要可哈希的，list 会让下游按文档去重的地方在缓存命中时才炸。
    return NewsDoc(**{**row, "symbols": tuple(row.get("symbols") or ())})


def cached_docs(
    cache: NewsCache | None,
    trade_date: date,
    key: str,
    fetch: Callable[[], list[NewsDoc]],
) -> list[NewsDoc]:
    """取缓存，没有才打网络，取回来再写一份。`cache` 为 None 就是直接打网络。

    **按栏目/feed 一个键，不按整源一个键。** 分路取数的源里每一路分属不同机构、
    各自会挂；整源一个键的话，一路超时就会让另外两路已经取回的内容一起失效，
    下次重跑再全打一遍。

    收口的理由不是这八行，是**「miss 才写」这个顺序**：`official_source` 与
    `rss_source` 此前各写一遍，两边一旦有一边把 `write_docs` 写进了 miss 分支
    外面，症状是那一路每次都重写缓存文件——不报错，也不改变返回值，只有翻缓存
    目录的 mtime 才看得出来。
    """
    if cache is not None:
        hit = cache.read_docs(trade_date, key)
        if hit is not None:
            return hit
    docs = fetch()
    if cache is not None:
        cache.write_docs(trade_date, key, docs)
    return docs
