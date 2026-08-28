"""次日盘前：把昨日盘后的执行卡复述一遍。

不产生任何新的市场判断——开盘前没有当日分时，现算只会拿到昨天的数据然后被当成
今天的。它的职责是在还来得及动手的时候，把价位、仓位上限与竞价纪律摆到眼前。

**这条路只在 CLI 上。** 守护进程 09:15 的 `preopen` 任务跑的是 `run_advise`
（见 `jobs/daily_cycle.py`），那条路有当日分时可用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ashare_decision_support.artifacts import POST_CLOSE_LATEST_NAME, SNAPSHOT_LATEST_NAME
from ashare_decision_support.models.schemas import DailyTechnicalSnapshot
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier, render_pre_open_brief
from ashare_decision_support.notify.base import Notifier, publish, written_path
from ashare_decision_support.schedule.timezone import beijing_today


def run_pre_open(
    payload_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    adapter: Any | None = None,
    notifiers: list[Notifier] | None = None,
) -> dict[str, Any]:
    source = Path(payload_path) if payload_path else Path("briefs") / POST_CLOSE_LATEST_NAME
    payload = json.loads(source.read_text(encoding="utf-8"))

    target_dir = Path(output_dir) if output_dir else source.parent
    rows = list(payload.get("actionable") or [])
    snapshots, snapshot_note = _load_snapshots(rows, source, adapter)

    data_notes = [f"执行卡来源：{source}"]
    if snapshot_note:
        data_notes.append(snapshot_note)

    content = render_pre_open_brief(payload, snapshots, data_notes)
    title = f"盘前提示（对应 {payload.get('trade_date')}）"

    file_notifier = FileNotifier(target_dir, slug="pre-open")
    channels = notifiers if notifiers is not None else [ConsoleNotifier(), file_notifier]
    delivered = publish(channels, title, content)

    return {
        "title": title,
        "trade_date": str(payload.get("trade_date")),
        "candidates": len(rows),
        "watch_only": len(payload.get("watch_only") or []),
        "delivered": delivered,
        "data_notes": data_notes,
        "brief_path": written_path(channels, file_notifier),
        "content": content,
    }


def _load_snapshots(
    rows: list[dict],
    source: Path,
    adapter: Any | None,
) -> tuple[dict[str, DailyTechnicalSnapshot], str | None]:
    """优先复用盘后已经写好的快照，只有明确要求时才重新取数。"""
    sibling = source.parent / SNAPSHOT_LATEST_NAME
    if sibling.exists():
        try:
            payload = json.loads(sibling.read_text(encoding="utf-8"))
            snapshots = {
                symbol: DailyTechnicalSnapshot.model_validate(item)
                for symbol, item in payload.items()
            }
            return snapshots, f"价位取自盘后快照：{sibling.name}"
        except (OSError, ValueError) as exc:
            return {}, f"盘后快照读取失败（{type(exc).__name__}），本次不展示价位参考"

    if adapter is None:
        return {}, "未找到盘后快照且未提供行情源，本次不展示价位参考"

    from datetime import timedelta

    from ashare_decision_support.signals import DailyTechnicalAnalyzer

    symbols = [row["symbol"] for row in rows if row.get("symbol")]
    if not symbols:
        return {}, None
    end = beijing_today()
    bars = adapter.get_historical_bars(symbols, end - timedelta(days=180), end)
    usable = {symbol: rows_ for symbol, rows_ in bars.items() if rows_}
    return DailyTechnicalAnalyzer().analyze_many(usable), "价位为盘前实时重算"
