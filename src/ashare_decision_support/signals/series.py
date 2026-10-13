"""共用的日线归一化、交易日解析和序列运算。

时间统一为北京时间；数字经 finite 过滤。缺失值的兜底由各入口明确约定。"""

from __future__ import annotations

from datetime import date, datetime
from statistics import fmean
from typing import Any

from ashare_decision_support.numeric import finite, first_finite
from ashare_decision_support.schedule.timezone import (
    beijing_from_epoch,
    beijing_naive_now,
    to_beijing_naive,
)

# `strptime` 认得而 `fromisoformat` 认不得的那几种写法：斜杠分隔，以及月/日不补零
# 的 `2025-8-1`。**顺序不重要**——真正常见的两种（`2025-08-01` 与带时分秒的形式）
# 在下面走的是 `fromisoformat` 那条快路，根本到不了这里。
# `%Y%m%d` 放在这里是兜底：3.11 起 `fromisoformat` 自己就认 `20250801`。
_FALLBACK_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%Y%m%d",
)


def parse_bar_datetime(value: Any) -> datetime | None:
    """解析为北京墙上时间；无效输入返回 None，不用当前时间代替。

    ISO 格式优先走 fromisoformat，斜杠等旧格式再用 strptime。
    bar_datetime 保留归一化所需的缺失兜底，bar_date/bar_day 用于交易日对齐。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return to_beijing_naive(value)
    if value is None or value == "":
        return None
    # 八位整数是 `20250801` 这种写法，不是 epoch：当成秒数会落到 1970-08-23，
    # 而且不报错——`daily_cache` 此前按字符串解析它是对的，这里不能退步。
    # 真正的 epoch 秒在这个区间里对应 1970 年 8 月那几天，不会有 K 线。
    if isinstance(value, int) and 19000101 <= value <= 21001231:
        value = str(value)
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            return beijing_from_epoch(ts)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    try:
        return to_beijing_naive(datetime.fromisoformat(text))
    except ValueError:
        pass
    for fmt in _FALLBACK_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def bar_datetime(value: Any) -> datetime:
    """解析 K 线时间；缺失值回退到北京时间当前时刻，无效文本抛 ValueError。

    交易日对齐应使用 parse_bar_datetime 或 bar_date，避免把缺失日期当作今天。"""
    if value is None or value == "":
        return beijing_naive_now()
    moment = parse_bar_datetime(value)
    if moment is None:
        # 全都不认时抛的是 `fromisoformat` 的 ValueError，与收口前完全一致。
        return datetime.fromisoformat(str(value))
    return moment


def series(bars: list[dict] | None, key: str) -> list[float]:
    """提取一列有限数，跳过缺失值；不补零，调用方须检查有效样本数。"""
    values: list[float] = []
    for bar in bars or []:
        value = finite(bar.get(key))
        if value is not None:
            values.append(value)
    return values


def change(closes: list[float], window: int) -> float | None:
    """近 `window` 根的涨跌幅。样本不够或基期非正时返回 None。"""
    if len(closes) <= window:
        return None
    previous = closes[-window - 1]
    if previous <= 0:
        return None
    return round(closes[-1] / previous - 1.0, 6)


def volume_ratio(volumes: list[float], window: int, *, digits: int | None = None) -> float | None:
    """最新成交量除以前 window 根的正值均量；样本不足时返回 None。

    基线排除零量，最新成交量为零时仍返回零。威科夫使用中位数基线和独立的
    样本不足处理，不使用本函数。"""
    if len(volumes) < window + 1:
        return None
    baseline = [value for value in volumes[-(window + 1) : -1] if value > 0]
    if not baseline:
        return None
    ratio = volumes[-1] / fmean(baseline)
    return round(ratio, digits) if digits is not None else ratio


# 日线日期字段按优先级共用一张表，TickFlow 的 timestamp 优先。
# 缓存、复盘、相对强度与隔夜策略均经 bar_date/bar_day 读取。
# TickFlow 快照的 _source_datetime 单独按采集时刻排序，不属于日线日期口径。
_TIMESTAMP_KEYS = ("timestamp", "date", "datetime", "日期", "time", "时间")


def normalize_bars(bars: list[dict] | None) -> list[dict]:
    """将日线归一为 timestamp/open/high/low/close/volume，并按时间排序。

    兼容中英文字段名。缺少收盘价的记录丢弃；开盘缺失取收盘，最高/最低缺失
    取开收包络，成交量缺失取零。时间字段按 _TIMESTAMP_KEYS 取首个非空值。"""
    rows: list[dict] = []
    for row in bars or ():
        if not isinstance(row, dict):
            continue
        close = first_finite(row, "close", "收盘")
        if close is None:
            continue
        open_price = first_finite(row, "open", "开盘")
        high = first_finite(row, "high", "最高")
        low = first_finite(row, "low", "最低")
        volume = first_finite(row, "volume", "成交量")
        if open_price is None:
            open_price = close
        rows.append(
            {
                "timestamp": bar_datetime(_first_present(row, _TIMESTAMP_KEYS)),
                "open": open_price,
                "high": max(open_price, close) if high is None else high,
                "low": min(open_price, close) if low is None else low,
                "close": close,
                "volume": 0.0 if volume is None else volume,
            }
        )
    rows.sort(key=lambda item: item["timestamp"])
    return rows


def parse_bar_date(value: Any) -> date | None:
    """取北京时间对应的交易日；无效输入返回 None。"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    moment = parse_bar_datetime(value)
    return None if moment is None else moment.date()


def bar_date(row: dict) -> date | None:
    """按字段优先级取有效交易日，无法解析的字段继续尝试后续别名。"""
    for key in _TIMESTAMP_KEYS:
        value = row.get(key)
        if value and (day := parse_bar_date(value)) is not None:
            return day
    return None


def bar_day(row: dict) -> str | None:
    """返回统一的 YYYY-MM-DD 交易日键，无法解析时返回 None。"""
    day = bar_date(row)
    return None if day is None else day.isoformat()


def _first_present(row: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value:
            return value
    return None
