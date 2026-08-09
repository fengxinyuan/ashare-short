"""筹码分布估算：换手衰减法。

`docs/DATA_SOURCES.md` §十一记录的结论是：生产链路里**没有真实筹码数据源**：
TickFlow 无 chip/cost/winner 接口，东财 CYQ（`akshare.stock_cyq_em`）在本机
`RemoteDisconnected`（2026-08-04 复测仍然如此，与 `push2` 被出口代理拒绝同因），
同花顺没有个股筹码接口。

但那份文档漏掉了一条路：**这个分布可以自己算，输入只要日线 OHLCV + 流通股本，
两样今天都有。** 算法是通达信 CYQ 的公开做法：

```text
逐日：t = 当日换手率 = 成交量 / 流通股本
      chips *= (1 - t)                      老筹码按换手率衰减
      chips[当日价格区间] += total * t       今日成交成为新筹码，按分布假设铺开
```

派生 `avg_cost`（加权平均成本）、`winner_ratio`（获利盘比例）、`concentration_90`
（90% 筹码的价格区间宽度）。

## 它和真实筹码差在哪

差在**分布假设**，不在算法本体。真实 CYQ 用更精细的日内分布（甚至逐笔），这里用
三角分布（低点→均价→高点线性升降）。所以：

- 命名一律 `chip_*_est`，简报里写「**估算**筹码分布（换手衰减法）」；
- **永远不和 `signals/dipbuy.py` 的 `turnover_cost_15/30` 混用一个字段名。**
  三个数三个名字：`chip_avg_cost_est`（这里）/ `chip_avg_cost`（真实源，尚无）/
  `turnover_cost_30`（成交成本代理）。混用一个名字，事后没有任何办法查出某个数
  当时是从哪来的。

## 为什么这条路值得先做

它零外部依赖、不受本机网络策略影响、可以逐日回算（因此可回测、可进台账），而且
「分布之和恒等于 1」是一条能写成测试的守恒律——真实筹码源给不了这个保证。

## 阈值

价格网格取 `GRID_SIZE` 档。太少会让 `avg_cost` 量化到几个离散值上，太多在 240 根
日线上纯属浪费。`concentration_90` 用的是 5%–95% 分位区间宽度除以现价。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ashare_decision_support.numeric import finite
from ashare_decision_support.signals.dipbuy import normalised_volume

# 价格网格档数。
GRID_SIZE = 200

# 换手率的上限。一天换手 100% 以上在 A 股是可能的（次新、重组复牌），但把
# `1 - t` 压成负数会让整条分布翻符号，所以夹住。
MAX_DAILY_TURNOVER = 0.98

# 至少要有这么多根日线才给读数。太短的历史算出来的分布几乎全是最近几天的成交，
# 那已经不是「筹码」而是「近几日均价」。
MIN_BARS = 60


@dataclass(frozen=True)
class ChipDistributionEstimate:
    """一只票在收盘后的**估算**筹码分布。

    每个字段都可能是 None——算不出来时返回 None 而不是 0，「没算出来」和「算出来
    是零」在事后统计里必须能区分开（同 `signals/dipbuy.py` 的规矩）。
    """

    # 命名带 _est 是刻意的，见模块 docstring。
    chip_avg_cost_est: float | None = None
    chip_winner_ratio_est: float | None = None
    chip_concentration_90_est: float | None = None
    # 现价相对估算平均成本的偏离。正=多数筹码浮盈。
    dist_to_chip_avg_est: float | None = None
    # 参与计算的日线根数，用来判断这份分布可信到什么程度。
    bars_used: int | None = None
    method: str = "turnover-decay-triangular"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> list[str]:
        """简报里能直接贴的行。**每一行都必须带「估算」二字。**"""
        lines: list[str] = []
        if self.chip_avg_cost_est is None:
            return lines
        text = f"估算平均成本 {self.chip_avg_cost_est:.2f}"
        if self.dist_to_chip_avg_est is not None:
            text += f"（现价偏离 {self.dist_to_chip_avg_est:+.1%}）"
        lines.append(text)
        if self.chip_winner_ratio_est is not None:
            lines.append(f"估算获利盘 {self.chip_winner_ratio_est:.0%}")
        if self.chip_concentration_90_est is not None:
            lines.append(f"估算 90% 筹码区间宽度 {self.chip_concentration_90_est:.1%}")
        lines.append("以上为换手衰减法估算，不是真实筹码数据")
        return lines


def estimate_chips(
    bars: list[dict], float_shares: float | None = None
) -> ChipDistributionEstimate:
    """从前复权日线 + 流通股本估算筹码分布。

    `float_shares` 缺失时**整体弃权**：没有流通股本就没有换手率，没有换手率就没有
    衰减速度，硬算等于给一个任意的记忆长度。

    任何情况下都不抛异常。
    """
    shares = finite(float_shares)
    if shares is None or shares <= 0:
        return ChipDistributionEstimate()

    rows = _usable(bars)
    if len(rows) < MIN_BARS:
        return ChipDistributionEstimate(bars_used=len(rows) or None)

    lows = [row["low"] for row in rows]
    highs = [row["high"] for row in rows]
    floor, ceiling = min(lows), max(highs)
    if not (ceiling > floor > 0):
        return ChipDistributionEstimate(bars_used=len(rows))

    step = (ceiling - floor) / GRID_SIZE
    grid = [floor + step * (index + 0.5) for index in range(GRID_SIZE)]
    chips = [0.0] * GRID_SIZE

    # 「到目前为止铺过筹码的那一段下标」。网格铺的是**全部** 240 根的价格全幅，
    # 而前几十根只走过其中很窄一条——这条带以外的档此时精确等于 0.0，而
    # `0.0 * decay` 精确还是 0.0，衰减它们不改变任何一位。实测这条带平均只占
    # 网格的 68.6%，也就是每根日线白乘了三分之一的档。
    touched_low = GRID_SIZE
    touched_high = -1

    for row in rows:
        turnover = min(row["volume"] / shares, MAX_DAILY_TURNOVER)
        if turnover <= 0:
            continue
        decay = 1.0 - turnover
        # 只衰减铺过的那一段，且用列表推导而不是逐下标乘——同样是 `v * decay`，
        # 逐位结果完全一致，但省掉了每档一次的下标读写。这一句每只票原本要跑
        # 240 × 200 次，是这个函数剩下的主要开销。
        if touched_high >= touched_low:
            chips[touched_low : touched_high + 1] = [
                value * decay for value in chips[touched_low : touched_high + 1]
            ]
        low_index, high_index = _deposit(chips, grid, step, row, turnover)
        if high_index >= low_index:
            touched_low = min(touched_low, low_index)
            touched_high = max(touched_high, high_index)

    total = sum(chips)
    if total <= 0:
        return ChipDistributionEstimate(bars_used=len(rows))

    weights = [value / total for value in chips]
    last_close = rows[-1]["close"]
    average = sum(price * weight for price, weight in zip(grid, weights, strict=True))
    winner = sum(
        weight for price, weight in zip(grid, weights, strict=True) if price <= last_close
    )

    return ChipDistributionEstimate(
        chip_avg_cost_est=round(average, 4),
        chip_winner_ratio_est=round(winner, 4),
        chip_concentration_90_est=_concentration(grid, weights, last_close),
        dist_to_chip_avg_est=(
            round(last_close / average - 1.0, 6) if average > 0 else None
        ),
        bars_used=len(rows),
    )


def _deposit(
    chips: list[float], grid: list[float], step: float, row: dict, turnover: float
) -> tuple[int, int]:
    """把当日成交按**三角分布**铺在 low..high 之间，峰值在均价。

    返回**实际写到的下标区间** `(low_index, high_index)`，一档都没写到时返回一个
    空区间（`low > high`）。调用方靠它维护「哪一段可能非零」，从而不必每根都去
    衰减那些精确为 0.0 的档。

    三角而不是均匀：一天的成交并不平均分布在最高最低之间，靠近均价的价位成交
    最密集。这是这套估算与真实 CYQ 唯一的实质差别，写在这里而不是文档里，是因为
    换掉它就是换掉整份分布的形状。

    **只扫 low..high 覆盖的那几档，不扫整条网格。** 网格铺的是 240 根日线的全幅
    （实测常见 2–4 倍价差），而单根 K 线的振幅通常在 2% 上下——200 档里真正落进
    区间的往往不到 10 档，其余 190 多档此前每根都要走一遍「比较 → append(0.0)」，
    再被 `sum` 和 `enumerate` 各扫一遍。实测这一处占整个 `analyze_universe` 的
    近一半时间，改成按下标切片后同一批输入**逐位完全相同**（差值 0.0），快 3.4 倍。
    """
    low, high, peak = row["low"], row["high"], row["vwap"]
    if high <= low:
        index = min(max(int((low - grid[0] + step / 2) / step), 0), len(chips) - 1)
        chips[index] += turnover
        return index, index

    # 各留一档余量，边界仍由下面的 `price < low or price > high` 判定——
    # 下标是算出来的，浮点误差不该由它来兜底。
    base = grid[0]
    start = max(0, int((low - base) / step) - 1)
    end = min(len(chips) - 1, int((high - base) / step) + 1)

    indices: list[int] = []
    heights: list[float] = []
    for index in range(start, end + 1):
        price = grid[index]
        if price < low or price > high:
            continue
        # 三角形：在 peak 处取 1，向两端线性降到 0。
        if price <= peak:
            height = (price - low) / (peak - low) if peak > low else 1.0
        else:
            height = (high - price) / (high - peak) if high > peak else 1.0
        if height > 0.0:
            indices.append(index)
            heights.append(height)

    weight_total = sum(heights)
    if weight_total <= 0:
        return 0, -1
    # 运算顺序保持 `turnover * weight / weight_total`，**不要**把除法提到循环外。
    # 提出来省一次除法，但 `w * (t / wt)` 与 `(t * w) / wt` 在浮点下差最后一位，
    # 而这份分布要进台账、要和历史产物对得上。测试里那份全网格对照实现会抓到它。
    for index, height in zip(indices, heights, strict=True):
        chips[index] += turnover * height / weight_total
    return indices[0], indices[-1]


def _concentration(
    grid: list[float], weights: list[float], last_close: float
) -> float | None:
    """90% 筹码所在价格区间的宽度，除以现价。越小越集中。"""
    if last_close <= 0:
        return None
    cumulative = 0.0
    low_price: float | None = None
    high_price: float | None = None
    for price, weight in zip(grid, weights, strict=True):
        previous = cumulative
        cumulative += weight
        if low_price is None and cumulative >= 0.05:
            low_price = price
        if high_price is None and cumulative >= 0.95 and previous < 0.95:
            high_price = price
    if low_price is None or high_price is None or high_price < low_price:
        return None
    return round((high_price - low_price) / last_close, 6)


def _usable(bars: list[dict]) -> list[dict]:
    """归一成 {low, high, close, vwap, volume(股)}。

    量纲用 `amount/(volume*close)` 反推（`signals/dipbuy.normalised_volume`）——
    volume 记的是手还是股，直接决定换手率差 100 倍，也就直接决定衰减速度。
    推不出单位的那一根**整根丢弃**，不拿原始数字硬算。
    """
    rows: list[dict] = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        low, high = finite(bar.get("low")), finite(bar.get("high"))
        close = finite(bar.get("close"))
        amount = finite(bar.get("amount"))
        volume = normalised_volume(bar)
        if None in (low, high, close) or volume is None or volume <= 0:
            continue
        if low <= 0 or high <= 0 or close <= 0:
            continue
        # 浮点卫生：复权后 high < low 的倒挂实测占 9.33%（`docs/DATA_SOURCES.md` §五）。
        if high < low:
            low, high = high, low
        vwap = amount / volume if amount and volume else close
        # VWAP 落在当根区间外说明量纲或成交额有问题，退回收盘价而不是信它。
        if not (low <= vwap <= high):
            vwap = close
        rows.append(
            {"low": low, "high": high, "close": close, "vwap": vwap, "volume": volume}
        )
    return rows
