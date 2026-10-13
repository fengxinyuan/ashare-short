"""个股相对宽基指数的超额涨跌读数，纯计算，无 I/O。

个股与基准按共同交易日对齐，以 close / prev_close - 1 计算收益。
结果保留实际覆盖天数；强弱标签使用规则阈值，不直接给出交易动作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.numeric import positive
from ashare_decision_support.signals.series import bar_day

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

# **代码格式必须和日线适配器一致（`000001.SH`），不是新浪那套前缀式（`sh000001`）。**
# 这四个代码唯一的去处是 `BenchmarkDailyAgent` → `fetch_daily_bars`，也就是取日线的
# 那条路；`broad_index.BROAD_INDICES` 里那份 `sh000001` 写法喂的是新浪的**行情**接口，
# 两者不是同一个命名空间。原来这里照抄了新浪那份，于是 TickFlow 对四个基准全部返回
# 0 根——`usable` 把它们过滤干净，`advise` 那句 `benchmark_daily.get(resolved[0], [])`
# 拿到空列表，相对强度整层弃权。取数**成功**、日志**不报错**，只有一行
# 「相对强度缺少宽基日线」，这是这类错误最难发现的形状。
BENCHMARKS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("688", "689"), "000688.SH", "科创50"),
    (("300", "301"), "399006.SZ", "创业板指"),
    (("600", "601", "603", "605"), "000001.SH", "上证指数"),
    (("000", "001", "002", "003"), "399001.SZ", "深证成指"),
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
        day = bar_day(bar)
        value = positive(bar.get("close"))
        if day is None or value is None:
            continue
        result[day] = value
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
