"""研究报告共用的评分、比例和名单格式化。

缺失读数显示为「—」，缺失名称显示为「未知」；此层只生成展示文本。"""

from __future__ import annotations

from typing import Any

MISSING = "—"

# 名单类读数只列前几个。研究包是给外部模型读的，把 90 个板块全铺开会把真正的
# 排序信号淹掉。
NAME_LIMIT = 5


def score(value: Any) -> str:
    """评分：浮点保留一位，其余原样，缺失是「—」。"""
    if value is None:
        return MISSING
    return f"{value:.1f}" if isinstance(value, float) else str(value)


def percent(value: Any) -> str:
    """比例读数，**带正负号**。入参是小数（0.0612 → +6.12%）。"""
    return MISSING if not isinstance(value, (int, float)) else f"{value:+.2%}"


def names(items: Any) -> str:
    """把板块名单压成一行。取不出名字的写「未知」，不印 `None`。"""
    if not items:
        return MISSING
    return "、".join(
        str(item.get("name") or item.get("sector") or "未知")
        if isinstance(item, dict)
        else str(item)
        for item in items[:NAME_LIMIT]
    )
