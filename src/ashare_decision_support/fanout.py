"""把一组互不依赖的取数并发跑掉，顺序和「失败只是少一路」的约定都不变。

**放在包根而不是 `news/` 下**，理由同 `numeric.py`：它约束的不是舆情，是**一类
取数形状**——N 个彼此无依赖的网络往返，任一路挂掉只算少一块覆盖面。今天的调用
方横跨两层：舆情五处（akshare 六路快讯、见闻两个频道、国内官方三个栏目、海外
五个 RSS、composite 四路源）和盘后龙虎榜席位的逐标的取数。

**为什么必须收口。** `adapters/limit_pool.py` 那套 `ThreadPoolExecutor` +
「失败返回 None 再统一记 note」的写法，仓库里已经有十份。这些地方如果各写一份，
差别不会出在并发上，会出在**顺序**和**失败语义**上：谁保留输入顺序、谁把异常吞成
空列表、谁把空结果也算失败。这三件事只要有一处不一致，同一条链路在不同源上的
行为就不一样了，而这种不一致在盘前是查不出来的——它只表现为「今天这一路没进来」。

**两条约定，写死在这里：**

- **保序**：结果按入参顺序返回，与完成顺序无关。舆情侧的 `retain_market_docs`
  按来源发配额，而**同一来源内部**取的是列表前缀——完成顺序一变，取到的就是另
  一批文档。盘前跑两次必须得到同一份输入。
- **抛异常 = 这一路失败**，不是「这一路今天没有内容」。两者的区别调用方要能
  看见，所以失败的入参连同异常一起回给调用方去写 note，这里不替它措辞——限流、
  证书过期和栏目改版在调用方眼里是三件不同的事。

串行时的代价是实测出来的：舆情层原先 14 次往返首尾相接 **15.61s**，并发后
**9.59s**，而其中 9.5s 是美国能源信息署那一路自己的服务端延迟（10KB 的响应），
也就是说除它之外的十三路加起来已经不到 1 秒。
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
