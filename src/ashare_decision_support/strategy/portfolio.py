"""组合层读数：这几笔持仓合起来是一个什么敞口。

单笔那一层已经齐了——`monitor/holdings.py` 判止损 / 目标 / 结构破坏 / 时间止损 /
浮盈回吐，`strategy/positioning.py` 把「单笔该给多少仓位」收在一处。**没有的是把
它们加起来的那一层**。

超短线里这一层恰恰是最容易出事的：三只票分属三个代码，看起来是三笔分散的仓位，
但它们可能挂在同一个概念上。那时它不是三笔，是一笔重仓——回调来的时候三个止损
会在同一根 K 线上一起触发，而「我已经分散了」这个印象是在建仓那一刻形成的，
不会自己更正。

这一层只做三件事，全部是确定性读数：

1. **权重**：每只占组合多少。有成本价和股数就按金额算，缺一个就退回按只数算，
   并且**说清楚现在是哪一种口径**——两种口径下「占 33%」是完全不同的两句话。
2. **归并**：按行业与题材把持仓合并，看合并之后最大的那一块有多大。题材用
   `SecurityMaster.themes()` 过滤过的那份，否则最大的一块永远是「融资融券」
   （覆盖 69% 的市场，那是可交易属性不是题材，见 `adapters/security_master.py`）。
3. **计划完整度**：哪几笔没有止损、没有目标、没有成本价。缺止损的那一笔不是
   「还没想好」，是**这笔仓位今天没有失效条件**——盘中监控对它只能报绝对价位。

## 不做的两件事

- **不报总仓位。** 「仓位上限 0.30」说的是占**账户总资金**的比例，而这套系统只
  知道持仓表，不知道你的账户里还有多少钱，也永远不该知道（它不接管账户）。
  拿持仓内部的权重去和周期上限比是个看起来对、实际错的数。
- **不给建议。** 集中度高不等于该减仓——主升段里集中在一个题材上正是打法本身。
  这一层只把「你现在实际押在什么上面」摆出来，动作由人决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.numeric import positive

# 单一板块占到这个比例，「三只票」这个说法就已经不成立了。**先验设定**，不是回测
# 出来的——同 `strategy/regime.py` 的那组切点：它的作用是让「其实是一笔重仓」这件
# 事第一次能被说出来，而不是精确。
CROWDED_CLUSTER = 0.60

# 单票占比。超过一半时组合的走势基本由它一只决定，其余几只是配角。
CROWDED_SINGLE = 0.50

# 权重口径。两个都要出现在读数里：同样是「占 33%」，按金额和按只数说的不是一件事。
BASIS_AMOUNT = "金额"
BASIS_COUNT = "只数"

LEVEL_INDUSTRY = "行业"
LEVEL_THEME = "题材"

# 一块敞口至少要两只票。**这个数同时是调用方的判据**：只有一只（或空仓）时归并
# 不出任何东西，那份 1.9MB / 5550 只的元数据缓存就不必读——`assemble()` 每次问答、
# 每次 `facts`、每次 MCP 调用都会走到那里，解析一次 58ms。
MIN_CLUSTER_SYMBOLS = 2


@dataclass(frozen=True)
class Cluster:
    """归并之后的一块敞口。"""

    label: str
    level: str
    symbols: tuple[str, ...]
    weight: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "名称": self.label,
            "层级": self.level,
            "标的": list(self.symbols),
            "权重": round(self.weight, 4),
        }


@dataclass(frozen=True)
class PortfolioRead:
    """一份持仓在组合层的样子。缺数据的字段一律 None / 空，不猜。"""

    count: int = 0
    basis: str = BASIS_COUNT
    cost_value: float | None = None
    weights: tuple[tuple[str, float], ...] = ()
    clusters: tuple[Cluster, ...] = ()
    plan_gaps: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def top(self) -> tuple[str, float] | None:
        """权重最大的那一只。空仓时没有。"""
        return self.weights[0] if self.weights else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "持仓数": self.count,
            "权重口径": self.basis,
            "成本市值": self.cost_value,
            "权重": [{"symbol": symbol, "权重": round(weight, 4)} for symbol, weight in self.weights],
            "归并": [cluster.to_dict() for cluster in self.clusters],
            "计划缺口": list(self.plan_gaps),
            "说明": list(self.notes),
        }

    def describe(self) -> list[str]:
        """给简报与事实包用的几句话。空仓时是空列表，不写「暂无持仓」那种废话。"""
        if not self.count:
            return []
        lines = [f"持仓 {self.count} 只，权重按{self.basis}计算"]
        top = self.top
        if top is not None and top[1] >= CROWDED_SINGLE:
            lines.append(f"单票集中：{top[0]} 占 {top[1]:.0%}，组合走势主要由它决定")
        for cluster in self.clusters:
            if cluster.weight < CROWDED_CLUSTER:
                continue
            lines.append(
                f"{cluster.level}集中：{cluster.label} 占 {cluster.weight:.0%}"
                f"（{len(cluster.symbols)}/{self.count} 只），"
                "回调时这几只大概率同向——按一笔仓位看待，不是几笔"
            )
        if self.plan_gaps:
            lines.append("计划缺口：" + "；".join(self.plan_gaps))
        return lines


def read_portfolio(
    entries: list[Any],
    *,
    master: Any = None,
    top_clusters: int = 3,
) -> PortfolioRead:
    """把持仓表读成组合层的读数。

    `entries` 是 `state.Position` 的列表（只用 `symbol` / `cost_price` / `shares` /
    `plan_stop` / `plan_target` 四个字段，所以任何同形状的对象都行）。`master` 是
    标的元数据，取不到就只有权重没有归并——**没有分类不是「没有集中」**，两者在
    读数里必须分得开，所以走 `notes` 而不是返回一个空的归并表。
    """
    rows = list(entries or [])
    if not rows:
        return PortfolioRead()

    amounts, basis, notes = _weights_of(rows)
    total = sum(amounts.values())
    weights = tuple(
        sorted(
            ((symbol, value / total) for symbol, value in amounts.items() if total > 0),
            key=lambda item: (-item[1], item[0]),
        )
    )
    weight_of = dict(weights)

    clusters, cluster_notes = _clusters_of(rows, weight_of, master)
    return PortfolioRead(
        count=len(rows),
        basis=basis,
        cost_value=round(total, 2) if basis == BASIS_AMOUNT else None,
        weights=weights,
        clusters=tuple(clusters[:top_clusters]),
        plan_gaps=_plan_gaps(rows),
        notes=tuple(notes + cluster_notes),
    )


def _weights_of(rows: list[Any]) -> tuple[dict[str, float], str, list[str]]:
    """每只的权重分子，以及这是哪一种口径。

    **全都有成本价和股数才按金额算。** 只有一半有的时候按金额算等于把缺的那几只
    当成 0 权重——它们会从集中度里消失，而消失的方向刚好是「看起来更分散」。
    """
    amounts: dict[str, float] = {}
    missing: list[str] = []
    for row in rows:
        symbol = str(getattr(row, "symbol", "") or "")
        if not symbol:
            continue
        cost = positive(getattr(row, "cost_price", None))
        shares = positive(getattr(row, "shares", None))
        if cost is None or shares is None:
            missing.append(symbol)
        amounts[symbol] = (cost or 0.0) * (shares or 0.0)

    if not missing:
        return amounts, BASIS_AMOUNT, []
    return (
        dict.fromkeys(amounts, 1.0),
        BASIS_COUNT,
        [
            (
                f"{'、'.join(missing)} 缺成本价或股数，本次按只数等权计算权重"
                "——金额口径下的集中度可能比这更高，也可能更低"
            )
        ],
    )


def _clusters_of(
    rows: list[Any], weight_of: dict[str, float], master: Any
) -> tuple[list[Cluster], list[str]]:
    """按行业与题材归并。取不到分类就返回空表加一条说明。"""
    if len(rows) < MIN_CLUSTER_SYMBOLS:
        # 一只票归并不出东西，**这不是降级**——此时不写「元数据不可用」，那句话会让
        # 人以为少了一份本该有的读数。调用方据同一个数决定不去读那份缓存。
        return [], []
    if master is None or not getattr(master, "ok", False):
        return [], ["标的元数据不可用，本次只有权重、没有行业与题材归并"]

    themes = set(master.themes()) if hasattr(master, "themes") else set()
    industry_groups: dict[str, list[str]] = {}
    theme_groups: dict[str, list[str]] = {}
    unknown: list[str] = []
    for row in rows:
        symbol = str(getattr(row, "symbol", "") or "")
        info = master.get(symbol) if symbol else None
        if info is None:
            unknown.append(symbol)
            continue
        if info.industry:
            industry_groups.setdefault(info.industry, []).append(symbol)
        for tag in info.concepts:
            # 题材只认过滤后的那份：不过滤的话最大的一块永远是「融资融券」。
            if tag in themes:
                theme_groups.setdefault(tag, []).append(symbol)

    clusters = [
        *_to_clusters(industry_groups, LEVEL_INDUSTRY, weight_of),
        *_to_clusters(theme_groups, LEVEL_THEME, weight_of),
    ]
    clusters.sort(key=lambda item: (-item.weight, item.level, item.label))
    notes = (
        [f"{'、'.join(unknown)} 在元数据里查不到，未参与归并"] if unknown else []
    )
    return clusters, notes


def _to_clusters(
    groups: dict[str, list[str]], level: str, weight_of: dict[str, float]
) -> list[Cluster]:
    """一只票挂在多个题材上时，它在每个题材里都按整份权重计——这几块**本来就重叠**。

    按题材数摊薄会得出一个谁也不超过阈值的表，而那不是事实：三只票都在 PCB 上，
    PCB 这一块的敞口就是这三只的全部权重，不因为它们同时还挂着别的标签而变小。
    """
    return [
        Cluster(
            label=label,
            level=level,
            symbols=tuple(symbols),
            weight=sum(weight_of.get(symbol, 0.0) for symbol in symbols),
        )
        for label, symbols in groups.items()
        if len(symbols) >= MIN_CLUSTER_SYMBOLS
    ]


def _plan_gaps(rows: list[Any]) -> tuple[str, ...]:
    """没有止损的那几笔。

    **止损缺失单独列，不和目标价混在一起。** 没有目标价只是少一个止盈参考；
    没有止损意味着这笔仓位今天**没有失效条件**，盘中监控对它只能报绝对价位，
    而「什么时候认错」这件事只能由人临场决定——那正是超短线最不该临场决定的事。
    """
    no_stop = [
        str(getattr(row, "symbol", "") or "")
        for row in rows
        if positive(getattr(row, "plan_stop", None)) is None
    ]
    no_target = [
        str(getattr(row, "symbol", "") or "")
        for row in rows
        if positive(getattr(row, "plan_target", None)) is None
    ]
    gaps: list[str] = []
    if no_stop:
        gaps.append(f"{'、'.join(no_stop)} 没有计划止损（这笔仓位没有失效条件）")
    if no_target:
        gaps.append(f"{'、'.join(no_target)} 没有计划目标")
    return tuple(gaps)
