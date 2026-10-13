"""JSON 状态读写：有限数校验、原子替换与协作式更新锁。

读取失败返回调用方的默认值；写入失败保留原文件。原子替换防止半份文件，
需要合并旧状态的调用方还必须在 `file_lock` 内完成读取、修改和写入，避免丢更新。
锁只覆盖本机上使用同一协议的写入者，不协调手工编辑或跨机器共享文件。
"""

from __future__ import annotations

import errno
import json
import math
import os
import tempfile
from _thread import LockType
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Any, TextIO
from weakref import WeakValueDictionary

_THREAD_LOCKS: WeakValueDictionary[Path, LockType] = WeakValueDictionary()
_THREAD_LOCKS_GUARD = Lock()


class FileLockBusyError(RuntimeError):
    """所需文件锁已被另一个线程或进程占用。"""


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """串行化同一路径的读改写；不要嵌套获取同一把锁。

    锁固定的旁路文件，不能锁会被原子替换的 JSON 本身，也不能在释放后删锁文件：
    等待者仍可能持有旧文件句柄。系统在进程退出时释放文件锁。
    """
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCKS_GUARD:
        local_lock = _THREAD_LOCKS.setdefault(resolved, Lock())
    if not local_lock.acquire(blocking=blocking):
        raise FileLockBusyError(str(resolved))
    try:
        with open(resolved.with_name(f".{resolved.name}.lock"), "a+b") as handle:
            if os.name == "nt":
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(
                        handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1
                    )
                except OSError as exc:
                    if not blocking and exc.errno in (errno.EAGAIN, errno.EACCES, errno.EDEADLK):
                        raise FileLockBusyError(str(resolved)) from exc
                    raise
                release = partial(msvcrt.locking, handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                except BlockingIOError as exc:
                    raise FileLockBusyError(str(resolved)) from exc
                release = partial(fcntl.flock, handle.fileno(), fcntl.LOCK_UN)
            try:
                yield
            finally:
                release()
    finally:
        local_lock.release()


def _reject_constant(name: str) -> Any:
    """`NaN` / `Infinity` 出现在状态文件里 = 这个文件坏了，不是一个读数。"""
    raise ValueError(f"state file contains a non-finite JSON constant: {name}")


def _parse_float(value: str) -> float:
    number = float(value)
    # `1e999` 是合法 JSON 数字，但 float 会把它溢出成 inf；parse_constant 看不到它。
    if not math.isfinite(number):
        _reject_constant(value)
    return number


def read_json(path: Path, default: Any = None) -> Any:
    """文件缺失、不可读或 JSON 损坏时返回 default；数据形状由调用方校验。"""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle, parse_constant=_reject_constant, parse_float=_parse_float)
    except (OSError, ValueError):
        return default


def read_json_object(path: Path) -> dict[str, Any]:
    """读取对象状态；语法正确但顶层类型错误的 JSON 同样按缺失处理。"""
    payload = read_json(path)
    return payload if isinstance(payload, dict) else {}


def record_map(value: Any) -> dict[str, dict]:
    """读取按名称索引的状态记录，坏记录不影响同表内的其他记录。"""
    if not isinstance(value, dict):
        return {}
    return {key: row for key, row in value.items() if isinstance(row, dict)}


def write_json(
    path: Path,
    payload: Any,
    *,
    indent: int | None = 2,
    default: Callable[[Any], Any] | None = str,
) -> None:
    with _atomic_text_file(path) as handle:
        json.dump(
            payload, handle, ensure_ascii=False, indent=indent, default=default, allow_nan=False
        )


def write_text(path: Path, content: str) -> None:
    """文本报告与 JSON 使用相同的原子替换和失败恢复约定。"""
    with _atomic_text_file(path) as handle:
        handle.write(content)


@contextmanager
def _atomic_text_file(path: Path) -> Iterator[TextIO]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 — 下面 `with handle:` 接管，失败路径显式 unlink
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    try:
        with handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        with suppress(OSError):
            Path(handle.name).unlink(missing_ok=True)
        raise


def append_jsonl(path: Path, record: dict) -> None:
    """追加并同步一条记录；合并或去重的调用方须持有 file_lock。"""
    encoded = (json.dumps(record, ensure_ascii=False, default=str, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        offset = handle.tell()
        prefix = b""
        if offset:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                prefix = b"\n"
        try:
            handle.write(prefix + encoded)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            with suppress(OSError):
                handle.truncate(offset)
                handle.flush()
            raise


def read_jsonl(path: Path, notes: list[str] | None = None) -> list[dict]:
    """Read a JSONL file, skipping unparseable lines rather than failing."""
    rows: list[dict] = []
    try:
        with open(path, "rb") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(
                        line, parse_constant=_reject_constant, parse_float=_parse_float
                    )
                except ValueError:
                    # 台账是逐行独立的，坏一行只丢一行——含 NaN 的那条同理。
                    if notes is not None:
                        notes.append(f"{path.name} 第 {number} 行损坏，已跳过")
                    continue
                if isinstance(record, dict):
                    rows.append(record)
                elif notes is not None:
                    notes.append(f"{path.name} 第 {number} 行不是记录对象，已跳过")
    except OSError as exc:
        if notes is not None and path.exists():
            notes.append(f"读取 {path.name} 失败：{type(exc).__name__}")
        return []
    return rows
