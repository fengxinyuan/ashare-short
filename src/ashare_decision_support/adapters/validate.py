"""数据校验：跨源比对、量纲、浮点卫生。

这一层管的是**错了不会报错、只会算错**的那类问题。它们的共同特征是：出错时链路
照跑、简报照出、数字看着完全正常，只有拿另一路数据比一比才看得出来。

## 为什么需要跨源比对

`CompositeMarketScanAdapter` 现在的做法是**合并**：主源整行为准，兜底只填空。
合并是对的（逐字段挑选会造出一份哪一路都不等于的混合读数），但它有个盲区——
**两路都有的字段从来没被比过**。主源的某个字段悄悄换了量纲、喂了昨天的数、
或者标的映射错位，合并会照单全收，而兜底那一路明明就摆在旁边。

`docs/DATA_SOURCES.md` 写着「两路逐字段吻合到新浪的取整精度，可以互为校验」——
那是一次手工实测的结论，不是代码里在做的事。这个模块把它变成代码在做的事。

## 量纲差异不是「两路不一致」

比对时必须把这两件事分开报：

- **比值 ≈ 100 或 10000** → 有一边的量纲错了（手/股、元/万元）。这是 bug，
  有确定的修法，且**修完两边就完全一致**。
- **比值 ≈ 1 但差了几个百分点** → 两个源对同一件事的测量确实不同（不同厂商
  把逐笔归并到分钟的边界处理不同，实测相对差最大 0.31%）。这是数据本身的
  性质，没有「正确答案」。

混在一起报会把唯一可修的那类埋进噪声里。实测记录在案的三处量纲陷阱
（`docs/DATA_SOURCES.md` §五）全都是 100 倍或 10000 倍。

## 契约

**所有函数都不抛异常，且从不修改传入对象。** 校验失败一律置空并标注，
绝不用低分辨率数据顶替——和全仓其余地方同一条约定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# A 股最小报价单位是 0.01，4 位小数远够用。取到这个精度之后，复权算出来的
# 1e-13 量级倒挂会归零。
PRICE_DECIMALS = 4

# 相对差超过这个值才算「两路不一致」。实测两家分钟线重叠部分相对差中位 ~1e-4、
# 最大 0.31%，所以 0.5% 以内属于正常的厂商口径差异，不该报。
DEFAULT_TOLERANCE = 0.005

# 已知的量纲倍数。比值落在这些数附近说明有一边搞错了单位，而不是数据有分歧。
#   100   手 vs 股（volume）、比值 vs 百分数（change_pct）
#   10000 元 vs 万元（流通市值）
KNOWN_SCALES: tuple[float, ...] = (100.0, 10000.0)

# 判定比值「贴着」某个量纲倍数的相对宽容度。开得比 DEFAULT_TOLERANCE 松，
# 因为两边除了量纲之外本来就还有正常的测量差。
SCALE_TOLERANCE = 0.02


@dataclass(frozen=True)
class Dispute:
    """一处两路对不上的字段。"""

    key: str
    field: str
    primary: float
    fallback: float
    ratio: float
    # 认出来的量纲倍数；None 表示这是真的读数分歧，不是单位问题。
    scale: float | None = None

    @property
    def is_unit_bug(self) -> bool:
        return self.scale is not None

    def describe(self) -> str:
        if self.scale is not None:
            return (
                f"{self.key} 的 {self.field} 两路差 {self.scale:.0f} 倍"
                f"（{self.primary:.6g} vs {self.fallback:.6g}）——量纲不一致，不是读数分歧"
            )
        return (
            f"{self.key} 的 {self.field} 两路不一致："
            f"{self.primary:.6g} vs {self.fallback:.6g}（相对差 {abs(self.ratio - 1):.2%}）"
        )


@dataclass
class CrossCheck:
    """一次跨源比对的结果。"""

    compared: int = 0
    agreed: int = 0
    disputes: list[Dispute] = field(default_factory=list)

    @property
    def unit_bugs(self) -> list[Dispute]:
        return [item for item in self.disputes if item.is_unit_bug]

    @property
    def reading_gaps(self) -> list[Dispute]:
        return [item for item in self.disputes if not item.is_unit_bug]

    def notes(self, max_examples: int = 3) -> list[str]:
        """给简报的说明。**没有分歧时也出一条**——「比过了，一致」和「根本没比」
        是两回事，而它们在只报异常的设计里长得一模一样。"""
        if not self.compared:
            return []

        out: list[str] = []
        bugs, gaps = self.unit_bugs, self.reading_gaps
        if bugs:
            # 量纲错误单独且优先报：它是唯一有确定修法的一类。
            out.append(
                f"⚠️ 跨源校验发现 {len(bugs)} 处**量纲**不一致，"
                f"这类是 bug 不是数据分歧：" + "；".join(d.describe() for d in bugs[:max_examples])
            )
        if gaps:
            out.append(
                f"跨源校验：{len(gaps)}/{self.compared} 处读数超出 "
                f"{DEFAULT_TOLERANCE:.1%} 容差（属厂商口径差异，非错误）："
                + "；".join(d.describe() for d in gaps[:max_examples])
            )
        if not self.disputes:
            out.append(f"跨源校验：{self.compared} 处字段两路一致")
        return out


def cross_check(
    primary: dict[str, dict[str, Any]],
    fallback: dict[str, dict[str, Any]],
    fields: Iterable[str],
    tolerance: float = DEFAULT_TOLERANCE,
) -> CrossCheck:
    """比对两路都有的字段。只看交集——一路独有的字段没有比对对象。

    两个输入都是 `{标的: {字段: 值}}`。返回的 `CrossCheck` 里，量纲差异与读数
    分歧是**分开的两类**，理由见模块开头。

    刻意**不修正、不挑选**：这个函数只回答「两路一不一致」。合并策略归
    `CompositeMarketScanAdapter`，把两件事混在一起会让「用了哪一路的数」
    变得不可追溯。
    """
    result = CrossCheck()
    keys = set(primary) & set(fallback)
    for key in sorted(keys):
        left, right = primary.get(key) or {}, fallback.get(key) or {}
        if not isinstance(left, dict) or not isinstance(right, dict):
            continue
        for name in fields:
            a, b = _number(left.get(name)), _number(right.get(name))
            if a is None or b is None or a == 0 or b == 0:
                # 缺值不是分歧。零值排除是因为比值在零附近没有意义，
                # 而「一边是 0 一边不是」由上游的缺值处理管。
                continue
            result.compared += 1
            ratio = a / b
            if abs(ratio - 1.0) <= tolerance:
                result.agreed += 1
                continue
            result.disputes.append(
                Dispute(key=key, field=name, primary=a, fallback=b, ratio=ratio, scale=_scale_of(ratio))
            )
    return result


def _scale_of(ratio: float) -> float | None:
    """比值是不是贴着某个已知量纲倍数（两个方向都算）。"""
    for scale in KNOWN_SCALES:
        for candidate in (scale, 1.0 / scale):
            if candidate and abs(ratio / candidate - 1.0) <= SCALE_TOLERANCE:
                return scale if candidate >= 1 else -scale
    return None


# ---- 浮点卫生 -------------------------------------------------------------


@dataclass
class Sanitised:
    bars: list[dict]
    inverted: int = 0
    notes: list[str] = field(default_factory=list)


def sanitise_ohlc(bars: list[dict]) -> Sanitised:
    """把 OHLC 取到报价精度，消掉复权带来的 `high < low` 倒挂。

    实测 60 只 / 15000 根 1 分钟线里 **1399 根（9.33%）满足 `high < low`**，
    倒挂量级 1e-13（例如 high=1350.599999999988、low=1350.6）。复权对四个字段
    各自算，尾巴不一样，倒挂就出来了。

    下游拿 `high - low` 当振幅会得到负数，拿 `low == min(lows)` 找枢轴点会漏掉
    真正的低点——**两者都不会报错**。这就是必须在入口统一处理的理由。

    倒挂在取整之后仍然存在的（真正的数据错误，不是浮点尾巴），交换 high/low
    并计数：一根 high/low 写反的 bar 比一根缺失的 bar 危险得多。
    """
    out: list[dict] = []
    inverted = 0
    for bar in bars or []:
        row = dict(bar)
        for name in ("open", "high", "low", "close"):
            value = _number(row.get(name))
            if value is not None:
                row[name] = round(value, PRICE_DECIMALS)
        high, low = _number(row.get("high")), _number(row.get("low"))
        if high is not None and low is not None and high < low:
            row["high"], row["low"] = low, high
            inverted += 1
        out.append(row)

    notes: list[str] = []
    if inverted:
        notes.append(
            f"{inverted}/{len(out)} 根 K 线取到 {PRICE_DECIMALS} 位小数后仍 high<low，"
            "已交换高低价——这不是浮点尾巴，是上游数据错误"
        )
    return Sanitised(bars=out, inverted=inverted, notes=notes)


# ---- 量纲 -----------------------------------------------------------------


def infer_volume_unit(
    volume: float | None, amount: float | None, price: float | None
) -> tuple[float | None, str]:
    """从 `amount / volume` 反推成交量的单位，返回 (换算成股的乘数, 说明)。

    **能推出来就不要去配置里读。** 单位是可以自证的：`amount / volume` 必须落在
    当根 bar 的价格量级上。落在 100 倍开外，说明 volume 记的是手。

    这比信一个配置项可靠——量纲搞错不会报错，只会让茅台的 VWAP 算成 135083
    而不是 1351，然后每只票都显示为永远在均价下方。

    推不出来时返回 `(None, 原因)`，调用方**不要猜**：宁可不给 VWAP，也不要给一个
    可能差 100 倍的。
    """
    if not volume or not amount or volume <= 0 or amount <= 0:
        return None, "缺成交额或成交量，无法反推单位"
    if not price or price <= 0:
        return None, "缺参考价，无法反推单位"

    implied = amount / volume
    for multiplier, label in ((1.0, "股"), (100.0, "手"), (10.0, "十股")):
        # volume 若以 `label` 为单位，换算成股之后隐含均价应当贴近参考价。
        if _close_enough(implied / multiplier, price):
            return multiplier, f"成交量单位判定为{label}（隐含均价 {implied / multiplier:.2f}）"
    return None, f"隐含均价 {implied:.2f} 与参考价 {price:.2f} 对不上任何已知单位，拒绝推导 VWAP"


def _close_enough(implied: float, price: float, tolerance: float = 0.35) -> bool:
    """均价与参考价的宽容度开得大，是因为它们本来就不该相等。

    参考价通常是收盘价或现价，隐含均价是全时段的加权平均——一根振幅 10% 的
    bar 里两者差几个百分点很正常。这里要分辨的是 1 倍与 100 倍，不是 1.0 与 1.05，
    所以宁可开松：开紧了会把正常的波动误判成量纲错误，那比漏判更糟。
    """
    if price <= 0:
        return False
    return abs(implied / price - 1.0) <= tolerance


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None
