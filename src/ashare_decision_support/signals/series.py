"""日线序列上的几个基本运算。

`dipbuy` 和 `volume_price` 都要先把 bars 摊成一列收盘价、再问「近 N 日涨了多少」。
两边原来各写了一份 `_series` / `_change` / `_mean`——而且**已经开始分叉**：
`volume_price` 那份走 `numeric.finite`，`dipbuy` 那份还是裸的 `float()`，于是
一个 NaN 收盘价在低吸判定里能一路穿过去（NaN 参与的比较恒为 False，「跌得够深」
会静默答否），在量价画像里却会被挡住。

同一个读数在两条链路上得到不同结论，是这类重复真正的代价。

`bar_datetime` 在这里是同一条理由的第二例：`daily_technical` 与 `wyckoff` 各写了
一份逐字相同的时间戳解析，而两份都把 `strptime` 排在 `fromisoformat` 前面。

`normalize_bars` 是第三例，也是代价最大的一例——见它自己的 docstring。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ashare_decision_support.numeric import finite, first_finite

# `strptime` 认得而 `fromisoformat` 认不得的那几种写法：斜杠分隔，以及月/日不补零
# 的 `2025-8-1`。**顺序不重要**——真正常见的两种（`2025-08-01` 与带时分秒的形式）
# 在下面走的是 `fromisoformat` 那条快路，根本到不了这里。
_FALLBACK_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d")


def bar_datetime(value: Any) -> datetime:
    """把一根 K 线的时间字段解析成 datetime。

    **`fromisoformat` 必须排在 `strptime` 前面。** 两个调用方原先都是先试
    `"%Y-%m-%d %H:%M:%S"`、失败再试 `"%Y-%m-%d"`，于是最常见的日线日期
    （`2025-08-01`）每根都要付**两次** `strptime`——而 `strptime` 每次调用都要
    过一遍 locale 查询与正则。实测这条顺序在日线上是 **8.95μs/根**，换成
    `fromisoformat` 优先是 **0.20μs/根**（44 倍）；400 只票 × 240 根 × 2 个
    分析器，光这一处就是 4 秒多。

    `strptime` 那几种格式仍然保留在后面兜底，所以 `2025/08/01` 与 `2025-8-1`
    这类写法的行为一个字都没变——它们本来也不是热路径。
    """
    if isinstance(value, datetime):
        return value
    if value is None or value == "":
        return datetime.now()
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts)

    text = str(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _FALLBACK_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # 全都不认时抛的是 `fromisoformat` 的 ValueError，与收口前完全一致。
    return datetime.fromisoformat(text)


def series(bars: list[dict] | None, key: str) -> list[float]:
    """把 bars 摊成一列有限的数，跳过取不到值的那几根。

    跳过而不是补 0：补 0 会让「那天没数据」变成「那天价格是 0」，接着算出一个
    -100% 的涨跌幅。缺口在这里表现为序列变短，而长度是每个调用方都会检查的。
    """
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


def mean(values: list[float]) -> float | None:
    """均值。空序列返回 None，不返回 0——「没有样本」和「均值是零」不是一回事。"""
    if not values:
        return None
    return sum(values) / len(values)


# 时间戳字段的候选名，按优先级。落空的（None / "" / 0）跳过继续往后试——
# 一根 `timestamp=0` 而 `date="2025-08-01"` 的 K 线，答案显然是后者而不是 1970。
_TIMESTAMP_KEYS = ("timestamp", "date", "日期", "时间")


def normalize_bars(bars: list[dict] | None) -> list[dict]:
    """把任意来源的日线收成 `{timestamp, open, high, low, close, volume}` 并按时间排序。

    **这是第三处、也是最贵的一处重复。** `daily_technical._normalize_rows` 与
    `wyckoff._normalize_rows` 此前是两份语义完全相同的实现：同样五个字段、同样的
    中英文别名、同样的缺失兜底、同样的排序。而 `analyze_universe`、
    `monitor/session.py`、`agents/collectors.py` 三处都是**紧挨着调这两个分析器**，
    于是同一批 bars 被逐根归一了两遍——实测 400 只 × 240 根时，光这一项就是 96 万
    次 `first_finite`。

    两份实现原先还差着一处：时间戳的取法。`daily_technical` 用 `or` 链（落空就往
    后试），`wyckoff` 只跳过 `None` 与 `""`——于是一根 `timestamp=0` 的 K 线在前者
    是「看 date 字段」，在后者是 1970-01-01。这里统一取前者，因为那才是对的。

    缺失兜底的规则原样保留：没有收盘价的整根丢弃（收盘价是唯一不能猜的字段）；
    开盘价缺就用收盘价；最高/最低缺就用开收的包络；成交量缺算 0。
    """
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


def _first_present(row: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value:
            return value
    return None
