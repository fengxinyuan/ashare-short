"""带结构版本戳的 JSON 缓存与日线序列合并。

读取校验顶层对象与 schema，有效期由调用方检查；写入使用原子替换，失败保留旧文件。
序列在更新锁内按日期合并，同日后到覆盖，最终按日期升序保存。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ashare_decision_support.state.store import file_lock, read_json, write_json

STAMP_KEY = "schema"


def read_stamped(path: Path | None, schema: Any) -> dict[str, Any] | None:
    """缓存缺失、损坏、结构或版本不匹配时返回 None，由调用方重新取数。"""
    if path is None:
        return None
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get(STAMP_KEY) != schema:
        return None
    return payload


def write_stamped(
    path: Path | None,
    payload: dict[str, Any],
    schema: Any,
    *,
    indent: int | None = None,
) -> bool:
    """原子写入带版本戳的缓存；失败返回 False，保留原文件。"""
    if path is None:
        return False
    try:
        write_json(path, {**payload, STAMP_KEY: schema}, indent=indent, default=None)
    except (OSError, TypeError, ValueError):
        return False
    return True


# 日线序列


def series_bars(payload: dict[str, Any] | None) -> list[dict]:
    """序列文件里能用的那些根。没有 `date` 的行丢掉——它排不进序，也对不上前一天。"""
    bars = (payload or {}).get("bars")
    if not isinstance(bars, list):
        return []
    return [bar for bar in bars if isinstance(bar, dict) and bar.get("date")]


def read_series(path: Path | None, schema: Any) -> list[dict]:
    """读回一份序列缓存里的日线；缺失、损坏或版本不匹配时为空列表。"""
    return series_bars(read_stamped(path, schema))


def merge_series(
    path: Path | None,
    schema: Any,
    bars: list[dict],
    header: Callable[[dict[str, Any] | None], dict[str, Any]],
) -> bool:
    """在更新锁内把 `bars` 并进已有序列；锁或保存失败时保留旧文件并返回 False。

    `header` 拿到锁内读到的旧文件（缺失或版本不对时为 None），返回 `bars` 之外
    的字段：板块序列要从旧文件里续用名字，海外序列的头是固定的。版本不对的旧
    文件不参与合并，整份换掉。空 `bars` 不落盘。
    """
    if not bars or path is None:
        return False
    try:
        with file_lock(path):
            payload = read_stamped(path, schema)
            merged = {str(bar["date"]): bar for bar in series_bars(payload)}
            merged.update({str(bar["date"]): bar for bar in bars})
            return write_stamped(
                path,
                {**header(payload), "bars": [merged[stamp] for stamp in sorted(merged)]},
                schema,
            )
    except OSError:
        return False
