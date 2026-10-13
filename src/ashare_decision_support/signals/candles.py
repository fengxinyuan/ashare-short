"""K 线收盘位置，供量价画像与隔夜筛选共用。"""

from __future__ import annotations

from ashare_decision_support.numeric import finite


def close_position(row: dict | None) -> float | None:
    """收盘价在当日区间的位置：0 = 最低，1 = 最高。

    OHLC 缺失或无效时返回 None。一字板没有可定义的日内位置，也返回 None，
    避免把封板误读为中性收盘。
    """
    if not row:
        return None
    values = [finite(row.get(key)) for key in ("open", "high", "low", "close")]
    if any(value is None for value in values):
        return None
    _, high, low, close = values
    if high <= low:
        return None
    return (close - low) / (high - low)
