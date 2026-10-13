"""Keep operational data problems visible when a report is compressed."""

from __future__ import annotations

URGENT_MARKERS = (
    "不可执行",
    "候选仅供观察",
    "不完整",
    "扫描缺失",
    "没有全市场行情",
    "缺少必要",
    "主攻板块无候选",
)
DEGRADED_MARKERS = (
    "失败",
    "不可用",
    "未测到",
    "未经",
    "行情源",
    "日线",
    "可交易性",
)


def compact_notes(notes: list[str], limit: int) -> list[str]:
    """Preserve severe caveats before routine collection notes, with stable order."""
    unique = list(dict.fromkeys(notes))

    def priority(note: str) -> int:
        if any(marker in note for marker in URGENT_MARKERS):
            return 0
        if any(marker in note for marker in DEGRADED_MARKERS):
            return 1
        return 2

    return sorted(unique, key=priority)[:limit]
