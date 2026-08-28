"""相对强度：个股涨跌减去它所属宽基指数的涨跌。

## 为什么需要它

整套系统此前对个股只有**绝对**涨跌。绝对涨跌答不了超短最常问的那句话——
「今天是它自己不行，还是大盘不行？」

2026-08-04 牧原股份实测：当日 −2.34%，而同一天深证成指 +2.18%（盘中读数）。
两个数分开看都不吓人，**差值 −4.5 个点**才是当天最该看见的那一行：在一个指数
大涨的日子里逆势破位，和跟着大盘一起回调，是完全不同的两件事。而系统当时给出的
是「趋势 RANGE、日线量比 0.55」这类纯绝对读数，把这条证据整个漏掉了。

同一天板块内对照也说明问题：温氏 −2.37%、新希望 −1.84%、神农 −1.02%、
华统 −0.74%、巨星 −0.07%。**两个最大市值的龙头跌得最狠**——洗盘阶段龙头通常
相对抗跌，龙头被砸得比小票厉害是派发特征。这层读数把这种对照变成可记录的数。

## 这一层只出读数

和 `signals/volume_price.py` 同一条口径：「超额 −4.5%」是事实，「所以该卖」是判断。
标签（`显著跑输` 等）只压缩事实，不含动作。含义由 `strategy/` 与 `advice/` 的判据链
决定，不在这里。

## 口径

- **纯计算，不做 I/O。** 基准日线由调用方给，和 `measure_volume_price` 一样。
  这样它在测试里不需要网络，也不会把某个数据源的可用性绑进信号层。
- 个股与基准必须用**同一段交易日**。两边日期对不齐时按共同交易日取交集，
  并在 `notes` 里写明用了几天——静默错位算出来的超额是最难发现的那种错。
- 收益率一律 `close / prev_close - 1`，与 `broad_index.IndexQuote.ret` 同口径。
- 个股用**前复权**日线时基准也必须是指数本身（指数不除权），两者天然可比。

## 阈值是先验设定

下面的档位是超短交易里通用的经验刻度，**不是从本系统台账标定出来的**，也没有回测
支持。它们的作用是让「跑输」这句话有一个确定的判据。等 `journal/stats.py` 攒够样本
再谈调参——在那之前按统计调这些数，调的是噪声。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.numeric import positive

# 超额收益的档位切点（单位：小数，0.005 = 0.5 个百分点）。
# 0.5% 以内算「同步」——留一条中性带，否则指数与个股都在 ±0.1% 抖动时，
# 标签会天天翻面。
SYNC_BAND = 0.005
STRONG_BAND = 0.02

# 多日超额的窗口。1 日回答「今天」，5 日回答「这一周是不是一直在被卖」，
# 20 日对应一个完整的派发周期。
WINDOWS: tuple[int, ...] = (1, 5, 20)

LABEL_STRONG_OUT = "显著跑赢"
LABEL_OUT = "跑赢"
LABEL_SYNC = "同步"
LABEL_UNDER = "跑输"
LABEL_STRONG_UNDER = "显著跑输"


# 个股 → 宽基基准。用**板块归属**而不是交易所，因为创业板/科创板的贝塔和主板
# 差得远：一只 300xxx 在创业板指跌 3% 的日子跌 2%，那是跑赢，不是跑输。
# 代码取 `broad_index.BROAD_INDICES` 里已有的那几个，不新增数据源。
BENCHMARKS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("688", "689"), "sh000688", "科创50"),
    (("300", "301"), "sz399006", "创业板指"),
    (("600", "601", "603", "605"), "sh000001", "上证指数"),
    (("000", "001", "002", "003"), "sz399001", "深证成指"),
)


@dataclass(frozen=True)
class RelativeStrength:
    """一只票相对它所属宽基指数的表现。缺基准时整体弃权，不退化成绝对涨跌。"""

    symbol: str
    benchmark_code: str | None = None
    benchmark_name: str | None = None
    stock_return: dict[int, float] = field(default_factory=dict)
    benchmark_return: dict[int, float] = field(default_factory=dict)
    excess: dict[int, float] = field(default_factory=dict)
    label: str | None = None
    sessions_used: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "benchmark_code": self.benchmark_code,
            "benchmark_name": self.benchmark_name,
            "stock_return": {f"{w}d": v for w, v in self.stock_return.items()},
            "benchmark_return": {f"{w}d": v for w, v in self.benchmark_return.items()},
            "excess": {f"{w}d": v for w, v in self.excess.items()},
            "label": self.label,
            "sessions_used": self.sessions_used,
            "notes": list(self.notes),
        }

    def describe(self) -> list[str]:
        if not self.excess or self.benchmark_name is None:
            return list(self.notes)
        lines = []
        for window in WINDOWS:
            if window not in self.excess:
                continue
            lines.append(
                f"{window} 日相对 {self.benchmark_name} 超额 {self.excess[window] * 100:+.2f}%"
                f"（个股 {self.stock_return[window] * 100:+.2f}%，指数 {self.benchmark_return[window] * 100:+.2f}%）"
            )
        if self.label:
            lines.append(f"相对强度：{self.label}")
        lines.extend(self.notes)
        return lines


def benchmark_for(symbol: str) -> tuple[str, str] | None:
    """把 `002714.SZ` / `002714` / `sz002714` 映射到它的宽基基准。"""
    digits = "".join(ch for ch in str(symbol) if ch.isdigit())
    if len(digits) < 6:
        return None
    prefix = digits[:3]
    for prefixes, code, name in BENCHMARKS:
        if prefix in prefixes:
            return code, name
    # 北交所与其余代码段没有合适的宽基口径。**返回 None 而不是硬套上证指数**——
    # 拿一个贝塔完全不同的基准算超额，比不算更有害。
    return None


def measure_relative_strength(
    symbol: str,
    stock_bars: list[dict],
    benchmark_bars: list[dict],
    benchmark_name: str | None = None,
    benchmark_code: str | None = None,
) -> RelativeStrength:
    """个股相对基准的多窗口超额收益。

    `stock_bars` / `benchmark_bars` 是日线，需要 `date` 与 `close` 两个字段，
    按日期升序或乱序都可以——这里会按日期对齐后再算。
    """
    resolved = benchmark_for(symbol)
    code = benchmark_code or (resolved[0] if resolved else None)
    name = benchmark_name or (resolved[1] if resolved else None)

    if code is None:
        return RelativeStrength(
            symbol=symbol,
            notes=["没有匹配的宽基基准（北交所或未知代码段），本次不出相对强度"],
        )

    stock_closes = _closes_by_date(stock_bars)
    bench_closes = _closes_by_date(benchmark_bars)
    common = sorted(set(stock_closes) & set(bench_closes))
    if len(common) < 2:
        return RelativeStrength(
            symbol=symbol,
            benchmark_code=code,
            benchmark_name=name,
            notes=["个股与基准的共同交易日不足 2 天，本次不出相对强度"],
        )

    notes: list[str] = []
    if len(common) < len(stock_closes):
        notes.append(
            f"个股 {len(stock_closes)} 个交易日、基准 {len(bench_closes)} 个，"
            f"按共同交易日 {len(common)} 天对齐"
        )

    stock_series = [stock_closes[day] for day in common]
    bench_series = [bench_closes[day] for day in common]

    stock_ret: dict[int, float] = {}
    bench_ret: dict[int, float] = {}
    excess: dict[int, float] = {}
    for window in WINDOWS:
        s = _window_return(stock_series, window)
        b = _window_return(bench_series, window)
        if s is None or b is None:
            continue
        stock_ret[window] = round(s, 6)
        bench_ret[window] = round(b, 6)
        excess[window] = round(s - b, 6)

    if not excess:
        return RelativeStrength(
            symbol=symbol,
            benchmark_code=code,
            benchmark_name=name,
            sessions_used=len(common),
            notes=[*notes, "共同交易日不足最短窗口，本次不出相对强度"],
        )

    return RelativeStrength(
        symbol=symbol,
        benchmark_code=code,
        benchmark_name=name,
        stock_return=stock_ret,
        benchmark_return=bench_ret,
        excess=excess,
        # 标签只看当日。多日超额是趋势读数，压成一个词会把「今天逆势跌」和
        # 「这个月一直弱」混成同一句话，而这两件事的处置完全不同。
        label=_label(excess.get(1)),
        sessions_used=len(common),
        notes=notes,
    )


def _label(excess_1d: float | None) -> str | None:
    if excess_1d is None:
        return None
    if excess_1d >= STRONG_BAND:
        return LABEL_STRONG_OUT
    if excess_1d >= SYNC_BAND:
        return LABEL_OUT
    if excess_1d <= -STRONG_BAND:
        return LABEL_STRONG_UNDER
    if excess_1d <= -SYNC_BAND:
        return LABEL_UNDER
    return LABEL_SYNC


def _closes_by_date(bars: list[dict]) -> dict[str, float]:
    """按日期取收盘。同一天出现多行时后来者覆盖——日线里那只可能是重复推送。"""
    result: dict[str, float] = {}
    for bar in bars or []:
        day = bar.get("date") or bar.get("day") or bar.get("trade_date")
        value = positive(bar.get("close"))
        if day is None or value is None:
            continue
        result[str(day)[:10]] = value
    return result


def _window_return(closes: list[float], window: int) -> float | None:
    """窗口收益。需要 `window + 1` 根——N 日收益是 N+1 个收盘价之间的事。

    **这个算式在仓里有三份，三份都是有意留着的**：这里、`signals/series.py:change`
    和 `sectors/panel.py:pct`。`panel.pct` 的 docstring 记着不并的理由（入参密度
    不同，两边的「近 5 根」不是同一个 5 根），这里补上第三份的位置：

    - `series.change` 收的是过 `series()` 的个股日线，取不到值的根被跳过，长度
      可能小于 bars；它还**收一次小数位**，另外两份不收。
    - 这一份的入参是两条序列按日期取交集之后的结果（见 `common`），密度由交集
      决定，而且**两条必须同下标对齐**——对齐是这一层的全部意义，合并到任何一个
      不知道「有两条序列」的通用函数里，这条约束就没有地方可写。

    并到 `panel.pct` 还会**把依赖方向倒过来**：`sectors/` 引 `signals/`（见
    `sectors/momentum.py`），反过来一次都没有。
    """
    if len(closes) < window + 1:
        return None
    base = closes[-(window + 1)]
    if base <= 0:
        return None
    return closes[-1] / base - 1.0
