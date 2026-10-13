"""五档盘口的确定性读数，输入量纲为股和元。

挂单墙提供候选价位依据，挂单可撤销，单次快照不能证明持续承接。
封死涨跌停时委比退化为离散状态，由 is_degenerate 标明，不能按连续强弱解释。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 一档要算「墙」，得同时满足两条：占本方五档总量的份额够大，且明显高过同侧
# 第二大的那一档。只用份额会在只有两档有挂单时误报（各占 50% 很正常）；只用
# 倍数会在盘口很薄时误报（100 股 vs 40 股也是 2.5 倍）。
WALL_SHARE = 0.45
WALL_RATIO = 2.0

# 墙还得有绝对量级——挂 200 股的「墙」在任何票上都不是墙。按金额卡，因为
# 股数的量级在 3 元的票和 1350 元的票之间差两个数量级。
WALL_MIN_VALUE_YUAN = 200_000.0

# 但绝对金额也不够。实测 2026-08-02 收盘后的茅台：买一挂 5 手 = 68 万元，
# 过了绝对门槛、占本方五档 56%，于是被报成「买盘墙」——而 68 万在一只当日
# 成交 73.7 亿的票上什么都挡不住。

# 所以再卡一道**相对当日成交额**的比例。它自己会缩放：贵的票、活跃的票门槛
# 自动抬高；开盘头几分钟成交额还小，门槛也自动低下来——那时候一堵相对已成交
# 量很大的挂单，确实比收盘前同样金额的挂单更有意义。
WALL_MIN_TURNOVER_SHARE = 0.001

# 价差宽到这个程度，五档报出来的价位就不适合直接当挂单价：中间是空的，
# 成交会滑到别处去。
WIDE_SPREAD_PCT = 0.004


@dataclass(frozen=True)
class BookWall:
    """五档里明显厚过其他档的那一档。"""

    side: str  # bid | ask
    price: float
    shares: float
    value: float
    share_of_side: float

    def describe(self) -> str:
        label = "买盘墙" if self.side == "bid" else "卖盘墙"
        return (
            f"{label} {self.price:.2f}（{self.shares / 100:.0f} 手、"
            f"{self.value / 10000:.0f} 万元，占本方五档 {self.share_of_side:.0%}）"
        )


@dataclass(frozen=True)
class OrderBookReading:
    """一只票某一时刻的盘口读数。全部字段都是可直接引用的事实。"""

    symbol: str
    last_price: float
    as_of: str | None = None
    source: str | None = None

    best_bid: float | None = None
    best_ask: float | None = None
    spread_pct: float | None = None

    bid_shares: float = 0.0
    ask_shares: float = 0.0
    bid_value: float = 0.0
    ask_value: float = 0.0
    imbalance: float | None = None
    depth_levels: int = 0

    bid_wall: BookWall | None = None
    ask_wall: BookWall | None = None

    limit_up: float | None = None
    limit_down: float | None = None
    at_limit_up: bool | None = None
    at_limit_down: bool | None = None

    # 委比退化：封在涨跌停上，或者一侧五档全空。
    is_degenerate: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def is_tradable_now(self) -> bool:
        """现在挂单能不能指望成交。封死涨停买不进、封死跌停卖不出。"""
        if self.at_limit_up or self.at_limit_down:
            return False
        return bool(self.best_bid and self.best_ask)

    def describe(self) -> list[str]:
        """告警里能直接贴的盘口旁证，最多两行。**只陈述读数，不做方向判断。**

        写在这一层而不是 `notify/render.py`，是因为哪些字段在什么状态下**不该
        被印出来**属于读数本身的性质，不是排版偏好：封板时委比恒为 ±1.0，把它
        印进告警，读的人会把一个常数当成强度读数。

        封板那一行排在最前，因为它改变的不是「值不值得动」而是「动不动得了」。
        """
        lines: list[str] = []
        if self.at_limit_up and self.limit_up:
            lines.append(f"封涨停 {self.limit_up:.2f}：此刻买不进，挂单只能排队等开板")
        elif self.at_limit_down and self.limit_down:
            lines.append(f"封跌停 {self.limit_down:.2f}：此刻卖不出，封死就只能等次日")

        facts: list[str] = []
        if self.bid_wall is not None:
            facts.append(self.bid_wall.describe())
        if self.ask_wall is not None:
            facts.append(self.ask_wall.describe())
        if not self.is_degenerate and self.imbalance is not None:
            facts.append(f"五档委比 {self.imbalance:+.2f}")
        if facts:
            lines.append("盘口 " + "；".join(facts) + "（挂单可撤，只作旁证）")
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "last_price": self.last_price,
            "as_of": self.as_of,
            "source": self.source,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread_pct": self.spread_pct,
            "bid_shares": self.bid_shares,
            "ask_shares": self.ask_shares,
            "bid_value": self.bid_value,
            "ask_value": self.ask_value,
            "imbalance": self.imbalance,
            "depth_levels": self.depth_levels,
            "bid_wall": self.bid_wall.describe() if self.bid_wall else None,
            "bid_wall_price": self.bid_wall.price if self.bid_wall else None,
            "ask_wall": self.ask_wall.describe() if self.ask_wall else None,
            "ask_wall_price": self.ask_wall.price if self.ask_wall else None,
            "limit_up": self.limit_up,
            "limit_down": self.limit_down,
            "at_limit_up": self.at_limit_up,
            "at_limit_down": self.at_limit_down,
            "is_degenerate": self.is_degenerate,
            "notes": self.notes,
        }


def read_order_book(snapshot: Any) -> OrderBookReading | None:
    """把一份 `adapters.snapshot.Snapshot` 读成盘口事实。取不出就返回 None。"""
    last = getattr(snapshot, "last_price", None)
    if not last or last <= 0:
        return None

    bids = [level for level in (getattr(snapshot, "bids", None) or []) if level.price > 0]
    asks = [level for level in (getattr(snapshot, "asks", None) or []) if level.price > 0]

    notes: list[str] = []
    bid_shares = sum(level.volume for level in bids)
    ask_shares = sum(level.volume for level in asks)
    bid_value = sum(level.price * level.volume for level in bids)
    ask_value = sum(level.price * level.volume for level in asks)

    total = bid_shares + ask_shares
    imbalance = round((bid_shares - ask_shares) / total, 4) if total > 0 else None

    at_limit_up = getattr(snapshot, "at_limit_up", None)
    at_limit_down = getattr(snapshot, "at_limit_down", None)

    # 退化的三种来源分开记，因为处理方式不同：封板是「买不进/卖不出」，
    # 单边空档是「这一路数据不全」。
    degenerate = False
    if at_limit_up:
        degenerate = True
        notes.append("封涨停：卖盘五档为空，委比恒为 +1.0，不代表买盘强度")
    elif at_limit_down:
        degenerate = True
        notes.append("封跌停：买盘五档为空，委比恒为 -1.0，不代表卖盘强度")
    elif not bids or not asks:
        degenerate = True
        notes.append("五档单边为空，委比无意义")

    best_bid = bids[0].price if bids else None
    best_ask = asks[0].price if asks else None
    spread_pct = None
    if best_bid and best_ask and best_ask > best_bid:
        spread_pct = round((best_ask - best_bid) / last, 6)
        if spread_pct >= WIDE_SPREAD_PCT:
            notes.append(f"买一卖一价差 {spread_pct:.2%}，盘口偏薄，五档价位不宜直接当挂单价")

    bid_wall = _find_wall(bids, "bid", bid_shares, getattr(snapshot, "amount", None))
    ask_wall = _find_wall(asks, "ask", ask_shares, getattr(snapshot, "amount", None))
    if bid_wall or ask_wall:
        notes.append("挂单墙只说明该价位当前有人挂着，撤单成本为零，不作为方向证据")

    return OrderBookReading(
        symbol=getattr(snapshot, "symbol", ""),
        last_price=last,
        as_of=_isoformat(getattr(snapshot, "as_of", None)),
        source=getattr(snapshot, "source", None),
        best_bid=best_bid,
        best_ask=best_ask,
        spread_pct=spread_pct,
        bid_shares=bid_shares,
        ask_shares=ask_shares,
        bid_value=round(bid_value, 2),
        ask_value=round(ask_value, 2),
        imbalance=imbalance,
        depth_levels=max(len(bids), len(asks)),
        bid_wall=bid_wall,
        ask_wall=ask_wall,
        limit_up=getattr(snapshot, "limit_up", None),
        limit_down=getattr(snapshot, "limit_down", None),
        at_limit_up=at_limit_up,
        at_limit_down=at_limit_down,
        is_degenerate=degenerate,
        notes=notes,
    )


def read_many(snapshots: dict[str, Any]) -> dict[str, OrderBookReading]:
    readings: dict[str, OrderBookReading] = {}
    for symbol, snapshot in (snapshots or {}).items():
        reading = read_order_book(snapshot)
        if reading is not None:
            readings[symbol] = reading
    return readings


def _find_wall(
    levels: list, side: str, side_total: float, session_amount: float | None = None
) -> BookWall | None:
    if len(levels) < 2 or side_total <= 0:
        return None
    ordered = sorted(levels, key=lambda level: level.volume, reverse=True)
    top, runner_up = ordered[0], ordered[1]
    if top.volume <= 0 or runner_up.volume <= 0:
        return None

    share = top.volume / side_total
    if share < WALL_SHARE or top.volume < runner_up.volume * WALL_RATIO:
        return None

    value = top.price * top.volume
    floor = WALL_MIN_VALUE_YUAN
    if session_amount and session_amount > 0:
        # 取两道门槛里高的那个。成交额缺失时退回绝对值，而不是放行——
        # 「没测到」不该变成「通过」。
        floor = max(floor, session_amount * WALL_MIN_TURNOVER_SHARE)
    if value < floor:
        return None

    return BookWall(
        side=side,
        price=round(top.price, 3),
        shares=top.volume,
        value=round(value, 2),
        share_of_side=round(share, 4),
    )


def _isoformat(value: Any) -> str | None:
    try:
        return value.isoformat(timespec="seconds")
    except (AttributeError, TypeError, ValueError):
        return None
