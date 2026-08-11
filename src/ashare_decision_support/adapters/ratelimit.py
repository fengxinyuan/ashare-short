"""免费档速率闸门：客户端节流 + 服务端 429 重试。

TickFlow 免费档是 **60 次 / 滚动 60 秒** 的硬闸门（2026-08-01 实测：第 61 次调用
必定 429，且 60 次只花了 22.5 秒——按自然节奏打，配额会以 2.7 倍速烧光）。

盘前全链路一次要打的次数远超 60：申万分类 335 个分片、板块动量 45 个批次、候选
股 240 日历史再若干批、外加 instruments。所以这里做两件事，缺一不可：

1. **事前节流**——滑动窗口令牌桶，把调用摊平到闸门以下，让 429 根本不发生；
2. **事后重试**——真撞上了就按服务端给的等待时长退避（错误消息里带
   「请 38266ms 后重试」，比任何本地猜测都准）。

只做第 2 件的话，每分钟都会先撞一次墙再等 38 秒，全链路会被拖到十几分钟；只做
第 1 件的话，多进程或时钟漂移下仍会漏。

付费档没有这个闸门，`limit=None` 时本模块退化为透明代理，零开销。
"""

from __future__ import annotations

import os
import re
import time
from collections import deque
from collections.abc import Callable
from typing import Any

# 服务端在 429 消息里自报的等待时长，例：「请 38266ms 后重试」。
_WAIT_MS = re.compile(r"(\d+)\s*ms\s*后重试")

# 实测闸门是 60/min。默认留 5 次余量给重试与时钟漂移——跑满 60 意味着任何一次
# 抖动都会变成一次 429，省下的那 5 次配额买不到等价的时间。
DEFAULT_FREE_TIER_LIMIT = 55
WINDOW_SECONDS = 60.0

# 撞墙后的重试次数。服务端给的等待时长是准的，两次退避还不成通常是配额被同机的
# 另一个进程占着，这时候继续等不如把错误抛给调用方。
MAX_RETRIES = 3
RETRY_BUFFER_SECONDS = 0.5


def is_rate_limit_error(exc: BaseException) -> bool:
    """判定一个异常是不是限流。

    不 import SDK 的异常类——适配层已经把原始异常包装成 ``TickFlowAdapterError``
    的路径上，类型信息只剩消息文本了，按类名与文本双路识别更稳。
    """
    if type(exc).__name__ == "RateLimitError":
        return True
    if getattr(exc, "status_code", None) == 429:
        return True
    text = str(exc)
    return "频率超限" in text or "RateLimitError" in text


def retry_after_seconds(exc: BaseException, default: float = 5.0) -> float:
    match = _WAIT_MS.search(str(exc))
    if match:
        return int(match.group(1)) / 1000.0 + RETRY_BUFFER_SECONDS
    return default


def resolve_limit(api_key: str | None) -> int | None:
    """付费档不限流；免费档读 ``TICKFLOW_RATE_LIMIT`` 覆盖，非法值退回默认。

    显式设成 0 或负数表示「关掉节流」，用于测试与本地压测。
    """
    if api_key:
        return None
    raw = os.getenv("TICKFLOW_RATE_LIMIT")
    if raw is None:
        return DEFAULT_FREE_TIER_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_FREE_TIER_LIMIT
    return value if value > 0 else None


class RateLimiter:
    """滑动窗口令牌桶。``acquire()`` 阻塞到有空位为止。"""

    def __init__(
        self,
        limit: int,
        window: float = WINDOW_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limit = limit
        self.window = window
        self._monotonic = monotonic
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self.waited_seconds = 0.0
        self.wait_count = 0

    def acquire(self) -> None:
        now = self._monotonic()
        self._evict(now)
        if len(self._calls) >= self.limit:
            # 最老的一次调用滑出窗口的时刻，就是下一个空位出现的时刻。
            gap = self._calls[0] + self.window - now
            if gap > 0:
                self.wait_count += 1
                self.waited_seconds += gap
                self._sleep(gap)
                now = self._monotonic()
                self._evict(now)
        self._calls.append(now)

    def penalise(self, seconds: float) -> None:
        """撞墙后调用：等待期间不能再放行，所以把窗口填满。"""
        self._sleep(seconds)
        now = self._monotonic()
        self._evict(now)
        self.wait_count += 1
        self.waited_seconds += seconds

    def _evict(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self.window:
            self._calls.popleft()

    def stats(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "waits": self.wait_count,
            "waited_seconds": round(self.waited_seconds, 1),
        }


class _Proxy:
    """透明代理的共同部分：可调用的过闸门，命名空间继续往下包，其余原样透传。

    `_Namespace` 和 `RateLimitedClient` 的转发逻辑必须**逐字一致**——一边改了
    另一边没改，表现是「顶层调用限了速、`client.klines.*` 没限」，而超限只在
    跑量大的那天才炸。所以共同部分放在基类里，两个子类只保留各自多出来的东西。
    """

    def __init__(self, target: Any, limiter: RateLimiter) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_limiter", limiter)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(object.__getattribute__(self, "_target"), name)
        limiter = object.__getattribute__(self, "_limiter")
        if callable(attr) and not isinstance(attr, type):
            return _throttled(attr, limiter)
        if _is_namespace(attr):
            return _Namespace(attr, limiter)
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_target"), name, value)


class _Namespace(_Proxy):
    """代理 ``client.klines`` 这类中间层，把下面的可调用对象继续包起来。"""


def _is_namespace(value: Any) -> bool:
    """SDK 的资源命名空间（``klines``/``universes``/``instruments``）。

    判据是「非内置类型的对象且带公开方法」，比枚举名字白名单更耐 SDK 升级。
    """
    if value is None or isinstance(value, (str, bytes, int, float, bool, list, tuple, dict, set)):
        return False
    return any(
        callable(getattr(value, name, None)) for name in dir(value) if not name.startswith("_")
    )


def _throttled(func: Callable[..., Any], limiter: RateLimiter) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        last: BaseException | None = None
        for _ in range(MAX_RETRIES):
            limiter.acquire()
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                if not is_rate_limit_error(exc):
                    raise
                last = exc
                limiter.penalise(retry_after_seconds(exc))
        assert last is not None
        raise last

    wrapper.__name__ = getattr(func, "__name__", "throttled")
    return wrapper


class RateLimitedClient(_Proxy):
    """TickFlow SDK client 的透明代理，所有调用先过闸门。

    包在 client 而不是适配器方法上，是因为 `sectors/taxonomy.py` 拿的是
    `adapter.client` 直接调 `universes.*`，绕过了适配器的全部方法。包 client
    是唯一能同时覆盖两条路径的位置。

    转发逻辑全在 `_Proxy` 里；这个子类只为**类型可辨**而存在——`wrap_client`
    的返回值要能一眼看出「限速了没有」。
    """


def wrap_client(client: Any, api_key: str | None = None, limit: int | None = -1) -> Any:
    """按档位决定是否加闸门。付费档或显式关闭时原样返回。"""
    effective = resolve_limit(api_key) if limit == -1 else limit
    if not effective:
        return client
    return RateLimitedClient(client, RateLimiter(effective))
