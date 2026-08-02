"""限价单口径：唯一能回答「我的低吸规则值不值那 0.2% 成本」的模拟。

`suitability.py` 的三档口径给的是**天花板与基线**——`oracle` 是事后视角的上限，
`neutral`／`strict` 是零判断的基线（实测约 -0.2%，正好一个往返成本）。真正的策略
落在两者之间，而这个模块负责把它算出来。

## 规则形状

    T-1 日收盘：算出挂单价 = close[T-1] × (1 - entry_atr × ATR)
    T   日     ：low[T] 触及则成交，成交价 = min(挂单价, open[T])   ← 跳空低开按更优价
    T+1 日     ：high[T+1] 触及止盈价则按 max(止盈价, open[T+1]) 卖出
                 否则收盘平掉

T-1 收盘决策、T 日买入、T+1 日卖出——**符合 T+1**，而且和 `neutral`／`strict` 同为
T→T+1 的跨度，三者可以直接比。

## 三个必须做对的地方，否则数字是假的

**ATR 必须滚动。** 挂单价用的波幅只能来自决策时刻已经收盘的那些天。拿全样本 ATR
算挂单价是未来函数——用未来的波动率决定今天挂多深，回测会凭空多出一大截收益。
`_rolling_atr()` 的 `atr[i]` 严格只用 `rows[:i]`。

**未成交不能记 0 收益。** 挂得越深成交率越低，而低成交率会让「成交样本的中位数」
虚高——那几次成交往往是暴跌日，幸存下来的更是特例。所以排序键用 `per_attempt`
（= 成交率 × 成交样本中位数），它同时惩罚「机会太少」和「单次太薄」。

**网格最优天生虚高。** 扫 24 个参数点再挑最好的那个，等于做了 24 次尝试只报最好
的一次。这里的对策有两条：最优参数一律拿去样本外重跑（`LimitScan.holdout`），以及
报告**邻域中位数**（`neighbourhood`）——只有一个点好、周围全差，那是噪声；一整片
区域都好，才可能是这只票的性质。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

from ashare_decision_support.screening.suitability import (
    ScreenConfig,
    _atr_pct,
    _median,
    _normalise,
    _quantile,
    _segment_medians,
)

# 挂单深度网格，单位是标的自身的 ATR。0.0 = 按昨收挂（几乎必成交，退化成市价单），
# 1.5 = 要跌一个半日常波动才接。再深就不是低吸而是抄底，样本会稀到没有统计意义。
DEFAULT_ENTRY_GRID = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5)

# 止盈目标网格，同样以 ATR 为单位。0.5 个 ATR 是「当天回到均值就走」，2.0 是「等
# 一根像样的阳线」。
DEFAULT_TARGET_GRID = (0.5, 1.0, 1.5, 2.0)

ATR_WINDOW = 20

# 样本内成交次数低于这个数的参数点不参与「最优」评选。两年日线约 480 根，30 次
# 成交意味着平均每月一次多一点——再少就不是统计，是轶事。
MIN_FILLS = 30


@dataclass(frozen=True)
class LimitRule:
    entry_atr: float
    target_atr: float

    def label(self) -> str:
        return f"跌{self.entry_atr:g}ATR买 / 涨{self.target_atr:g}ATR走"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LimitResult:
    """一组参数在一段区间上的模拟结果。"""

    rule: LimitRule
    attempts: int = 0
    fills: int = 0
    fill_rate: float | None = None
    target_hits: int = 0
    hit_rate: float | None = None
    """成交之后打到止盈的比例。剩下的都是收盘平仓——那部分才是真实的成本来源。"""
    stuck: int = 0
    """卖出日一字跌停、卖不掉的次数。按跌停价记账，仍然**低估**了损失（实际要多
    套至少一天），所以这个数要单独看，不能只看收益分布。"""
    median: float | None = None
    win_rate: float | None = None
    p25: float | None = None
    worst: float | None = None
    per_attempt: float | None = None
    """成交率 × 成交样本中位数。**排序用这个**，不要用 `median`。"""
    segments: list[float] = field(default_factory=list)
    segment_pass: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LimitScan:
    """一只票上的完整网格扫描。"""

    symbol: str
    best: LimitResult | None = None
    holdout: LimitResult | None = None
    neighbourhood: float | None = None
    decay: float | None = None
    grid: list[LimitResult] = field(default_factory=list)
    verdict: str = "reject"
    """`pass` / `unstable`（样本外或邻域垮掉）/ `reject`（样本内就没有正收益参数）"""
    notes: list[str] = field(default_factory=list)

    def to_dict(self, include_grid: bool = False) -> dict[str, Any]:
        payload = {
            "symbol": self.symbol,
            "verdict": self.verdict,
            "best": self.best.to_dict() if self.best else None,
            "holdout": self.holdout.to_dict() if self.holdout else None,
            "neighbourhood": self.neighbourhood,
            "decay": self.decay,
            "notes": self.notes,
        }
        if include_grid:
            payload["grid"] = [item.to_dict() for item in self.grid]
        return payload


def scan_limit_rules(
    symbol: str,
    bars: list[dict],
    config: ScreenConfig | None = None,
    *,
    entry_grid: tuple[float, ...] = DEFAULT_ENTRY_GRID,
    target_grid: tuple[float, ...] = DEFAULT_TARGET_GRID,
) -> LimitScan:
    """在样本内扫参数网格，把最优的一组拿到样本外重跑。

    和 `screen_symbol()` 用同一个切分比例，两边的样本外区间因此是同一段——否则
    「适配性通过但限价规则不通过」这类结论没法归因到底是票的问题还是规则的问题。
    """
    cfg = config or ScreenConfig()
    rows = _normalise(bars)
    if len(rows) < cfg.min_bars:
        return LimitScan(symbol=symbol, notes=[f"日线仅 {len(rows)} 根，不足以扫参数"])

    split = int(len(rows) * (1.0 - cfg.holdout_ratio))
    in_sample, out_sample = rows[:split], rows[split:]

    grid: list[LimitResult] = []
    by_position: dict[tuple[int, int], LimitResult] = {}
    for ei, entry in enumerate(entry_grid):
        for ti, target in enumerate(target_grid):
            result = simulate(symbol, in_sample, LimitRule(entry, target), cfg)
            grid.append(result)
            by_position[(ei, ti)] = result

    ranked = [
        item
        for item in grid
        if item.fills >= MIN_FILLS and item.per_attempt is not None
    ]
    if not ranked:
        return LimitScan(
            symbol=symbol,
            grid=grid,
            notes=[f"网格内没有参数点达到 {MIN_FILLS} 次成交，无法评选"],
        )

    best = max(ranked, key=lambda item: item.per_attempt)
    notes: list[str] = []

    if best.per_attempt is not None and best.per_attempt <= 0:
        return LimitScan(
            symbol=symbol,
            best=best,
            grid=grid,
            verdict="reject",
            notes=["样本内最优参数的每次挂单期望仍为负——这只票上没有能覆盖成本的低吸规则"],
        )

    neighbourhood = _neighbourhood(by_position, best, entry_grid, target_grid)
    holdout = (
        simulate(symbol, out_sample, best.rule, cfg) if len(out_sample) >= 40 else None
    )
    decay = None
    if holdout and holdout.per_attempt is not None and best.per_attempt:
        decay = round(holdout.per_attempt / best.per_attempt, 4)

    verdict = "pass"
    if holdout is None:
        verdict = "unstable"
        notes.append("样本外区间不足 40 根日线，最优参数没有经过验证")
    elif decay is None or decay < cfg.min_decay:
        verdict = "unstable"
        notes.append(
            f"最优参数在样本外衰减到 {_ratio(decay)}——网格最优是挑出来的，不是这只票的性质"
        )
    if neighbourhood is not None and best.per_attempt and neighbourhood <= 0:
        verdict = "unstable"
        notes.append(
            "最优点的邻域中位数为负：只有这一个参数点好，周围全差，形状像噪声而不像规律"
        )

    return LimitScan(
        symbol=symbol,
        best=best,
        holdout=holdout,
        neighbourhood=neighbourhood,
        decay=decay,
        grid=grid,
        verdict=verdict,
        notes=notes,
    )


def simulate(
    symbol: str, rows: list[dict], rule: LimitRule, config: ScreenConfig | None = None
) -> LimitResult:
    """跑一组参数。`rows` 必须已经过 `_normalise()`。"""
    cfg = config or ScreenConfig()
    cost = cfg.cost.round_trip
    atrs = _rolling_atr(rows, ATR_WINDOW)

    nets: list[float] = []
    attempts = fills = hits = stuck = 0

    # i 是买入日，i+1 是卖出日；挂单价在 i-1 日收盘时就定下来了。
    for i in range(1, len(rows) - 1):
        atr = atrs[i]
        if not atr:
            continue
        prev, day, nxt = rows[i - 1], rows[i], rows[i + 1]
        if not day.get("volume") or not nxt.get("volume"):
            continue

        attempts += 1
        limit_price = prev["close"] * (1.0 - rule.entry_atr * atr)
        if day["low"] > limit_price:
            continue  # 没跌到挂单价，这次不成交

        # 跳空低开时限价单成交在开盘价，比挂单价更优。反过来不会发生。
        entry = min(limit_price, day["open"])
        if entry <= 0:
            continue
        fills += 1

        target_price = entry * (1.0 + rule.target_atr * atr)
        if nxt["high"] >= target_price:
            exit_price = max(target_price, nxt["open"])
            hits += 1
        else:
            exit_price = nxt["close"]
            if _is_one_word_down(nxt):
                stuck += 1

        nets.append(round(exit_price / entry - 1.0 - cost, 6))

    if not nets:
        return LimitResult(
            rule=rule,
            attempts=attempts,
            fill_rate=0.0 if attempts else None,
        )

    ordered = sorted(nets)
    fill_rate = fills / attempts if attempts else None
    med = _median(nets)
    segments = _segment_medians(nets, 4)

    return LimitResult(
        rule=rule,
        attempts=attempts,
        fills=fills,
        fill_rate=round(fill_rate, 4) if fill_rate is not None else None,
        target_hits=hits,
        hit_rate=round(hits / fills, 4) if fills else None,
        stuck=stuck,
        median=med,
        win_rate=round(sum(1 for net in nets if net > 0) / len(nets), 4),
        p25=round(_quantile(ordered, 0.25), 6),
        worst=round(ordered[0], 6),
        per_attempt=(
            round(fill_rate * med, 6) if fill_rate is not None and med is not None else None
        ),
        segments=segments,
        segment_pass=sum(1 for value in segments if value > 0),
    )


def _rolling_atr(rows: list[dict], window: int) -> list[float | None]:
    """`atr[i]` 只用 `rows[:i]` 算——决策发生在 i-1 日收盘，i 日的波幅那时还不存在。

    这是全模块最容易写错、写错了又最不容易发现的一处：用全样本 ATR 回测出来的
    收益会明显更高，而且曲线依然平滑好看。
    """
    out: list[float | None] = []
    for i in range(len(rows)):
        window_rows = rows[max(0, i - window) : i]
        out.append(_atr_pct(window_rows) if len(window_rows) >= 6 else None)
    return out


def _is_one_word_down(row: dict) -> bool:
    """一字跌停：全天一个价，且跌幅到位。卖不掉的那种日子。"""
    if not (row["high"] == row["low"] == row["open"] == row["close"]):
        return False
    prev = row.get("prev_close")
    return bool(prev and prev > 0 and row["close"] < prev)


def _neighbourhood(
    by_position: dict[tuple[int, int], LimitResult],
    best: LimitResult,
    entry_grid: tuple[float, ...],
    target_grid: tuple[float, ...],
) -> float | None:
    """最优点周围 8 格的 `per_attempt` 中位数。

    参数曲面平不平，比峰值有多高重要得多：一个孤立的尖峰几乎总是采样噪声，一片
    连续的高地才对应真实的市场结构。
    """
    position = next(
        (pos for pos, item in by_position.items() if item is best),
        None,
    )
    if position is None:
        return None
    ei, ti = position

    values: list[float] = []
    for dei in (-1, 0, 1):
        for dti in (-1, 0, 1):
            if dei == 0 and dti == 0:
                continue
            neighbour = by_position.get((ei + dei, ti + dti))
            if neighbour is not None and neighbour.per_attempt is not None:
                values.append(neighbour.per_attempt)
    return round(median(values), 6) if values else None


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"
