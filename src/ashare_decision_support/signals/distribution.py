"""派发 / 洗盘判别：同一根下跌 K 线的两种解释。

## 它回答的问题

「主力出货还是压价洗盘」——这是持仓者在回调里唯一真正要回答的问题，也是整套系统
此前答不了的问题。`signals/wyckoff.py` 给阶段（DISTRIBUTION/ACCUMULATION），
`signals/volume_price.py` 给当日四象限，但没有一层把它们和相对强度、位置合起来
给出**带证据链的判别**。

`docs/ROADMAP.md` 224-296 已经把形态序列设计好了：

```text
放量上涨 → 缩量下跌 → 缩量下跌 → 缩量下跌收阳     洗盘结束
放量上涨 → 放量滞涨 → 放量下跌                     派发
放量下跌 → 放量下跌                                 加速出货
```

这一层做的是把那张表变成可执行的判据，并补上 ROADMAP 里没有的两个维度——
**相对强度**与**日内供给**。

## 判别的骨架

洗盘和派发在**价格**上看起来一样（都在跌），区别全在别的维度：

| 维度 | 洗盘 | 派发 |
|---|---|---|
| 量能 | 缩量回调——卖压在衰竭 | 放量下跌 / 放量滞涨 |
| 涨跌量比 | 上涨日的量大于下跌日 | 下跌日的量更大 |
| 相对强度 | 跟跌不多，或仍跑赢 | 持续跑输，尤其在指数反弹日 |
| 位置 | 区间下沿、均线附近 | 高位、刚见过天量 |
| 日内 | 砸下去能收回均价 | 全天贴在均价下方 |

## 三条硬约束

1. **必须能说「不判别」。** 证据不够或两边打平时给 `UNDECIDED`。在一个二选一的
   问题上强行选边，是这类判别最典型的害人方式——它会把「不知道」包装成「知道」。

2. **它不是预测，也不出动作。** 「派发」说的是已经发生的量价与筹码结构，不是
   「明天会跌」。系统里没有拟合模型，给不出概率（见 `advice/` 的纪律提醒）。
   动作留给 `strategy/playbook.py` 与人。

3. **权重是先验设定。** 下面每条判据的分值都是超短交易里的经验刻度，**没有回测
   支持**，也不是从本系统台账标定的。它们的作用是让「这更像派发」这句话有一个
   可复核的算式，而不是精确。等 `journal/stats.py` 攒够样本再谈调参。

## 实战复盘：这一层是怎么长出来的

2026-08-04 牧原股份。人工分析当时给了五条看空证据，事后有两条被数据推翻：

- 「放量破位，量能 2.36 倍」→ 收盘实际 **1.09 倍**。`signals/intraday.py` 的
  量比按 `elapsed/240` 线性折算基线，早盘系统性高估（已修）。
- 「逆势跑输指数 4.7 个点」→ 放进 20 日窗口看，牧原相对深证成指超额 **+13.4%**
  （指数 −12.77%，个股 +0.65%）。它是那一个月的避险品种，8/4 只是指数反弹日的
  风格换仓。**单日相对强度脱离多日背景会读反。**

所以这里的相对强度判据**同时看 1 日与 20 日**，且当两者方向相反时不给分、只留
一条 counter-evidence。那次误判的根源就在这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.signals.volume_price import (
    DIVERGENCE_TOP,
    QUADRANT_DOWN_CONTRACT,
    QUADRANT_DOWN_EXPAND,
    QUADRANT_DOWN_FLAT,
    QUADRANT_FLAT,
    QUADRANT_UP_EXPAND,
)

VERDICT_SHAKEOUT = "洗盘"
VERDICT_DISTRIBUTION = "派发"
VERDICT_ACCELERATING = "加速出货"
VERDICT_UNDECIDED = "不判别"

# 判据全是先验设定，理由见模块 docstring。
UP_DOWN_WEAK = 0.9          # 涨跌量比低于此：下跌日的量更大
UP_DOWN_STRONG = 1.1        # 高于此：上涨日的量更大
HIGH_POSITION = 0.70        # 60 日分位高于此才谈得上「高位派发」
LOW_POSITION = 0.35         # 低于此更像区间下沿的洗盘
EXCESS_WEAK = -0.02         # 多日超额低于此算持续跑输
EXCESS_STRONG = 0.02
VWAP_PINNED = -0.005        # 收盘价低于分时均价这么多，算全天贴在均价下方

# 判决门槛：**同时**要求净分差与最低证据量。只看分差的话，1 分对 0 分也会出结论，
# 而那只是一条判据在说话。
MIN_MARGIN = 2.0
MIN_EVIDENCE = 3


@dataclass
class DistributionRead:
    """派发/洗盘判别。`verdict` 恒为四个常量之一，`UNDECIDED` 是正常输出。"""

    verdict: str = VERDICT_UNDECIDED
    distribution_points: float = 0.0
    shakeout_points: float = 0.0
    evidence: list[str] = field(default_factory=list)
    counter_evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "distribution_points": round(self.distribution_points, 2),
            "shakeout_points": round(self.shakeout_points, 2),
            "evidence": list(self.evidence),
            "counter_evidence": list(self.counter_evidence),
            "notes": list(self.notes),
        }

    def describe(self) -> list[str]:
        headline = (
            f"派发/洗盘判别：**{self.verdict}**"
            f"（派发 {self.distribution_points:.1f} 分 / 洗盘 {self.shakeout_points:.1f} 分）"
        )
        lines = [headline]
        lines.extend(f"支持：{item}" for item in self.evidence)
        lines.extend(f"反证：{item}" for item in self.counter_evidence)
        lines.extend(self.notes)
        lines.append("这是对**已经发生**的量价与筹码结构的分类，不是对明天的预测")
        return lines


def judge_distribution(
    features: Any = None,
    relative: Any = None,
    range_position_60d: float | None = None,
    price_vs_vwap_pct: float | None = None,
    wyckoff_phase: str | None = None,
    prior_quadrants: list[str] | None = None,
) -> DistributionRead:
    """把各层读数合成一个判别。每个入参都可以是 None——缺的那条不计分。

    `features` 是 `VolumePriceFeatures`，`relative` 是 `RelativeStrength`；
    这里按属性名鸭子取值，所以测试可以塞简单的替身对象。
    """
    read = DistributionRead()
    dist: list[tuple[float, str]] = []
    shake: list[tuple[float, str]] = []

    quadrant = getattr(features, "quadrant", None)
    _judge_quadrant(quadrant, features, dist, shake)
    _judge_flow(features, dist, shake)
    _judge_position(range_position_60d, quadrant, dist, shake)
    _judge_relative(relative, read, dist, shake)
    _judge_intraday(price_vs_vwap_pct, dist, shake)
    _judge_structure(features, wyckoff_phase, dist, shake)

    read.distribution_points = sum(w for w, _ in dist)
    read.shakeout_points = sum(w for w, _ in shake)
    read.evidence = [text for _, text in sorted(dist, key=lambda x: -x[0])]
    read.counter_evidence = [text for _, text in sorted(shake, key=lambda x: -x[0])]

    total = len(dist) + len(shake)
    margin = read.distribution_points - read.shakeout_points
    if total < MIN_EVIDENCE:
        read.verdict = VERDICT_UNDECIDED
        read.notes.append(f"可用判据仅 {total} 条（需要 {MIN_EVIDENCE} 条），不判别")
        return read
    if abs(margin) < MIN_MARGIN:
        read.verdict = VERDICT_UNDECIDED
        read.notes.append(f"两侧证据接近（净分差 {margin:+.1f}），不判别")
        return read

    if margin > 0:
        read.verdict = (
            VERDICT_ACCELERATING if _accelerating(prior_quadrants, quadrant) else VERDICT_DISTRIBUTION
        )
    else:
        read.verdict = VERDICT_SHAKEOUT
        # 洗盘这个判别本身就带着「还会回来」的暗示，而它恰恰是最贵的一句话。
        read.notes.append("「洗盘」只说明当前的卖压结构不像派发，不保证价格会收回")
    return read


def _judge_quadrant(quadrant, features, dist, shake) -> None:
    if quadrant == QUADRANT_DOWN_EXPAND:
        dist.append((2.0, f"{quadrant}：跌的时候量放出来了，卖压不在衰竭"))
    elif quadrant == QUADRANT_DOWN_CONTRACT:
        shake.append((2.0, f"{quadrant}：跌的时候量在缩，卖压在衰竭"))
    elif quadrant in (QUADRANT_FLAT, QUADRANT_DOWN_FLAT) and getattr(features, "climax_stall", None):
        dist.append((2.0, "天量滞涨：放巨量而收在日内下半段，高位换手"))
    elif quadrant == QUADRANT_UP_EXPAND and getattr(features, "climax", None):
        dist.append((1.0, "放量上涨但量比已达情绪透支区间"))


def _judge_flow(features, dist, shake) -> None:
    ratio = getattr(features, "up_down_vol_ratio", None)
    if ratio is None:
        return
    if ratio <= UP_DOWN_WEAK:
        dist.append((2.0, f"涨跌量比 {ratio:.2f}：下跌日的成交量大于上涨日"))
    elif ratio >= UP_DOWN_STRONG:
        shake.append((2.0, f"涨跌量比 {ratio:.2f}：上涨日的成交量仍大于下跌日"))


def _judge_position(position, quadrant, dist, shake) -> None:
    if position is None:
        return
    if position >= HIGH_POSITION and quadrant in (QUADRANT_DOWN_EXPAND, QUADRANT_FLAT, QUADRANT_DOWN_FLAT):
        dist.append((1.5, f"60 日分位 {position:.2f}：高位放量滞涨/下跌才谈得上派发"))
    elif position <= LOW_POSITION:
        shake.append((1.5, f"60 日分位 {position:.2f}：已在区间下沿，派发的空间有限"))


def _judge_relative(relative, read, dist, shake) -> None:
    """相对强度**必须同时看单日与多日**，否则会读反。

    2026-08-04 牧原就是反例：单日跑输 4.5 个点，而 20 日超额 +13.4%。只看单日会
    判成「逆势破位」，放进多日背景才看得出那是避险品种在指数反弹日的风格换仓。
    """
    excess = getattr(relative, "excess", None) or {}
    day = excess.get(1)
    month = excess.get(20) or excess.get(5)
    if day is None and month is None:
        return

    if month is not None and month <= EXCESS_WEAK:
        dist.append((2.0, f"多日相对指数超额 {month:+.1%}：持续跑输，不是单日噪声"))
    elif month is not None and month >= EXCESS_STRONG:
        shake.append((2.0, f"多日相对指数超额 {month:+.1%}：这段时间仍在跑赢"))

    if day is None or month is None:
        return
    if day <= EXCESS_WEAK and month >= EXCESS_STRONG:
        read.notes.append(
            f"单日超额 {day:+.1%} 与多日 {month:+.1%} 方向相反："
            "更像高位品种在指数反弹日被换仓，不单独作为派发证据"
        )
    elif day <= EXCESS_WEAK and month <= EXCESS_WEAK:
        dist.append((1.0, f"单日超额 {day:+.1%}，与多日弱势同向"))


def _judge_intraday(price_vs_vwap_pct, dist, shake) -> None:
    if price_vs_vwap_pct is None:
        return
    if price_vs_vwap_pct <= VWAP_PINNED:
        dist.append((1.0, f"收在分时均价下方 {abs(price_vs_vwap_pct):.2%}：日内供给持续压制"))
    elif price_vs_vwap_pct > 0:
        shake.append((1.0, "收在分时均价上方：日内砸下去能收回"))


def _judge_structure(features, wyckoff_phase, dist, shake) -> None:
    if getattr(features, "breakdown_on_volume", None):
        dist.append((1.5, "放量跌破 MA20：和缩量回踩正好相反的那一面"))
    if getattr(features, "divergence", None) == DIVERGENCE_TOP:
        dist.append((1.0, "顶背离：价创新高而量没跟上"))
    if getattr(features, "dry_up", None):
        shake.append((1.5, "地量：连续缩量，卖压已经很薄"))
    if wyckoff_phase:
        phase = str(wyckoff_phase).upper()
        if "DISTRIBUTION" in phase:
            dist.append((1.0, f"威科夫阶段 {wyckoff_phase}"))
        elif "ACCUMULATION" in phase:
            shake.append((1.0, f"威科夫阶段 {wyckoff_phase}"))


def _accelerating(prior_quadrants: list[str] | None, quadrant: str | None) -> bool:
    """连续两根放量下跌 → 加速出货。序列判据来自 ROADMAP 296 行。"""
    if quadrant != QUADRANT_DOWN_EXPAND or not prior_quadrants:
        return False
    return prior_quadrants[-1] == QUADRANT_DOWN_EXPAND
