"""外部数字的统一解析与基本统计。

数值转换拒绝缺失、布尔值、NaN、Infinity 和溢出；可用值转为 Python 原生 float。
缺失返回 None，避免把未知读数当作零参与计算。"""

from __future__ import annotations

import math
from collections.abc import Iterable
from statistics import median
from typing import Any

# 数据源用来表示「这一格没有值」的几种写法。挡在这里，是因为它们全都能骗过
# 一个只写了 try/float 的解析器：`float("-")` 抛异常还算好的，`float("nan")`
# 会**成功**返回一个 NaN。
MISSING_TOKENS = frozenset({"", "-", "--", "null", "None", "N/A", "n/a", "nan"})


def finite(value: Any) -> float | None:
    """转为有限的 Python float；缺失、布尔值或无法转换时返回 None。

    原生 float 直接检查有限性。精确类型判断保证 numpy.float64 等子类仍经过
    float() 转换，避免向计算结果和 JSON 输出透传外部标量类型。"""
    if type(value) is float:
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if value in MISSING_TOKENS:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    # NaN 与 ±inf 都不是读数。放它们过去等于让下游每一处比较都悄悄变成 False。
    return number if math.isfinite(number) else None


def positive(value: Any) -> float | None:
    """有限且大于零的读数；零、负数或无效输入返回 None。"""
    number = finite(value)
    return number if number is not None and number > 0.0 else None


def first_finite(row: dict[str, Any], *keys: str) -> float | None:
    """按给定字段优先级取第一个有限数；所有字段无效时返回 None。"""
    for key in keys:
        number = finite(row.get(key))
        if number is not None:
            return number
    return None


def mean(values: Iterable[Any]) -> float | None:
    """有限值的算术平均；没有有效样本时返回 None。

    零和负数均参与统计；需要正数口径的调用方先用 positive 过滤。"""
    usable = [number for number in (finite(value) for value in values) if number is not None]
    return sum(usable) / len(usable) if usable else None


def median_of(values: Iterable[Any]) -> float | None:
    """有限值的中位数；没有有效样本时返回 None。"""
    usable = [number for number in (finite(value) for value in values) if number is not None]
    return median(usable) if usable else None


def compound(returns: Iterable[float]) -> float:
    """逐日收益率连乘成区间收益：`(1+r1)(1+r2)…-1`。

    `news/theme_history.py` 与 `sectors/outcomes.py` 各写过一份，两份逐字相同——
    它们算的是同一件事（信号发出后 N 日的板块与市场收益），改一处漏一处就会让
    题材账本和板块账本在同一段行情上报出两个「超额收益」。入参由调用方先收
    成有限值；这里不再过 `finite`，因为一个 None 混进复利里没有合理的默认值，
    调用方该在取数时就整段放弃，而不是让这里悄悄跳过一天。
    """
    value = 1.0
    for item in returns:
        value *= 1.0 + item
    return value - 1.0


def clamp(value: float, low: float, high: float) -> float:
    """把已校验的数值限制在 [low, high]。

    调用方须先用 finite 排除 NaN：Python 的 min/max 会将 NaN 压成 low，
    不能用此函数代替输入校验。"""
    return max(low, min(value, high))


def as_int(value: Any, default: int = 0) -> int:
    """有限数截断为 int；无效输入返回 default，支持字符串 "3.0"。"""
    number = finite(value)
    return default if number is None else int(number)
