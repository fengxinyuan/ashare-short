"""逐路检查舆情源可达性、延迟与覆盖。

探针直接请求来源，不使用响应缓存；汇总保留各路失败信息。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from ashare_decision_support.fanout import gather

T = TypeVar("T")


def probe_feeds(
    source_name: str,
    units: Sequence[T],
    label: Callable[[T], str],
    fetch: Callable[[T], Any],
) -> dict[str, Any]:
    """并发探测每一路，返回逐路结果 + 整源汇总。

    `fetch` 抛异常算这一路不可达；返回空结果算**可达但今天没有内容**——这两件
    事在 note 里必须分得开，理由见 `fanout.py`。
    """
    started = time.monotonic()
    done, failed = gather(units, fetch)

    feeds: dict[str, dict[str, Any]] = {}
    for unit, rows in done:
        feeds[label(unit)] = {"reachable": True, "count": _count(rows), "error": None}
    for unit, exc in failed:
        feeds[label(unit)] = {
            "reachable": False,
            "count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }

    reachable = [name for name, item in feeds.items() if item["reachable"]]
    return {
        "source": source_name,
        # 整源的 `reachable` 是「至少还有一路」，因为这一层的降级单位就是一路。
        # 覆盖面缺口看 `unreachable_feeds`，那才是决定简报少哪一块的东西。
        "reachable": bool(reachable),
        "latency_ms": int((time.monotonic() - started) * 1000),
        "feed_count": len(feeds),
        "reachable_feeds": sorted(reachable),
        "unreachable_feeds": sorted(name for name in feeds if name not in set(reachable)),
        "feeds": dict(sorted(feeds.items())),
    }


def _count(rows: Any) -> int:
    try:
        return len(rows)
    except TypeError:
        return 0
