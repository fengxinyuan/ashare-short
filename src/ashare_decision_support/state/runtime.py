"""同一状态目录中的长运行任务互斥，进程退出后由操作系统释放。"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import FileLockBusyError, file_lock

_P = ParamSpec("_P")
_R = TypeVar("_R")


def exclusive_run(name: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """第二个实例立即报告占用；不同状态目录互不阻塞。"""

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(function)
        def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                with file_lock(state_path("runtime", name), blocking=False):
                    return function(*args, **kwargs)
            except FileLockBusyError as exc:
                raise RuntimeError(
                    f"{name} 已有实例正在使用当前状态目录；请停止旧实例或设置其他 ASHARE_DS_HOME"
                ) from exc

        return guarded

    return decorate
