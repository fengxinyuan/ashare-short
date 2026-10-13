"""并发执行独立任务，结果按输入顺序返回。

成功结果与失败项分别返回，失败项保留原输入及异常。
空结果仍是成功调用，覆盖说明和重试策略由调用方决定。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")

# 舆情源都是外部公开接口，并发太高只会换来对端限流。这几处的路数本来就是个位数，
# 上限存在的意义是防止有人往 listings 里塞几十条之后把自己打成 429。
MAX_WORKERS = 8


def gather(
    items: Sequence[T],
    fetch: Callable[[T], R],
    max_workers: int = MAX_WORKERS,
) -> tuple[list[tuple[T, R]], list[tuple[T, Exception]]]:
    """并发跑 `fetch`，返回 (按入参顺序的成功对, 按入参顺序的 (失败项, 异常))。

    `fetch` 允许抛异常；抛出即算这一路失败。成功但返回空结果的一路算**成功**，
    因为「今天这个栏目没更新」和「这个栏目取不到」是两回事，只有后者该进 note。

    异常本身也一并交还，不是只交还「失败了」：note 里那个 `ConnectionError` /
    `HTTPError` 的类名是排查时唯一的线索，而这一层看不到该怎么措辞——限流、
    证书过期和栏目改版在调用方眼里是三件不同的事。
    """
    if not items:
        return [], []

    def execute(item: T) -> tuple[R | None, Exception | None]:
        try:
            return fetch(item), None
        except Exception as exc:
            return None, exc

    workers = min(max(1, max_workers), len(items))
    if workers == 1:
        outcomes = [execute(item) for item in items]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # `pool.map` 保序，这正是上面第一条约定要的。
            outcomes = list(pool.map(execute, items))

    paired = list(zip(items, outcomes, strict=True))
    done = [(item, value) for item, (value, exc) in paired if exc is None]
    failed = [(item, exc) for item, (_, exc) in paired if exc is not None]
    return done, failed  # type: ignore[return-value]
