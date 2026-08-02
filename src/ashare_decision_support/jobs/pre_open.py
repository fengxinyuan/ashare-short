"""Pre-open job: restate yesterday's matrix as this morning's execution card.

Adds no new market view — the free tier has nothing new to say before the open.
Its job is to put the levels, position bands and auction-timing rules in front of
the trader while there is still time to act on them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from ashare_decision_support.models.schemas import DailyTechnicalSnapshot, DecisionMatrix
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier, render_pre_open_brief
from ashare_decision_support.notify.base import Notifier
from ashare_decision_support.jobs.post_close import SNAPSHOT_LATEST_NAME


def run_pre_open(
    matrix_path: str | Path,
    output_dir: str | Path | None = None,
    adapter: Any | None = None,
    notifiers: list[Notifier] | None = None,
) -> dict[str, Any]:
    matrix_file = Path(matrix_path)
    matrix = DecisionMatrix.model_validate(json.loads(matrix_file.read_text(encoding="utf-8")))

    target_dir = Path(output_dir) if output_dir else matrix_file.parent
    snapshots, snapshot_note = _load_snapshots(matrix, matrix_file, adapter)

    data_notes = [f"决策矩阵来源：{matrix_file}"]
    if snapshot_note:
        data_notes.append(snapshot_note)

    content = render_pre_open_brief(matrix, snapshots, data_notes)
    title = f"盘前提示（对应 {matrix.trade_date}）"

    file_notifier = FileNotifier(target_dir, slug="pre-open")
    channels = notifiers if notifiers is not None else [ConsoleNotifier(), file_notifier]
    delivered = [notifier.capabilities.channel for notifier in channels if notifier.send(title, content)]

    return {
        "title": title,
        "trade_date": str(matrix.trade_date),
        "candidates": len(matrix.candidates),
        "delivered": delivered,
        "data_notes": data_notes,
        "brief_path": str(file_notifier.last_path) if file_notifier.last_path else None,
        "content": content,
    }


def _load_snapshots(
    matrix: DecisionMatrix,
    matrix_file: Path,
    adapter: Any | None,
) -> tuple[dict[str, DailyTechnicalSnapshot], str | None]:
    """Prefer the snapshots the post-close run already wrote; refetch only if asked."""
    sibling = matrix_file.parent / SNAPSHOT_LATEST_NAME
    if sibling.exists():
        try:
            payload = json.loads(sibling.read_text(encoding="utf-8"))
            snapshots = {
                symbol: DailyTechnicalSnapshot.model_validate(item) for symbol, item in payload.items()
            }
            return snapshots, f"价位取自盘后快照：{sibling.name}"
        except (OSError, ValueError) as exc:
            return {}, f"盘后快照读取失败（{type(exc).__name__}），本次不展示价位参考"

    if adapter is None:
        return {}, "未找到盘后快照且未提供行情源，本次不展示价位参考"

    from datetime import date, timedelta

    from ashare_decision_support.signals import DailyTechnicalAnalyzer

    symbols = [candidate.symbol for candidate in matrix.candidates]
    if not symbols:
        return {}, None
    end = date.today()
    bars = adapter.get_historical_bars(symbols, end - timedelta(days=180), end)
    usable = {symbol: rows for symbol, rows in bars.items() if rows}
    return DailyTechnicalAnalyzer().analyze_many(usable), "价位为盘前实时重算"
