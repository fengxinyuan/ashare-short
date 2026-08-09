"""JSON-file persistence with atomic writes.

Atomic matters more than it looks: the monitor loop rewrites the alert state
every couple of minutes, and a crash mid-write on a plain `open(...,"w")`
leaves a truncated file that fails to parse on the next run. The system then
re-fires every alert it had already sent. Write to a temp file in the same
directory and rename — rename is atomic on POSIX.

**NaN 在这一层两个方向都要挡住，而 Python 的 `json` 默认两个方向都放行。**

- 写：`json.dump` 对 NaN 会写出裸的 `NaN` —— 这**不是合法 JSON**。文件从此
  `jq` 读不了、别的语言读不了，而 Python 自己读得回来，所以在这套系统内部完全
  没有症状。持仓成本、止损、台账都存在这里。
- 读：`json.load` **默认接受** `NaN` / `Infinity` 字面量并还原成 float。于是一个
  被写坏过一次的状态文件会**永久**把 NaN 喂给每一次跑批：成本是 NaN，浮盈是
  NaN，「跌破止损」的比较恒为 False。

写侧改成 `allow_nan=False`（抛错，暴露在写入点，而不是在三天后的某次告警里）；
读侧把这两个字面量当成文件损坏，走既有的「返回 default、调用方重建」那条路 ——
那正是本模块开头承诺的处置方式。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _reject_constant(name: str) -> Any:
    """`NaN` / `Infinity` 出现在状态文件里 = 这个文件坏了，不是一个读数。"""
    raise ValueError(f"state file contains a non-finite JSON constant: {name}")


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, returning `default` for missing or corrupt files.

    A corrupt state file must not take down a run. The caller gets the default
    and rebuilds, which for every store here is a recoverable position.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle, parse_constant=_reject_constant)
    except (OSError, ValueError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 — 下面 `with handle:` 接管，失败路径显式 unlink
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    try:
        with handle:
            # `allow_nan=False`：宁可在写入点抛，也不要写出一个别的工具读不了、
            # 而 Python 自己读得回来的文件——后者三天后才会以「止损没触发」暴露。
            json.dump(
                payload, handle, ensure_ascii=False, indent=2, default=str, allow_nan=False
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def append_jsonl(path: Path, record: dict) -> None:
    """Append one record. Used by the journal, which is write-once-per-event."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str, allow_nan=False))
        handle.write("\n")


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, skipping unparseable lines rather than failing."""
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line, parse_constant=_reject_constant)
                except ValueError:
                    # 台账是逐行独立的，坏一行只丢一行——含 NaN 的那条同理。
                    continue
                if isinstance(record, dict):
                    rows.append(record)
    except OSError:
        return []
    return rows
