"""JSON-file persistence with atomic writes.

Atomic matters more than it looks: the monitor loop rewrites the alert state
every couple of minutes, and a crash mid-write on a plain `open(...,"w")`
leaves a truncated file that fails to parse on the next run. The system then
re-fires every alert it had already sent. Write to a temp file in the same
directory and rename — rename is atomic on POSIX.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, returning `default` for missing or corrupt files.

    A corrupt state file must not take down a run. The caller gets the default
    and rebuilds, which for every store here is a recoverable position.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
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
        handle.write(json.dumps(record, ensure_ascii=False, default=str))
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
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    rows.append(record)
    except OSError:
        return []
    return rows
