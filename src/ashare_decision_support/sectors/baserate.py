"""条件基准率：**「过去出现同样条件时，这个板块后来怎么走」**。

这一层补的是整套系统里最要命的一个空档。板块层能算出「今天电子排第一」，舆情层
能识别出「费半跌了 2%」，宏观层能连出「韩股存储 → A 股半导体」这条链——但**没有
任何一处回答过这些链条在 A 股历史上到底兑不兑现**。于是所有映射都只是措辞：说得
通，查不了。

而 A 股恰恰是那种「说得通」和「真会涨」相关性很弱的市场。有政策、有业绩、外盘
大涨，第二天照样能低开走绿。所以这一层的产物**不是预测，是基准率**——一个可以
查证的历史事实：同样条件下，过去 N 次里涨了几次、超额中位数是多少。

## 三条口径（都影响结论，改之前先读）

1. **超额，不是绝对收益。** 前向收益一律减掉**当日全部板块的横截面中位数**。
   不减的话，一轮普涨里随便挑哪个板块的「胜率」都是 80%，而那衡量的是大盘，
   不是这条映射。基准率只用于研究上下文，不进入板块评分或交易信号。

2. **反应日与前向分开报。** `reaction` 是条件出现后 A 股**第一个能反应的交易日
   自身**的涨跌；`horizons` 是从那一天收盘再往后 1/3/5 日。两者答的是不同问题，
   而它们经常反号——那正是「一日游」的定量长相，也正是电风扇行情里最该看见的
   东西。合成一个数会把这两件事抵消掉。

3. **样本数与独立事件数并列。** 前向窗口互相重叠，相邻交易日又属于同一轮行情，
   所以样本数**不是**独立事件数。两个都报，`trusted` 要求两个都过线。胜率不能
   当概率读，`BaseRate.notes` 里每次都会写这句话。

## 这一层不做的事

- **不给建议、不给权重、不产出订单。** 它只回答「历史上是这样」。
- **不做参数搜索。** 窗口取 1/3/5 是因为主链路的持有期就是超短 1–3 日，换一组
  数字再挑最好看的那组，得到的是过拟合，不是发现。
- **样本不足时不外推。** `trusted=False` 的基准率照样报出来（藏起来只会让人
  误读那个数字），但驱动层不许拿它加权——见 `sectors/drivers.py`。
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from ashare_decision_support.numeric import finite, median_of

# 前向窗口。1/3/5 对应主链路的超短持有期；再长的窗口不是这套系统的口径，
# 见模块 docstring 第三条「不做参数搜索」。
DEFAULT_HORIZONS = (1, 3, 5)

# 一档基准率至少要这么多样本才允许被下游加权。与 `journal/stats.py:MIN_BUCKET`
# 取同一个数：两处问的是同一类问题（这一档的统计量可不可信），门槛不一致会让
# 「台账说样本够」和「基准率说样本不够」在同一天同时成立。
MIN_SAMPLES = 20

# 独立事件数下限。前向窗口重叠 + 相邻交易日同属一轮行情，所以 60 个样本可能只是
# 8 轮行情。这道门槛防的正是「攒够了足够多**相关的**记录」冒充「见过足够多
# **不同的**行情」——同 `journal/stats.py:MODEL_READY_SESSIONS` 那条纪律。
MIN_INDEPENDENT_EVENTS = 8

SAMPLE_CAVEAT = "前向窗口互相重叠，样本数不是独立事件数；胜率是历史频率，不能当概率读"


@dataclass(frozen=True)
class HorizonStat:
    """一个前向窗口上的统计量。`None` 一律表示这一档没有可用样本。"""

    horizon: int
    samples: int
    median_excess: float | None = None
    median_absolute: float | None = None
    win_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon,
            "samples": self.samples,
            "median_excess": self.median_excess,
            "median_absolute": self.median_absolute,
            "win_rate": self.win_rate,
        }


@dataclass(frozen=True)
class BaseRate:
    """一个板块在一个条件下的历史基准率。"""

    sector: str
    condition: str
    samples: int
    independent_events: int
    span: tuple[str, str] | None = None
    # 条件出现后第一个能反应的交易日**自身**的涨跌。horizon 固定为 0。
    reaction: HorizonStat | None = None
    horizons: tuple[HorizonStat, ...] = ()
    trusted: bool = False
    notes: tuple[str, ...] = ()

    def horizon(self, days: int) -> HorizonStat | None:
        return next((item for item in self.horizons if item.horizon == days), None)

    @property
    def headline(self) -> str:
        """一句话摘要。样本不足时**先说样本不足**，再说数字。"""
        primary = self.horizon(1)
        if primary is None or primary.median_excess is None:
            return f"{self.sector}｜{self.condition}：{self.samples} 个样本，无可用前向读数"
        prefix = "" if self.trusted else "⚠️ 样本不足，仅供观察："
        reaction = (
            f"反应日 {self.reaction.median_absolute:+.2%}、"
            if self.reaction is not None and self.reaction.median_absolute is not None
            else ""
        )
        return (
            f"{prefix}{self.sector}｜{self.condition}：{self.samples} 个样本"
            f"（{self.independent_events} 轮独立行情），{reaction}"
            f"其后 1 日超额中位 {primary.median_excess:+.2%}、胜率 {primary.win_rate:.0%}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sector": self.sector,
            "condition": self.condition,
            "samples": self.samples,
            "independent_events": self.independent_events,
            "span": list(self.span) if self.span else None,
            "reaction": self.reaction.to_dict() if self.reaction is not None else None,
            "horizons": [item.to_dict() for item in self.horizons],
            "trusted": self.trusted,
            "headline": self.headline,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class SectorPanel:
    """全部板块的收盘序列。**算一次，所有条件复用。**

    只存收盘和位置表，前向收益按需算：一次基准率查询只碰到几百个交易日，而全量
    预计算是 90 板块 × 4600 日 × 3 窗口。前者是几万次浮点运算，后者要在内存里
    摆一百多兆——而且绝大部分永远用不上。
    """

    closes: dict[str, tuple[tuple[str, float], ...]] = field(default_factory=dict)
    index: dict[str, dict[str, int]] = field(default_factory=dict)
    trading_days: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def sectors(self) -> tuple[str, ...]:
        return tuple(self.closes)

    @property
    def ok(self) -> bool:
        return bool(self.closes) and bool(self.trading_days)

    def span(self) -> tuple[str, str] | None:
        return (self.trading_days[0], self.trading_days[-1]) if self.trading_days else None

    def forward(self, sector: str, session: str, horizon: int) -> float | None:
        """从 `session` 收盘买入、持有 `horizon` 个交易日的收益。

        用**该板块自己的**序列位置数交易日，不用全场交易日表：板块指数有各自的
        起始日（实测白酒等 16 个板块起于 2018-11-26），拿全场位置去索引会在缺口
        处错位，而错位之后算出来的仍是一个合法的数。
        """
        position = self.index.get(sector, {}).get(session)
        rows = self.closes.get(sector)
        if position is None or rows is None:
            return None
        target = position + horizon
        if target < 0 or target >= len(rows):
            return None
        base = rows[position][1]
        return rows[target][1] / base - 1.0 if base > 0 else None


def build_panel(series: dict[str, tuple[str, list[dict]]]) -> SectorPanel:
    """`{code: (板块名, 日线)}` -> 面板。键按**板块名**，因为映射表按名字写。

    重名的板块只留第一个并记一条 note：同名两条序列会让「电子」这个查询在两份
    数据之间不确定地取一份，而那种不确定性事后查不出来。
    """
    closes: dict[str, tuple[tuple[str, float], ...]] = {}
    index: dict[str, dict[str, int]] = {}
    days: set[str] = set()
    notes: list[str] = []

    for code, (name, bars) in sorted(series.items()):
        label = str(name or code).strip() or code
        if label in closes:
            notes.append(f"板块名重复，已跳过 {code}：{label}")
            continue
        rows = tuple(
            (str(bar["date"]), value)
            for bar in bars
            if bar.get("date") and (value := finite(bar.get("close"))) is not None and value > 0
        )
        if len(rows) < 2:
            continue
        closes[label] = rows
        index[label] = {stamp: position for position, (stamp, _) in enumerate(rows)}
        days.update(stamp for stamp, _ in rows)

    return SectorPanel(
        closes=closes,
        index=index,
        trading_days=tuple(sorted(days)),
        notes=tuple(notes),
    )


def align_sessions(
    dates: Iterable[str],
    trading_days: Sequence[str],
    *,
    strictly_after: bool,
) -> list[str]:
    """外部事件日 -> A 股**第一个能反应的交易日**。去重后升序。

    `strictly_after` 就是 `OverseasInstrument.after_a_share_close`：

    - **美股**（True）：T 日收在北京 T+1 凌晨，A 股当天已经收盘了，所以取 `> T`。
    - **亚太 / 国内政策**（False）：T 日的信息在 A 股 T 日收盘前就有了，取 `>= T`。

    差一天不会报错，只会让基准率整体偏掉——把美股按 `>=` 对齐，等于用一个尚未
    发生的收盘去解释当天的走势，那种偷看未来会让数字好看得离谱。
    """
    if not trading_days:
        return []

    ordered = list(trading_days)
    aligned: set[str] = set()
    for raw in dates:
        stamp = str(raw or "").strip()
        if not stamp:
            continue
        position = (
            bisect.bisect_right(ordered, stamp)
            if strictly_after
            else bisect.bisect_left(ordered, stamp)
        )
        if position < len(ordered):
            aligned.add(ordered[position])
    return sorted(aligned)


def base_rate(
    panel: SectorPanel,
    sector: str,
    sessions: Sequence[str],
    condition: str,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    min_samples: int = MIN_SAMPLES,
) -> BaseRate:
    """条件成立时，这个板块后来怎么走。`sessions` 必须已经过 `align_sessions`。"""
    notes: list[str] = [SAMPLE_CAVEAT]
    if sector not in panel.closes:
        return BaseRate(
            sector=sector,
            condition=condition,
            samples=0,
            independent_events=0,
            notes=(f"没有 {sector} 的板块指数序列，这条映射无法验证",),
        )

    usable = [stamp for stamp in sessions if stamp in panel.index[sector]]
    if not usable:
        return BaseRate(
            sector=sector,
            condition=condition,
            samples=0,
            independent_events=0,
            span=panel.span(),
            notes=(*notes, "条件成立的交易日与该板块的序列没有交集"),
        )

    windows = [days for days in horizons if days > 0]
    reaction = _reaction_stat(panel, sector, usable)
    stats = tuple(_horizon_stat(panel, sector, usable, days) for days in windows)
    independent = _independent_events(usable, panel.trading_days, max(windows or (1,)))

    primary = next((item for item in stats if item.samples), None)
    samples = primary.samples if primary is not None else 0
    trusted = samples >= min_samples and independent >= MIN_INDEPENDENT_EVENTS
    if not trusted:
        notes.append(
            f"样本 {samples}/{min_samples}、独立行情 {independent}/{MIN_INDEPENDENT_EVENTS}，"
            "两个都过线才算可信；这条读数只作观察，不参与任何加权"
        )
    return BaseRate(
        sector=sector,
        condition=condition,
        samples=samples,
        independent_events=independent,
        span=(usable[0], usable[-1]),
        reaction=reaction,
        horizons=stats,
        trusted=trusted,
        notes=tuple(notes),
    )


# ---- internals ------------------------------------------------------------


def _reaction_stat(panel: SectorPanel, sector: str, sessions: Sequence[str]) -> HorizonStat | None:
    """反应日**自身**的涨跌：前一根收盘 → 这一根收盘。

    走 `forward(..., -1)` 的镜像而不是另写一遍：`forward` 已经处理了序列边界与
    板块各自的起始日，重写一遍等于把同一个越界判断再实现一次。
    """
    absolutes: list[float] = []
    excesses: list[float] = []
    for stamp in sessions:
        value = _self_return(panel, sector, stamp)
        if value is None:
            continue
        peers = [
            other
            for name in panel.closes
            if (other := _self_return(panel, name, stamp)) is not None
        ]
        centre = median(peers) if peers else None
        absolutes.append(value)
        if centre is not None:
            excesses.append(value - centre)
    if not absolutes:
        return None
    return HorizonStat(
        horizon=0,
        samples=len(absolutes),
        median_excess=_rounded(median_of(excesses)),
        median_absolute=_rounded(median_of(absolutes)),
        win_rate=_rate(excesses),
    )


def _self_return(panel: SectorPanel, sector: str, session: str) -> float | None:
    position = panel.index.get(sector, {}).get(session)
    rows = panel.closes.get(sector)
    if position is None or rows is None or position < 1:
        return None
    base = rows[position - 1][1]
    return rows[position][1] / base - 1.0 if base > 0 else None


def _horizon_stat(
    panel: SectorPanel, sector: str, sessions: Sequence[str], horizon: int
) -> HorizonStat:
    absolutes: list[float] = []
    excesses: list[float] = []
    for stamp in sessions:
        value = panel.forward(sector, stamp, horizon)
        if value is None:
            continue
        # 横截面中位只在**这一天**算，且只算当天有前向读数的那些板块。拿全样本
        # 的平均去减，会把不同市场阶段的收益混在一起当基准。
        peers = [
            other
            for name in panel.closes
            if (other := panel.forward(name, stamp, horizon)) is not None
        ]
        centre = median(peers) if peers else None
        absolutes.append(value)
        if centre is not None:
            excesses.append(value - centre)
    return HorizonStat(
        horizon=horizon,
        samples=len(absolutes),
        median_excess=_rounded(median_of(excesses)),
        median_absolute=_rounded(median_of(absolutes)),
        win_rate=_rate(excesses),
    )


def _independent_events(
    sessions: Sequence[str], trading_days: Sequence[str], spacing: int
) -> int:
    """粗略独立事件数：相邻两次至少隔 `spacing` 个交易日才算两轮。

    只是一个粗计数，不是统计意义上的独立性检验——它挡的是「一轮行情连续成立
    十天，被当成十个样本」这种最常见的高估。
    """
    if not sessions:
        return 0
    positions = {stamp: index for index, stamp in enumerate(trading_days)}
    ordered = sorted(
        position for stamp in sessions if (position := positions.get(stamp)) is not None
    )
    if not ordered:
        return 0
    count, previous = 1, ordered[0]
    for position in ordered[1:]:
        if position - previous >= max(1, spacing):
            count += 1
            previous = position
    return count


def _rate(values: Sequence[float]) -> float | None:
    """超额为正的比例。**按超额算，不按绝对收益算**——理由见模块 docstring 第一条。"""
    if not values:
        return None
    return round(sum(1 for value in values if value > 0) / len(values), 4)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)
