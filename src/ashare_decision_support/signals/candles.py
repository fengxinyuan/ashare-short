"""单根 K 线的几何，以及相邻 K 线之间的包含关系。

**这一层只出几何事实。** 「今天的收盘价高于昨天那根阴线的开盘价」是事实，「反包
成立、可以上车」是判断——判断在 `strategy/playbook.py`。理由和
`signals/volume_price.py` 模块说明里那条完全一样：同一个几何形态在不同位置下含义
相反，一根阳线吞掉前一根阴线，在深跌末端是衰竭反转，在高位第三次出现是诱多。

## 为什么反包要比到「前一根阴线的开盘价」

反包这个词唯一的实质内容是**筹码结构**，不是形状。收盘价站上前一根阴线的开盘价，
意味着那一天买入的人**全部回本或浮盈**，该日堆积的套牢盘归零，上方那一段解套抛压
随之消失。比到最高价（强反包）则连当天在最高位接的人也解套了。

所以判据必须是前一根的 `open` / `high` 这两个**价位**，不是「阳线实体比阴线长」。
系统里此前最接近反包的是 `playbook._check_reversal` 里那个叫 `engulf` 的判据——它
只看当日收阳且放量，从不比对前一根的价位，严格说是「放量阳线」而不是反包。两者在
深跌末端经常同时成立，所以那个命名一直没被发现是错的。

## 十字星不构成被反包的对象

`open == close` 时既不算阳也不算阴。`engulf()` 要求被吞的那一根是**阴线**，十字星
因此直接不成立——这是刻意从严：实体为零的那一天没有堆积出可辨认的套牢盘，「把它
反包掉」没有对应的筹码含义，判成立只会让这个信号在横盘区间里频繁误报。

## 缺数据一律按「不成立」处理

`engulf()` 在数据不足时和在形态不成立时都返回 None，这是本模块唯一一处刻意的合并。
其余读数（`close_position` / `body_position`）仍然遵守全仓库的规矩：算不出来返回
None，不返回 0。

合并的理由是**反包是确认型判据**：它的作用是给一个买入动作放行，而「没有数据确认」
和「确认了但不成立」对买入侧是同一个结论——不放行。这与 `playbook._check_breakout`
把 `expanding is None` 处理成「放行并标注」的取向相反，因为那里 `expanding` 是**加严
条件**（缺了它不该反过来否决一个本来成立的突破），这里 `engulf` 是**成立条件**本身。
两种缺省方向对应两种角色，不该统一。
"""

from __future__ import annotations

from dataclasses import dataclass

from ashare_decision_support.numeric import finite

# 反包的两档。字符串而不是 bool，因为「反包了」和「强反包」在下游是不同的仓位
# 待遇——后者上方连当日最高位的套牢盘都清掉了。
ENGULF_BODY = "反包"
ENGULF_RANGE = "强反包"


@dataclass(frozen=True)
class Candle:
    """一根 K 线的四个价位。构造保证 high ≥ low，其余不做校验。"""

    open: float
    high: float
    low: float
    close: float

    @property
    def up(self) -> bool:
        """阳线。十字星（open == close）既不是阳也不是阴。"""
        return self.close > self.open

    @property
    def down(self) -> bool:
        return self.close < self.open

    @property
    def body_top(self) -> float:
        return max(self.open, self.close)

    @property
    def body_bottom(self) -> float:
        return min(self.open, self.close)


def candle(row: dict | None) -> Candle | None:
    """一根 bar 收成 `Candle`。四个价位缺任意一个、或 high < low 就返回 None。

    `high < low` 是数据错误而不是极端行情，遇到就整根弃权——用一根上下颠倒的 K 线
    算出来的反包会是随机的。一字板（high == low）是合法的，保留。
    """
    if not row:
        return None
    values = [finite(row.get(key)) for key in ("open", "high", "low", "close")]
    if any(value is None for value in values):
        return None
    open_, high, low, close = values  # type: ignore[misc]
    if high < low:
        return None
    return Candle(open=open_, high=high, low=low, close=close)


def close_position(row: dict | None) -> float | None:
    """收盘价在当日区间里的位置：0 = 收在最低，1 = 收在最高。

    一字板（high == low）返回 None——那一天的「日内位置」没有定义，给 0.5 会让
    一字涨停在「天量滞涨」这类判据里被当成中性收盘。
    """
    bar = candle(row)
    if bar is None or bar.high <= bar.low:
        return None
    return (bar.close - bar.low) / (bar.high - bar.low)


def body_position(row: dict | None, reference: dict | None) -> float | None:
    """`row` 的收盘价落在 `reference` 实体的什么位置：0 = 实体下沿，1 = 实体上沿。

    用来表达「D1 那根倍量阴收在 D0 涨停实体的上半部还是下半部」——上半部是换手，
    下半部是出货（见 `docs/KNOWLEDGE_VOLUME_PRICE.md` §3.3 约束 2）。

    **不截断到 [0, 1]。** 收在参考实体之上会得到 >1，之下会得到 <0，两者都是真实
    且有用的信息；`volume_price._range_position` 截断是因为那是个分位数，按定义就在
    区间内，这里不是。

    参考实体为零高度（十字星）时返回 None：除数是 0，且「落在一个没有高度的实体的
    什么位置」本身无意义。
    """
    bar = candle(row)
    base = candle(reference)
    if bar is None or base is None:
        return None
    span = base.body_top - base.body_bottom
    if span <= 0:
        return None
    return (bar.close - base.body_bottom) / span


def engulf(bars: list[dict] | None, cover: int = 1) -> str | None:
    """最后 `cover` 根 K 线，对它们前面那一根阴线的反包程度。

    `cover=1` 是常规反包：今天这根阳线吞没昨天那根阴线。
    `cover=2` 回答白皮书那条「高价区一根长阴，若后两根大阳也不能吞没，表示天价
    成立」——问的是同一件事，只是覆盖方多了一根，所以共用一个函数而不是写两个。

    判据只看**最后一根的收盘价**与被吞那根的 `open` / `high`，中间几根怎么走不影响
    结论：反包问的是「那天的套牢盘解没解套」，解套与否只取决于现价在哪，不取决于
    路径。

    不成立时返回 None，数据不足时同样返回 None——见模块 docstring 里对这处合并的
    说明。
    """
    if cover < 1:
        return None
    rows = bars or []
    if len(rows) < cover + 1:
        return None

    target = candle(rows[-cover - 1])
    last = candle(rows[-1])
    if target is None or last is None:
        return None
    # 被吞的必须是阴线，收尾的必须是阳线。少了任一条，「解套」这个说法就不成立。
    if not target.down or not last.up:
        return None

    if last.close > target.high:
        return ENGULF_RANGE
    if last.close >= target.open:
        return ENGULF_BODY
    return None
