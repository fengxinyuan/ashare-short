"""可选的跑批进度回调；显示失败不影响任务结果。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ProgressFn = Callable[[str], Any] | None


def progress_reporter(progress: ProgressFn) -> Callable[[str], None]:
    """包装可选回调，忽略回调异常；None 对应无操作。"""

    def report(message: str) -> None:
        if progress is None:
            return
        try:
            progress(message)
        except Exception:
            return

    return report
