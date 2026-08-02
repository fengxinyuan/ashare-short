"""止跌确认：低吸三层里的第三层，回答「什么时候买」。

前两层由日线回答——趋势还在吗（L1）、回调到位了吗（L2）。但**回调到位不等于
可以买**：下跌途中的任何一点都「到位过」，接刀和低吸的区别只在于跌势有没有停。

所以进场需要一个盘中的确认信号。没有它，「进入买入区间」这条告警在下跌趋势里
会连着触发一路向下，而每一次看起来都像机会。

三种形态，任一成立即算确认：

| 形态 | 判据 | 说的是 |
|---|---|---|
| **缩量止跌** | 近端均量明显萎缩，且不再创新低 | 卖压自然衰竭，没人急着出了 |
| **放量反包** | 收盘越过前段高点，且成交量放大 | 有资金主动买回来 |
| **二次探底不破** | 回到前低附近又收回，低点抬高 | 前低被测试过并守住了 |

**判据全部用相对量，不用绝对阈值**——和 `signals/dipbuy.py` 的理由一样：同一个
「成交量 1000 手」对不同标的意义完全不同。

这里只做确认，不给价位。价位是 `advice/engine.py` 的事。
"""

from __future__ import annotations

from dataclasses import dataclass

# 近端窗口：用最后这么多根分钟线判断「现在」的状态。
RECENT_BARS = 10

# 基线窗口：近端之前的这么多根，作为对比基准。
BASELINE_BARS = 20

# 缩量判定。近端均量掉到基线均量的这个比例以下算明显萎缩。
CONTRACTION_RATIO = 0.7

# 放量反包的量能倍数与回看根数。
REVERSAL_VOLUME_RATIO = 1.5
REVERSAL_LOOKBACK = 5

# 二次探底认定「回到前低附近」的容差。
RETEST_TOLERANCE = 0.01

PATTERN_CONTRACTION = "缩量止跌"
PATTERN_REVERSAL = "放量反包"
PATTERN_RETEST = "二次探底不破"


@dataclass(frozen=True)
class Stabilisation:
    """一只票在当下有没有出现止跌信号。

    `confirmed=False` 且 `reason` 非空表示「看过了，还没有」；`insufficient=True`
    表示「数据不够，判断不了」。两者必须分开——把「不知道」当成「没有」会让数据
    源出问题时静默地不再产生任何买点。
    """

    confirmed: bool = False
    pattern: str | None = None
    reason: str = ""
    insufficient: bool = False
    volume_contraction: float | None = None
    bars_since_low: int | None = None

    def describe(self) -> str:
        if self.insufficient:
            return "分时数据不足，无法判断是否止跌"
        if self.confirmed:
            return f"{self.pattern}：{self.reason}"
        return f"尚未出现止跌信号（{self.reason}）" if self.reason else "尚未出现止跌信号"


def assess_stabilisation(bars: list[dict]) -> Stabilisation:
    """从分钟线判断跌势有没有停。任何情况下不抛异常。"""
    rows = _clean(bars)
    if len(rows) < RECENT_BARS + BASELINE_BARS:
        return Stabilisation(
            insufficient=True,
            reason=f"仅 {len(rows)} 根分钟线，需要 {RECENT_BARS + BASELINE_BARS} 根",
        )

    recent = rows[-RECENT_BARS:]
    baseline = rows[-(RECENT_BARS + BASELINE_BARS) : -RECENT_BARS]

    contraction = _contraction(recent, baseline)
    since_low = _bars_since_low(rows)

    reversal = _reversal(rows)
    if reversal is not None:
        return Stabilisation(
            confirmed=True, pattern=PATTERN_REVERSAL, reason=reversal,
            volume_contraction=contraction, bars_since_low=since_low,
        )

    retest = _retest(rows)
    if retest is not None:
        return Stabilisation(
            confirmed=True, pattern=PATTERN_RETEST, reason=retest,
            volume_contraction=contraction, bars_since_low=since_low,
        )

    # 缩量止跌放最后判：它是三者里最弱的信号，能被更明确的形态取代时就不该用它。
    making_new_lows = since_low is not None and since_low < RECENT_BARS // 2
    if contraction is not None and contraction <= CONTRACTION_RATIO and not making_new_lows:
        return Stabilisation(
            confirmed=True,
            pattern=PATTERN_CONTRACTION,
            reason=f"近端均量降至基线的 {contraction:.0%}，且 {since_low} 根未创新低",
            volume_contraction=contraction,
            bars_since_low=since_low,
        )

    if making_new_lows:
        reason = f"{since_low} 根前刚创新低，跌势未止"
    elif contraction is None:
        reason = "无成交量数据"
    else:
        reason = f"近端均量为基线的 {contraction:.0%}，抛压未见衰竭"
    return Stabilisation(
        confirmed=False, reason=reason,
        volume_contraction=contraction, bars_since_low=since_low,
    )


def _reversal(rows: list[dict]) -> str | None:
    """放量反包：最新一根收在前段高点之上，且量能放大。"""
    if len(rows) < REVERSAL_LOOKBACK + 2:
        return None
    last = rows[-1]
    prior = rows[-(REVERSAL_LOOKBACK + 1) : -1]
    prior_high = max(row["high"] for row in prior)
    prior_volume = _mean([row["volume"] for row in prior])
    if not prior_volume or last["close"] <= prior_high:
        return None
    ratio = last["volume"] / prior_volume
    if ratio < REVERSAL_VOLUME_RATIO:
        return None
    return f"收 {last['close']:.2f} 越过前 {REVERSAL_LOOKBACK} 根高点，量能 {ratio:.1f} 倍"


def _retest(rows: list[dict]) -> str | None:
    """二次探底不破：回到前低附近又收回，且低点抬高。"""
    if len(rows) < RECENT_BARS * 2:
        return None
    recent = rows[-RECENT_BARS:]
    earlier = rows[: -RECENT_BARS]

    prior_low = min(row["low"] for row in earlier)
    recent_low = min(row["low"] for row in recent)
    if prior_low <= 0:
        return None

    # 探到了前低附近（容差内），但没有跌破，且当前价已收回。
    approached = recent_low <= prior_low * (1.0 + RETEST_TOLERANCE)
    held = recent_low >= prior_low
    recovered = rows[-1]["close"] > recent_low * (1.0 + RETEST_TOLERANCE / 2)
    if approached and held and recovered:
        return f"回踩 {recent_low:.2f} 未破前低 {prior_low:.2f}，已收回至 {rows[-1]['close']:.2f}"
    return None


def _contraction(recent: list[dict], baseline: list[dict]) -> float | None:
    base = _mean([row["volume"] for row in baseline])
    now = _mean([row["volume"] for row in recent])
    if not base or now is None:
        return None
    return round(now / base, 4)


def _bars_since_low(rows: list[dict]) -> int | None:
    """距离最低那根过了几根。刚创新低说明跌势还在走。"""
    if not rows:
        return None
    lows = [row["low"] for row in rows]
    index = min(range(len(lows)), key=lambda i: lows[i])
    return len(lows) - 1 - index


def _clean(bars: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for bar in bars or []:
        try:
            row = {
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "volume": float(bar.get("volume") or 0.0),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if row["close"] > 0:
            rows.append(row)
    return rows


def _mean(values: list[float]) -> float | None:
    usable = [value for value in values if value > 0]
    if not usable:
        return None
    return sum(usable) / len(usable)
