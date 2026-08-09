"""舆情源的可达性探针，口径对齐 `adapters/` 那十个 `health_check`。

**为什么补这个。** `scripts/source_audit.py` 能逐个 probe 行情源并给出「今天这台
机器上哪几路能用、多慢」，而舆情层此前一个 `health_check` 都没有——同样是外部
免费接口，同样会挂，唯独这一层只能靠盘前跑一次真链路来发现。差别在故障那天最
明显：行情源挂了审计脚本立刻指出是哪一路，舆情源挂了只在简报末尾多一行 note。

**逐路报，不报一个总的布尔值。** 「舆情可用」对这一层没有意义：四路源覆盖面
互补（盘面 / 宏观数据 / 国内政策 / 海外官方），少哪一路决定的是**少哪一块**，
不是少多少条。一个 `reachable: true` 会把「政策源挂了但快讯还在」显示成健康。

探针**不走缓存**：它问的是「现在还能不能打通」，缓存命中只能证明今天早些时候
能打通。这和 `fetch_market` 的取舍正好相反，所以两条路分开写。
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
