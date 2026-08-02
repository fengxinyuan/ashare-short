"""持仓判断：已持仓的票要回答的是**什么时候卖**，不是什么时候买。

同一只票，没持仓时盯的是「进入买入区间 → 止跌确认」，已持仓时盯的是四类离场
条件。两套判断的输入、阈值、紧急程度都不同，所以分开算。

**四类卖出触发，缺一不可：**

| 触发 | 说的是 | 为什么单独一类 |
|---|---|---|
| 止损 | 跌破你定的价位 | 底线，不解释 |
| 目标达成 | 到了预设目标 | 兑现，避免坐电梯 |
| **结构破坏** | 跌破当初买入依据的那个支撑 | **比止损更早**。支撑没了，持有的理由就没了，不该等价格走到止损位才反应 |
| **时间止损** | 买入 N 天没按预期反弹 | **低吸最大的隐性亏损来源**。价格没跌到止损、也没涨到目标，就那么耗着，占着仓位和注意力 |

系统只出建议，**不下单、不撤单、不接管账户**——持仓成本、股数、计划止损全部由
你手工维护（`watchlist hold`），这里只读不写。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

# 低吸的自然持有期是 2–5 个交易日（等回调结束 → 反弹到前高）。超过这个窗口还没
# 有像样的反弹，说明当初的判断没兑现——继续持有是在等一个已经没有依据的结果。
#
# 用自然日而不是交易日：差一两天不改变判断，而换算交易日要拖进交易日历，为一个
# 本来就模糊的阈值增加一处依赖不值得。
TIME_STOP_DAYS = 8

# 时间止损的收益门槛。到期时浮盈还不到这个数就算「没兑现」。不是要求盈利，只是
# 要求**有反应**——低吸买对了通常几天内就该看到反弹。
TIME_STOP_MIN_GAIN = 0.02

# 结构破坏的缓冲。日线支撑不是一条精确的线，盘中插一下就报警会制造噪声，所以要
# 跌破一段才算数。
STRUCTURE_BREAK_BUFFER = 0.005


@dataclass(frozen=True)
class HoldingView:
    """一笔持仓在当下的状态。全部字段都可能缺——缺了就不给对应的判断。"""

    symbol: str
    cost_price: float | None = None
    shares: int | None = None
    opened_on: str | None = None
    plan_stop: float | None = None
    plan_target: float | None = None
    held_days: int | None = None

    @classmethod
    def from_entry(cls, entry: Any, today: date | None = None) -> "HoldingView | None":
        """从 `WatchEntry` 构造。没持仓返回 None——调用方据此走买点那条路。"""
        if entry is None or not getattr(entry, "holding", False):
            return None
        return cls(
            symbol=entry.symbol,
            cost_price=entry.cost_price,
            shares=entry.shares,
            opened_on=entry.opened_on,
            plan_stop=entry.plan_stop,
            plan_target=entry.plan_target,
            held_days=_held_days(entry.opened_on, today),
        )

    def unrealised_pct(self, price: float | None) -> float | None:
        if not self.cost_price or not price:
            return None
        return round(float(price) / float(self.cost_price) - 1.0, 6)

    def describe(self, price: float | None) -> list[str]:
        """给告警正文用。持仓告警不带成本和浮盈，等于让你自己去翻账户。"""
        lines: list[str] = []
        if self.cost_price:
            gain = self.unrealised_pct(price)
            text = f"成本 {self.cost_price:.2f}"
            if gain is not None:
                text += f"，现价 {price:.2f}，浮动{'盈利' if gain >= 0 else '亏损'} {gain:+.2%}"
            if self.shares:
                text += f"，{self.shares} 股"
            lines.append(text)
        if self.held_days is not None:
            lines.append(f"已持有 {self.held_days} 天")
        return lines


def effective_stop(holding: HoldingView | None, computed: float | None) -> float | None:
    """你定的止损优先于系统算的。

    手工填的 `plan_stop` 是**决定**，系统算的是**建议**。两者冲突时听你的——
    你可能知道系统不知道的事（仓位、税、别处的敞口）。
    """
    if holding is not None and holding.plan_stop:
        return float(holding.plan_stop)
    return computed


def structure_broken(price: float | None, daily_support: float | None) -> bool:
    """跌破日线结构支撑。

    这比止损早一步：支撑是当初买入的**依据**，依据没了就该重新评估，而不是等
    价格再跌到止损位。加一段缓冲，避免盘中插针制造噪声。
    """
    if not price or not daily_support or daily_support <= 0:
        return False
    return price < daily_support * (1.0 - STRUCTURE_BREAK_BUFFER)


def time_stop_due(holding: HoldingView | None, price: float | None) -> bool:
    """持有到期且没有像样的反弹。

    注意它**不要求亏损**：不亏不赚地耗着同样是失败——占着仓位、占着注意力，而
    当初买入的理由（回调结束会反弹）已经被时间证伪了。
    """
    if holding is None or holding.held_days is None:
        return False
    if holding.held_days < TIME_STOP_DAYS:
        return False
    gain = holding.unrealised_pct(price)
    # 算不出浮盈（没填成本）时不触发：宁可不报，也不拿猜出来的数催人卖。
    return gain is not None and gain < TIME_STOP_MIN_GAIN


def _held_days(opened_on: Any, today: date | None) -> int | None:
    if not opened_on:
        return None
    try:
        opened = date.fromisoformat(str(opened_on)[:10])
    except (TypeError, ValueError):
        return None
    return max(0, ((today or date.today()) - opened).days)
