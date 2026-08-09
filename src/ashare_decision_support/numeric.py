"""数字收口：把外部数据里的「像个数」变成一个可用的 float，或者变成 None。

行情源返回的数字有六七种坏法：字符串 `"12.5"`、缺失占位 `"-"`、空串、`"null"`、
NaN、正负无穷，以及 numpy 的标量包装。此前每个适配器各写一份 `_float`——
仓库里有九份，**没有两份的判据是一样的**：有的挡 NaN 不挡 inf，有的挡空串不挡
NaN，有的连 `True` 都会算成 1.0。

各写一份的代价不是重复，是**每份漏掉的那一项都不同**。一个 NaN 从没挡它的那份
流进来，不会报错——它会一路参与比较（NaN 的比较永远是 False），把「跌破止损」
静默变成「没跌破」。

这里只有一条判据：**能变成有限实数就要，否则一律 None**。下游因此只需要判
`is None`，不需要再各自防一次 NaN。
"""

from __future__ import annotations

import math
from typing import Any

# 数据源用来表示「这一格没有值」的几种写法。挡在这里，是因为它们全都能骗过
# 一个只写了 try/float 的解析器：`float("-")` 抛异常还算好的，`float("nan")`
# 会**成功**返回一个 NaN。
MISSING_TOKENS = frozenset({"", "-", "--", "null", "None", "N/A", "n/a", "nan"})


def finite(value: Any) -> float | None:
    """收成一个有限的 float，收不成返回 None。

    挡掉 bool 是有意的：`float(True)` 是 1.0，而一个本该是价格的字段里出现
    `True`，含义是「上游给错了」，不是「价格等于一块钱」。

    **判据只有一条，快路只是少绕几步。** 开头那个 `type(value) is float` 不改变
    任何一个输入的结论——它只是让「已经是 float」这种最常见的情况跳过两次
    `isinstance` 和一次 `float()`。这个函数是全仓调用最密的一个（一次 400 只票的
    `analyze_universe` 里 238 万次、一次全市场板块动量里 197 万次），实测这条快路
    让盘前选股段整体快 14%。

    用 `type(...) is float` 而不是 `isinstance(...)` 是必须的：`numpy.float64`
    **是 `float` 的子类**，`isinstance` 会把它放进快路并**原样返回一个 numpy 标量**，
    而现在的行为是经 `float()` 转成 Python 原生 float。`adapters/qmt_xtdata._to_plain`
    整个存在的理由就是不让 numpy 标量漏进下游，这里不能反过来放一个口子。
    精确类型判断让 numpy 标量照旧走下面的慢路，结论与转换都一个字不变。
    """
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
    except (TypeError, ValueError):
        return None
    # NaN 与 ±inf 都不是读数。放它们过去等于让下游每一处比较都悄悄变成 False。
    return number if math.isfinite(number) else None


def positive(value: Any) -> float | None:
    """有限且大于 0，否则 None。

    价格、成交量、成交额这类字段上，0 的含义是「没取到」而不是「等于零」——
    真的停牌是没有这一行，不是价格为 0。
    """
    number = finite(value)
    return number if number is not None and number > 0.0 else None


def first_finite(row: dict[str, Any], *keys: str) -> float | None:
    """按顺序试几个字段名，取第一个能收成有限 float 的。

    同一个读数在不同源里叫不同名字（`close` / `收盘` / `last`），调用方给出
    优先级，这里只负责「哪个先收得住」。
    """
    for key in keys:
        number = finite(row.get(key))
        if number is not None:
            return number
    return None


def clamp(value: float, low: float, high: float) -> float:
    """夹到 `[low, high]` 里。

    收在这里不是因为这两行难写，是因为它在四个打分层里各写了一份（`wyckoff` /
    `daily_technical` / `rule_based` / `predict`），共 44 处调用——而**夹持是评分
    的量纲约定**：这套系统里所有的分项都承诺落在自己声明的区间内，越界的分项会
    在合成时把总分推出 0-100。四份实现今天逐字相同，靠的是没人动过它们；一旦
    有人在其中一份上加个「越界时记一条 note」，另外三条链路就会在同样的越界上
    保持沉默。

    **NaN 进来会被悄悄压成 `low`，不是原样交还。** `max(low, min(nan, high))` 里
    两次比较全是 False，于是 `min` 交还 nan、`max` 交还 `low`——实测
    `clamp(nan, -2.5, 2.5) == -2.5`。也就是说一个「上游给错了」会变成**这一项能
    给的最负的分**，而不是一个一眼能看出不对的 NaN。

    所以 NaN 必须挡在取数那一层（`finite`），这里挡不住也不该挡：在夹持这步把
    它转成 0 或抛异常，都是把一个取数问题伪装成评分问题。这条写在这里是因为
    **它曾经写反过**——原文说「会原样交还 NaN」，于是 `scoring/` 里两处用
    `isinstance(v, (int, float))` 收数的地方看起来无害，实际每个 NaN 都在给板块
    关系分打满负分。
    """
    return max(low, min(value, high))


def as_int(value: Any, default: int = 0) -> int:
    """收成 int，收不成返回 `default`。

    先过 `finite`，所以 `"3.0"`、NaN、`True` 的结果分别是 3、default、default
    ——而不是 int("3.0") 抛异常、int(nan) 抛异常、int(True) 得 1。
    """
    number = finite(value)
    return default if number is None else int(number)
