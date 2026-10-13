"""跨源读数校验与成交量单位常量。

CompositeMarketScanAdapter 合并行情时调用 cross_check，将已知量纲倍数、
大额分歧和普通厂商差异分别报告。只检查两路共有的有限数，不修改输入或修补价格。
各适配器负责按来源换算单位，分钟线价格精度由 tickflow_intraday.to_bars 处理。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.numeric import finite

# 相对差超过这个值才算「两路不一致」。实测两家分钟线重叠部分相对差中位 ~1e-4、
# 最大 0.31%，所以 0.5% 以内属于正常的厂商口径差异，不该报。
DEFAULT_TOLERANCE = 0.005

# A 股一手 = 100 股。TickFlow 的成交量与盘口量、腾讯的成交量与五档挂单量都记
# 「手」，新浪记「股」；各适配器在解析层换算成股，下游只认一种单位。
LOTS_TO_SHARES = 100.0

# 已知的量纲倍数。比值落在这些数附近说明有一边搞错了单位，而不是数据有分歧。
#   100   手 vs 股（volume）、比值 vs 百分数（change_pct）
#   10000 元 vs 万元（流通市值）
KNOWN_SCALES: tuple[float, ...] = (LOTS_TO_SHARES, 10000.0)

# 判定比值「贴着」某个量纲倍数的相对宽容度。开得比 DEFAULT_TOLERANCE 松，
# 因为两边除了量纲之外本来就还有正常的测量差。
SCALE_TOLERANCE = 0.02


# 读数分歧超过这个相对差就不能再叫「厂商口径差异」。实测两家分钟线重叠部分相对差
# 中位 ~1e-4、最大 0.31%，DEFAULT_TOLERANCE 的 0.5% 正是照这个量级定的。

# 2026-08-02 全市场实测：179 处分歧里有 21 处超过 10%，最大 77.7%，全部落在
# `float_cap` 上（多半是一边的股本数在增发/解禁后没更新）。把这些和 0.6% 的舍入
# 差一起写成「属厂商口径差异，非错误」，等于让简报**主动叫读者忽略一个真的错**——
# 而 `float_cap` 恰好是 `screens/LiquidityScreen` 用来放行或否决候选的那个字段，
# 差一倍就是候选里多一只或少一只。
MATERIAL_GAP = 0.10


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

    @property
    def relative_gap(self) -> float:
        return abs(self.ratio - 1.0)

    @property
    def is_material(self) -> bool:
        """大到不能用「厂商口径不同」解释。见 `MATERIAL_GAP`。"""
        return self.scale is None and self.relative_gap >= MATERIAL_GAP

    def describe(self) -> str:
        if self.scale is not None:
            return (
                f"{self.key} 的 {self.field} 两路差 {self.scale:.0f} 倍"
                f"（{self.primary:.6g} vs {self.fallback:.6g}）——量纲不一致，不是读数分歧"
            )
        return (
            f"{self.key} 的 {self.field} 两路不一致："
            f"{self.primary:.6g} vs {self.fallback:.6g}（相对差 {self.relative_gap:.2%}）"
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
    def material_gaps(self) -> list[Dispute]:
        """大到必须有人看一眼的分歧，从大到小排。"""
        return sorted(
            (item for item in self.disputes if item.is_material),
            key=lambda item: item.relative_gap,
            reverse=True,
        )

    @property
    def reading_gaps(self) -> list[Dispute]:
        """能用厂商口径差异解释的那些。"""
        return [item for item in self.disputes if not item.is_unit_bug and not item.is_material]

    def notes(self, max_examples: int = 3) -> list[str]:
        """给简报的说明。**没有分歧时也出一条**——「比过了，一致」和「根本没比」
        是两回事，而它们在只报异常的设计里长得一模一样。

        分三档报，因为三档的处置完全不同：量纲错有确定修法；大额分歧要人去判断
        信哪一路；小额分歧是数据本身的性质，看一眼就行。
        """
        if not self.compared:
            return []

        out: list[str] = []
        bugs, material, gaps = self.unit_bugs, self.material_gaps, self.reading_gaps
        if bugs:
            # 量纲错误单独且优先报：它是唯一有确定修法的一类。
            out.append(
                f"⚠️ 跨源校验发现 {len(bugs)} 处**量纲**不一致，"
                f"这类是 bug 不是数据分歧：" + "；".join(d.describe() for d in bugs[:max_examples])
            )
        if material:
            fields = "、".join(dict.fromkeys(d.field for d in material))
            out.append(
                f"⚠️ 跨源校验发现 {len(material)}/{self.compared} 处**大额分歧**"
                f"（相对差 ≥ {MATERIAL_GAP:.0%}，最大 {material[0].relative_gap:.0%}，"
                f"集中在 {fields}）——这个量级不能用厂商口径解释，两路必有一路是错的，"
                "涉及的标的读数不可信：" + "；".join(d.describe() for d in material[:max_examples])
            )
        if gaps:
            out.append(
                f"跨源校验：{len(gaps)}/{self.compared} 处读数在 "
                f"{DEFAULT_TOLERANCE:.1%}~{MATERIAL_GAP:.0%} 之间（属厂商口径差异，非错误）："
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
            a, b = finite(left.get(name)), finite(right.get(name))
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
                Dispute(
                    key=key, field=name, primary=a, fallback=b, ratio=ratio, scale=_scale_of(ratio)
                )
            )
    return result


def _scale_of(ratio: float) -> float | None:
    """比值是不是贴着某个已知量纲倍数（两个方向都算）。"""
    for scale in KNOWN_SCALES:
        for candidate in (scale, 1.0 / scale):
            if candidate and abs(ratio / candidate - 1.0) <= SCALE_TOLERANCE:
                return scale if candidate >= 1 else -scale
    return None
