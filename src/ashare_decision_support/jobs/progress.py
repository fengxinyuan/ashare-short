"""跑批过程中的进度回调。

进度是**给人看的旁路**：CLI 用它在长跑批里打「① 正在算板块」，守护进程把它丢
进日志。它和结果无关——一条进度打不出来，跑批的产出一个字都不会变。

所以这里把回调包成「永不抛」的样子。让一个 print 的 BrokenPipeError（比如输出
被 `head` 截断）中断盘前主链路，是把一次显示故障升级成了一次没有简报的早上。

`morning` 与 `advise` 原来各自内联了一份逐字相同的包装。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ProgressFn = Callable[[str], Any] | None


def progress_reporter(progress: ProgressFn) -> Callable[[str], None]:
    """把一个可选的进度回调包成一个总能调用、且不会抛的函数。

    传 None 就得到一个什么都不做的函数，调用方因此不必在每个打点处写
    `if progress is not None`——那种写法一旦漏掉一处就是一次 AttributeError。
    """

    def report(message: str) -> None:
        if progress is None:
            return
        try:
            progress(message)
        except Exception:
            return

    return report
