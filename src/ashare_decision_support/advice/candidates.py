"""L2 候选层：规则生成**一组带出处的价位**，而不是一个答案。

原来的 `advice/engine.py` 直接算出一组 entry/stop/target 就完事。它有两个毛病：

- **没有出处。** 报出来的 22.95 是从分时均价来的、从日线支撑来的、还是从买盘墙
  来的，事后谁也说不清。台账里存的只是一个数，兑现核对能回答「准不准」，回答
  不了「哪一类依据准」。
- **没有取舍空间。** 只有一个答案，模型要么原样复述，要么改数——而改数是被
  数字闸门禁止的。结果是 LLM 在买卖点上完全没有位置，`SOLUTION.md` 里写的
  「保留自主能力」落不了地。

所以 L2 改成产出**候选集**：每个候选带 `basis`（这个价从哪来）、`evidence`
（支撑它的 L1 读数）、`default_rank`（规则自己的排序）。

三层的分工因此是清楚的：

```text
L1  只出数字，不做取舍          →  分时均价 22.90、日线支撑 22.31、买盘墙 22.80
L2  把数字变成带出处的候选      →  entry 候选三个，规则默认选 rank 0
L3  只在候选里挑，不造新数      →  「今天选 22.80，因为买盘墙在那里」或者弃权
```

**L3 造不出新价位**，因为它能选的东西是一个有限集合。这不是靠提示词约束的，
是靠候选集本身是枚举的——`adjudicate.py` 的闸门做的是**精确相等**比对，不是
「看起来像」。

规则仍然给 `default_rank`：LLM 挂了、越界了、或者根本没配 key，取 rank 0 就是
原来那套行为，链路照跑。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

SIDE_ENTRY = "entry"
SIDE_STOP = "stop"
SIDE_TARGET = "target"

# 出处标签。写成常量是因为它们会进台账，将来要按 basis 分组统计
# 「哪一类依据的兑现率高」——字符串写错一个字，那一组样本就凭空消失。
BASIS_VWAP = "分时均价回踩"
BASIS_SESSION_LOW = "日内低点"
BASIS_SESSION_HIGH = "日内高点"
BASIS_DAILY_SUPPORT = "日线支撑"
BASIS_DAILY_RESISTANCE = "日线压力"
BASIS_BID_WALL = "五档买盘墙"
BASIS_ASK_WALL = "五档卖盘墙"
BASIS_BREAKOUT_TRIGGER = "突破触发价"
BASIS_LIMIT_DOWN = "跌停价"
BASIS_STRUCTURE_BREAK = "结构破位"
BASIS_NOISE_FLOOR = "日内波动下沿"

# 候选价距现价超过这个比例就不再是「今天的价位」，是下周的愿望。
#
# **这两个上限不适用于规则自己的默认值。** 规则算出来的那个价必须无条件进候选
# 集：`engine.py` 报的 `advice.stop` 就是它，一旦被距离过滤器筛掉，候选集里就
# 没有引擎正在报的那个价——L3 的闸门会把引擎自己的数判成越界，台账里记的
# `rule_stop` 也会和 `stop_price` 对不上。护栏是给**额外**候选用的。
MAX_ENTRY_DISTANCE = 0.06
# 突破是唯一允许高于现价入场的打法。触发价过远，盘中结构已经变了，不该把
# "等涨回来"伪装成今天可执行的突破单。
MAX_BREAKOUT_DISTANCE = 0.03
MAX_STOP_DISTANCE = 0.09


@dataclass(frozen=True)
class PriceCandidate:
    """一个候选价位，以及它凭什么是这个价。

    `evidence` 存的是**支撑这个价的 L1 读数**，不是解释文本。它有两个用途：
    喂给 L3 让它有东西可引用；进台账让事后能回答「当时依据的读数是什么」。
    """

    side: str
    price: float
    basis: str
    default_rank: int
    evidence: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        """候选的稳定标识。L3 用它来指认自己选了哪个，而不是重报一遍价格。"""
        return f"{self.side}:{self.basis}:{self.price:.3f}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key(),
            "side": self.side,
            "price": self.price,
            "basis": self.basis,
            "default_rank": self.default_rank,
            "evidence": self.evidence,
        }


@dataclass
class CandidateSet:
    """一只票的全部候选，按 side 分组。"""

    symbol: str
    entries: list[PriceCandidate] = field(default_factory=list)
    stops: list[PriceCandidate] = field(default_factory=list)
    targets: list[PriceCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def all(self) -> list[PriceCandidate]:
        return [*self.entries, *self.stops, *self.targets]

    def prices(self) -> set[float]:
        """闸门用的白名单：L3 只能报出这里面的数。"""
        return {round(item.price, 3) for item in self.all()}

    def by_key(self, key: str) -> PriceCandidate | None:
        return next((item for item in self.all() if item.key() == key), None)

    def default(self, side: str) -> PriceCandidate | None:
        """规则自己的选择。LLM 不可用时走的就是这条路。"""
        pool = {SIDE_ENTRY: self.entries, SIDE_STOP: self.stops, SIDE_TARGET: self.targets}[side]
        return pool[0] if pool else None

    @property
    def is_empty(self) -> bool:
        return not (self.entries or self.stops or self.targets)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "entries": [item.to_dict() for item in self.entries],
            "stops": [item.to_dict() for item in self.stops],
            "targets": [item.to_dict() for item in self.targets],
            "notes": self.notes,
        }


def build_candidates(
    symbol: str,
    last_price: float,
    vwap: float | None,
    session_low: float,
    session_high: float,
    span: float,
    daily_supports: list[float] | None = None,
    daily_resistances: list[float] | None = None,
    book: Any = None,
    entry_band: float | None = None,
    breakout_trigger: float | None = None,
    rule_stop: float | None = None,
    rule_stop_basis: str = BASIS_NOISE_FLOOR,
) -> CandidateSet:
    """从 L1 读数生成候选集。

    `entry_band` / `rule_stop` 是 `engine.py` 已经算好的那两个数，作为
    rank 0 传进来——**候选集的默认答案必须和改造前逐位相同**，否则台账里
    改造前后的记录就不可比，而那正是这套持续学习机制唯一的资产。
    """
    result = CandidateSet(symbol=symbol)
    if not last_price or last_price <= 0:
        result.notes.append("无现价，无法生成任何候选")
        return result

    _entry_candidates(
        result,
        last_price,
        vwap,
        session_low,
        session_high,
        span,
        daily_supports,
        book,
        entry_band,
        breakout_trigger,
    )
    _stop_candidates(
        result, last_price, session_low, span, daily_supports, book, rule_stop, rule_stop_basis
    )
    _target_candidates(result, last_price, session_high, daily_resistances, book)
    return result


# ---- entry ---------------------------------------------------------------


def _entry_candidates(
    result: CandidateSet,
    last: float,
    vwap: float | None,
    low: float,
    high: float,
    span: float,
    supports: list[float] | None,
    book: Any,
    entry_band: float | None,
    breakout_trigger: float | None,
) -> None:
    seen: set[float] = set()

    def add(price: float | None, basis: str, evidence: dict, mandatory: bool = False) -> None:
        if price is None or price <= 0:
            return
        price = round(price, 3)
        if price in seen:
            return
        if not mandatory:
            # 高于现价的「买入价」不是买入价，是追价——低吸口径下直接不生成。
            if price > last:
                return
            if (last - price) / last > MAX_ENTRY_DISTANCE:
                return
        seen.add(price)
        result.entries.append(
            PriceCandidate(
                side=SIDE_ENTRY,
                price=price,
                basis=basis,
                default_rank=len(result.entries),
                evidence=evidence,
            )
        )

    if breakout_trigger is not None:
        _breakout_entry(result, add, last, high, breakout_trigger)

    # 非突破默认值是分时均价回踩带的下沿。突破触发价已存在时，它降为备选，
    # 保留给 L3 在候选集内选择更保守的执行方案。
    add(
        entry_band,
        BASIS_VWAP,
        {"vwap": vwap, "session_low": low, "session_high": high, "band_span": round(span, 3)},
        mandatory=breakout_trigger is None,
    )
    if breakout_trigger is None and entry_band is not None and entry_band > last:
        # 规则的买入带跑到现价上方，只有一种成因：**价格已经跌穿了回踩带**。
        # 这个数照发（它是引擎正在报的那个），但它此刻的含义不是「跌到这里买」，
        # 是「已经跌过头了」——不说明的话读起来像一个正常的低吸位。
        result.notes.append(
            f"规则买入带 {round(entry_band, 3)} 高于现价 {round(last, 3)}："
            "价格已跌穿回踩区间，这不是低吸位而是失守位"
        )

    # 五档买盘墙：**唯一一个「此刻真的有人在这个价挂着」的候选**。其余候选都是
    # 从历史推出来的位置，只有这个是当下的事实。但它也最容易消失——撤单是免费的。
    wall = getattr(book, "bid_wall", None) if book is not None else None
    if wall is not None:
        add(
            wall.price,
            BASIS_BID_WALL,
            {
                "shares": wall.shares,
                "value": wall.value,
                "share_of_side": wall.share_of_side,
                "imbalance": getattr(book, "imbalance", None),
                "caveat": "挂单可撤，不构成承接证据",
            },
        )

    # 买一价：能立刻成交的那个价。它不是「好价位」，它是「现在的价位」——
    # 留在候选里是因为「就按现价买」本身是一个正当的取舍。
    best_bid = getattr(book, "best_bid", None) if book is not None else None
    if best_bid:
        add(best_bid, "买一价", {"spread_pct": getattr(book, "spread_pct", None)})

    # 日内低点：今天已经被验证过一次的位置。
    add(low, BASIS_SESSION_LOW, {"session_low": low, "session_high": high})

    # 日线支撑：现价下方最近的一档。
    below = sorted((level for level in (supports or []) if level < last), reverse=True)
    if below:
        add(below[0], BASIS_DAILY_SUPPORT, {"level": round(below[0], 3), "session_low": low})

    if not result.entries:
        result.notes.append("现价下方 6% 内没有任何有依据的买入位，属于无参照的追高位置")


def _breakout_entry(
    result: CandidateSet,
    add: Callable[..., None],
    last: float,
    high: float,
    breakout_trigger: float,
) -> None:
    """突破打法的那一个候选：站上触发价才买，太远就不买。

    单独一段，是因为它是买入侧唯一**允许高于现价**的口径——其余候选全部落在
    现价下方的低吸带里，共用 `add` 里那条「高于现价的买入价不是买入价，是追价」
    的默认闸门。突破是那条闸门的唯一例外（`mandatory=True` 放行），而例外和它
    自己的距离上限（`MAX_BREAKOUT_DISTANCE`，比低吸的 6% 严一半）必须挨在一起
    读——拆开之后，「哪条规则在放行它」在两个地方各写了一半。
    """
    distance = (breakout_trigger - last) / last
    if not (0 <= distance <= MAX_BREAKOUT_DISTANCE):
        result.notes.append(
            f"突破触发价 {round(breakout_trigger, 3)} 距现价 {distance:.1%}，"
            f"超过 {MAX_BREAKOUT_DISTANCE:.0%} 执行上限，放弃追价"
        )
        return
    # 突破打法的默认值必须是确认价，不是回踩带。它仍是一个受限候选，
    # L3 可以明确改选更保守的低吸候选，但不能另造追价。
    add(
        breakout_trigger,
        BASIS_BREAKOUT_TRIGGER,
        {
            "trigger": round(breakout_trigger, 3),
            "session_high": high,
            "distance_pct": round(distance, 4),
            "caveat": "仅在放量站上该价后执行；未触发前不追价",
        },
        mandatory=True,
    )


# ---- stop ----------------------------------------------------------------


def _stop_candidates(
    result: CandidateSet,
    last: float,
    low: float,
    span: float,
    supports: list[float] | None,
    book: Any,
    rule_stop: float | None,
    rule_stop_basis: str,
) -> None:
    seen: set[float] = set()

    def add(price: float | None, basis: str, evidence: dict, mandatory: bool = False) -> None:
        if price is None or price <= 0 or price >= last:
            return
        price = round(price, 3)
        if price in seen:
            return
        if not mandatory and (last - price) / last > MAX_STOP_DISTANCE:
            return
        seen.add(price)
        result.stops.append(
            PriceCandidate(
                side=SIDE_STOP,
                price=price,
                basis=basis,
                default_rank=len(result.stops),
                evidence=evidence,
            )
        )

    # rank 0 —— 规则默认，和改造前一致。同样无条件进集合。
    add(rule_stop, rule_stop_basis, {"session_low": low, "span": round(span, 3)}, mandatory=True)

    below = sorted((level for level in (supports or []) if level < low), reverse=True)
    if below:
        add(
            below[0],
            BASIS_STRUCTURE_BREAK,
            {"level": round(below[0], 3), "session_low": low, "meaning": "跌破即日线结构破坏"},
        )

    limit_down = getattr(book, "limit_down", None) if book is not None else None
    if limit_down:
        # 跌停价当止损没有操作意义（跌停就卖不掉），但它是**风险的绝对下界**，
        # 让 L3 在算最坏情况时有一个可引用的数，而不是自己编一个。
        add(
            limit_down,
            BASIS_LIMIT_DOWN,
            {"meaning": "当日最大下行边界；封死时无法成交，不可作为可执行止损"},
        )

    if not result.stops:
        result.notes.append("现价下方 9% 内没有可用止损位")


# ---- target --------------------------------------------------------------


def _target_candidates(
    result: CandidateSet,
    last: float,
    high: float,
    resistances: list[float] | None,
    book: Any,
) -> None:
    seen: set[float] = set()

    def add(price: float | None, basis: str, evidence: dict) -> None:
        if price is None or price <= last:
            return
        price = round(price, 3)
        if price in seen:
            return
        seen.add(price)
        result.targets.append(
            PriceCandidate(
                side=SIDE_TARGET,
                price=price,
                basis=basis,
                default_rank=len(result.targets),
                evidence=evidence,
            )
        )

    add(high, BASIS_SESSION_HIGH, {"session_high": high})

    wall = getattr(book, "ask_wall", None) if book is not None else None
    if wall is not None:
        add(
            wall.price,
            BASIS_ASK_WALL,
            {
                "shares": wall.shares,
                "value": wall.value,
                "share_of_side": wall.share_of_side,
                "caveat": "挂单可撤，不构成压力证据",
            },
        )

    for level in sorted(item for item in (resistances or []) if item > last)[:2]:
        add(level, BASIS_DAILY_RESISTANCE, {"level": round(level, 3)})

    if not result.targets:
        result.notes.append("上方无任何参照位，给不出目标价")
