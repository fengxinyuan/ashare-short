"""回本价与阻力带的重合检测：解套要穿过几层。

## 它回答的问题

持仓者在浮亏里真正要问的不是「跌了多少」，是**「回本要穿过什么」**。系统里
均线、箱体边界、前高这些位都算得出来，持仓成本也记在持仓表里，但两者
从来不交叉——于是「浮亏 2.8%」这个数被当成了全部信息。

2026-08-04 牧原股份实测：持仓成本 39.12，而当日 MA5 39.00 / MA20 39.01 /
MA10 39.15。**回本价正好落在三线粘合带里。**这意味着解套价和均线压力是同一个
价——市场必须同时消化掉均线压力和箱体里的套牢盘才能让人回本。这是全场最难啃的
位置，而系统当时一个字都没提。

## 口径

- **只出结构，不出动作。** 「回本价上方叠了三层压力」是事实，「所以该割」是判断。
- 阻力只算**现价与回本价之间**的那些。回本价上方的位再多也和这次解套无关。
- 重合带宽 `CLUSTER_BAND` 是先验设定，不是标定出来的；它只让「粘合」这个词有个
  确定判据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 多个位落在回本价这个相对带宽内，算「重合」。1% 是超短里一档常见的噪声宽度。
CLUSTER_BAND = 0.01

# 上方压力层数达到这个数，就单独说一句。三层是经验刻度，先验设定。
STACKED_LEVELS = 3


@dataclass
class BreakevenRead:
    """回本价相对现价与阻力位的结构。浮盈时 `underwater=False`，读数依然给。"""

    cost: float | None = None
    last_price: float | None = None
    unrealised_pct: float | None = None
    underwater: bool = False
    gap_to_breakeven_pct: float | None = None
    levels_between: list[tuple[str, float]] = field(default_factory=list)
    cluster_at_cost: list[tuple[str, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cost": self.cost,
            "last_price": self.last_price,
            "unrealised_pct": self.unrealised_pct,
            "underwater": self.underwater,
            "gap_to_breakeven_pct": self.gap_to_breakeven_pct,
            "levels_between": [list(item) for item in self.levels_between],
            "cluster_at_cost": [list(item) for item in self.cluster_at_cost],
            "notes": list(self.notes),
        }

    def describe(self) -> list[str]:
        if self.cost is None or self.last_price is None:
            return list(self.notes)
        lines = [f"成本 {self.cost:.2f}，现价 {self.last_price:.2f}，浮动 {self.unrealised_pct:+.2%}"]
        if not self.underwater:
            lines.append("当前浮盈，回本价已在下方")
            lines.extend(self.notes)
            return lines

        lines.append(f"回本还需 {self.gap_to_breakeven_pct:+.2%}")
        if self.levels_between:
            joined = "、".join(f"{name} {value:.2f}" for name, value in self.levels_between)
            lines.append(f"现价到回本之间叠着 {len(self.levels_between)} 层压力：{joined}")
            if len(self.levels_between) >= STACKED_LEVELS:
                lines.append("压力层数已达密集区间，向上每一档都要重新消化一次供给")
        else:
            lines.append("现价到回本之间没有识别到阻力位")

        if self.cluster_at_cost:
            joined = "、".join(f"{name} {value:.2f}" for name, value in self.cluster_at_cost)
            lines.append(
                f"**回本价与 {joined} 重合**（{CLUSTER_BAND:.0%} 带内）："
                "解套价和压力位是同一个价，这是最难啃的位置"
            )
        lines.extend(self.notes)
        return lines


def measure_breakeven(
    cost: float | None,
    last_price: float | None,
    levels: dict[str, float | None] | None = None,
) -> BreakevenRead:
    """回本价相对阻力位的结构。

    `levels` 是命名价位，例如 `{"MA5": 39.0, "MA20": 39.01, "箱体下沿": 38.42}`。
    值为 None 的项直接跳过——缺一个位不该让整层弃权。
    """
    read = BreakevenRead(cost=cost, last_price=last_price)
    if not cost or not last_price or cost <= 0 or last_price <= 0:
        read.cost = None
        read.last_price = None
        read.notes.append("缺成本价或现价，本次不出回本结构")
        return read

    read.unrealised_pct = round(last_price / cost - 1.0, 6)
    read.underwater = last_price < cost
    if not read.underwater:
        return read

    read.gap_to_breakeven_pct = round(cost / last_price - 1.0, 6)

    usable = [
        (name, float(value))
        for name, value in (levels or {}).items()
        if value is not None and float(value) > 0
    ]
    # 现价与回本价之间的那些位才算数。回本价上方的位再多，也和这次解套无关。
    read.levels_between = sorted(
        [(name, value) for name, value in usable if last_price < value <= cost],
        key=lambda item: item[1],
    )
    read.cluster_at_cost = sorted(
        [(name, value) for name, value in usable if abs(value / cost - 1.0) <= CLUSTER_BAND],
        key=lambda item: item[1],
    )
    return read
