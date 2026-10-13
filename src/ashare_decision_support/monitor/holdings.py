"""持仓判断：已持仓的票要回答的是**什么时候卖**，不是什么时候买。

同一只票，没持仓时盯的是「进入买入区间 → 止跌确认」，已持仓时盯的是五类离场
条件。两套判断的输入、阈值、紧急程度都不同，所以分开算。

**五类卖出触发，缺一不可：**

| 触发 | 说的是 | 为什么单独一类 |
|---|---|---|
| 止损 | 跌破你定的价位 | 底线，不解释 |
| 目标达成 | 到了预设目标 | 兑现，避免坐电梯 |
| **结构破坏** | 跌破当初买入依据的那个支撑 | **比止损更早**。支撑没了，持有的理由就没了，不该等价格走到止损位才反应 |
| **时间止损** | 买入 N 天没按预期反弹 | **低吸最大的隐性亏损来源**。价格没跌到止损、也没涨到目标，就那么耗着，占着仓位和注意力 |
| **浮盈回吐** | 冲高之后又跌回来 | 上面四条**没有一条会响**：止损远在下方、目标没到、支撑没破、期限没到。而这正是「坐电梯」的全过程 |

系统只出建议，**不下单、不撤单、不接管账户**——持仓成本、股数、计划止损与目标全部由
你手工维护（`ashare-ds position open`），这里只读不写。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.signals.breakeven import measure_breakeven

# 浮动盈亏的公式只有一份，在持有成本价的那一层。`HoldingView` 是 `state.Position`
# 的投影，这个数在两边必须逐位相同——所以取的是同一个函数，不是同一段代码。
from ashare_decision_support.state.positions import unrealised_pct as position_gain

# 低吸的自然持有期是 2–5 个交易日（等回调结束 → 反弹到前高）。超过这个窗口还没
# 有像样的反弹，说明当初的判断没兑现——继续持有是在等一个已经没有依据的结果。

# 用自然日而不是交易日：差一两天不改变判断，而换算交易日要拖进交易日历，为一个
# 本来就模糊的阈值增加一处依赖不值得。
TIME_STOP_DAYS = 8

# 时间止损的收益门槛。到期时浮盈还不到这个数就算「没兑现」。不是要求盈利，只是
# 要求**有反应**——低吸买对了通常几天内就该看到反弹。
TIME_STOP_MIN_GAIN = 0.02

# 结构破坏的缓冲。日线支撑不是一条精确的线，盘中插一下就报警会制造噪声，所以要
# 跌破一段才算数。
STRUCTURE_BREAK_BUFFER = 0.005

# 浮盈回吐（移动止损）

# 两条同时成立才算「坐电梯」，缺一不可：

# 1. **回吐量本身超出这只票的日常波动**（≥ 1 个日均真实波幅）。只用比例的话，
#    一只日振幅 6% 的票每天都在「回吐一半浮盈」，告警会退化成噪声。
# 2. **且吐掉了峰值浮盈的一半以上**。只用绝对回撤的话，一笔浮盈 30% 的持仓回落
#    1 个日波幅就报警——那是正常回踩，不是坐电梯。

# 单位取标的自身的 ATR 而不是固定百分比，理由和 `signals/dipbuy.py` 的档位、
# `signals/daily_technical.py` 的止损距离完全一样：绝对百分比在长江电力
# （日振幅 1.82%）和计算机题材（6.23%）上含义差三倍。
GIVEBACK_MIN_ATR = 1.0
GIVEBACK_RATIO = 0.5


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
    def from_position(cls, entry: Any, today: date | None = None) -> HoldingView:
        """从 `state.Position` 构造。

        以前这里还要判一次「这条记录到底持没持仓」——自选股和持仓混在同一份
        清单里，靠一个 `holding` 布尔位区分。持仓表拆出来之后那个分支消失了：
        表里的每一条按定义都是持仓，没有第二种可能。
        """
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
        return position_gain(self.cost_price, price)

    @property
    def t_plus_one_locked(self) -> bool:
        """今天建的仓今天卖不出——A 股 T+1。

        建仓日未登记时 `held_days` 是 None，这里返回 False：不知道不等于锁定，
        但也不等于可卖——那种情况下告警照常发，只是不加这句提示。
        """
        return self.held_days == 0

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


def effective_targets(holding: HoldingView | None, computed: list[float] | None) -> list[float]:
    """手工目标优先；指定时替代系统目标阶梯。"""
    if holding is not None and holding.plan_target:
        return [float(holding.plan_target)]
    return list(computed or [])


def t_plus_one_lines(holding: HoldingView | None) -> list[str]:
    """卖出告警末尾那一句：今日建仓的持仓，触发了也只能明天处理。

    止损、结构破坏、浮盈回吐、触及目标四条都要求一个卖出动作，而建仓当天这个
    动作做不成。不加这句，一条 ACTION 级的「跌破止损」会催人去做一件此刻做不到
    的事；加了这句，它变成明日开盘的处置提示——若明早开盘仍满足条件，按开盘价
    处理，不是按今天报出来的这个价。
    """
    if holding is None or not holding.t_plus_one_locked:
        return []
    return [
        (
            "T+1：今日建仓，今天卖不出。这条是明日开盘的处置提示——"
            "若明早开盘仍满足该条件，按开盘价处理，不是按现在这个价位"
        )
    ]


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


@dataclass(frozen=True)
class Giveback:
    """浮盈从峰值回吐了多少。`due=False` 时其余字段照样有值，供告警正文引用。"""

    due: bool
    peak_price: float
    price: float
    peak_gain_pct: float
    current_gain_pct: float
    given_back_ratio: float  # 吐掉了峰值浮盈的几成
    given_back_atr: float  # 回吐幅度折成几个日波幅

    def describe(self) -> list[str]:
        return [
            (
                f"持仓期最高 {self.peak_price:.2f}（浮盈 {self.peak_gain_pct:+.2%}），"
                f"现价 {self.price:.2f}（{self.current_gain_pct:+.2%}）"
            ),
            (
                f"已回吐峰值浮盈的 {self.given_back_ratio:.0%}，"
                f"回吐幅度 {self.given_back_atr:.1f} 个日波幅"
            ),
        ]


def assess_giveback(
    holding: HoldingView | None,
    price: float | None,
    peak_price: float | None,
    atr: float | None,
) -> Giveback | None:
    """检查持仓浮盈回吐；成本、ATR 或峰值未知时返回 None。"""
    if holding is None or not holding.cost_price or not price or not peak_price:
        return None
    # 没有波幅单位就不判。固定百分比在不同波动的票上含义差几倍，宁可不报。
    if not atr or atr <= 0:
        return None

    cost = float(holding.cost_price)
    peak_gain = float(peak_price) - cost
    current_gain = float(price) - cost
    # 从来没有过浮盈的持仓不归这里管——那是止损与结构破坏的事。
    if peak_gain <= 0:
        return None

    given_back = float(peak_price) - float(price)
    if given_back <= 0:
        return None

    return Giveback(
        due=(
            given_back >= GIVEBACK_MIN_ATR * float(atr)
            and current_gain <= peak_gain * (1.0 - GIVEBACK_RATIO)
        ),
        peak_price=float(peak_price),
        price=float(price),
        peak_gain_pct=peak_gain / cost,
        current_gain_pct=current_gain / cost,
        given_back_ratio=given_back / peak_gain,
        given_back_atr=given_back / float(atr),
    )


def _held_days(opened_on: Any, today: date | None) -> int | None:
    if not opened_on:
        return None
    try:
        opened = date.fromisoformat(str(opened_on)[:10])
    except (TypeError, ValueError):
        return None
    return max(0, ((today or beijing_today()) - opened).days)


def breakeven_lines(cost: float | None, price: float | None, daily: Any) -> list[str]:
    """把成本价与日线压力交叉，持仓告警与单票分析共用。"""
    if daily is None:
        return []
    levels = {
        "MA5": getattr(daily, "ma5", None),
        "MA10": getattr(daily, "ma10", None),
        "MA20": getattr(daily, "ma20", None),
        "MA60": getattr(daily, "ma60", None),
        **{
            str(getattr(level, "label", "压力位")): getattr(level, "price", None)
            for level in (getattr(daily, "resistances", None) or [])
        },
    }
    # 第一行重复了 HoldingView 的成本/浮盈信息，告警里只保留结构结论。
    return measure_breakeven(cost, price, levels).describe()[1:]
