"""Buy/sell levels and forward scenarios for individual symbols.

This covers two of the four working needs: *where would I act on this name*
and *what happens next*. Both answers are level-based, never probabilistic —
the engine has no fitted model, so a number like "68% chance of continuation"
would be invented. What it can state honestly is: here is the price that
changes the picture, and here is what it means if it trades there.

Price-scale reconciliation matters here more than anywhere else in the system.
Daily bars are **比例前复权**, minute bars are raw — and those two turn out to
already share one price scale, because 比例前复权 anchors on the *latest* bar:
it leaves today untouched and rescales history. So a support formed before an
ex-dividend date arrives already expressed in today's money, which is exactly
the number you would quote on an order. 2026-08-02 实测 400 只，前复权序列的最
新一根与不复权最新一根 **无一例外完全相同**（茅台两边都是 1350.60），而 60 天
前那一根差 2.3%——差的正是应该差的那部分。

This is worth stating because the opposite is easy to assume and would be
actively harmful: it is **后复权** that leaves history raw and rescales today,
and only under 后复权 would published levels need converting. Under 比例前复权
they need nothing. `_session_alignment` therefore exists for a different reason
entirely — see its docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.models.schemas import DailyTechnicalSnapshot, WyckoffSignal
from ashare_decision_support.signals.intraday import IntradaySnapshot

# Fraction of the session range used as a buffer under a stop, so a level is
# not placed exactly where everyone else's stop sits.
STOP_BUFFER = 0.15

# Half-width of the entry band, as a fraction of the session range. Wide enough
# to be fillable, narrow enough that the stop below it stays meaningful.
ENTRY_BAND_RATIO = 0.12

# A stop this close to the entry is inside normal intraday noise.
MIN_STOP_DISTANCE_PCT = 0.008


@dataclass
class Scenario:
    name: str
    trigger: str
    implication: str
    level: float | None = None


@dataclass
class StockAdvice:
    symbol: str
    name: str | None = None
    score: float | None = None
    last_price: float | None = None
    session_date: str | None = None
    is_current_session: bool = False
    trend_state: str | None = None
    wyckoff_bias: str | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    targets: list[float] = field(default_factory=list)
    rrr: float | None = None
    scenarios: list[Scenario] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    intraday: dict[str, Any] = field(default_factory=dict)

    @property
    def has_actionable_levels(self) -> bool:
        return self.entry_low is not None and self.stop is not None


class AdviceEngine:
    """Turns daily structure plus intraday behaviour into levels and scenarios."""

    def build(
        self,
        symbol: str,
        daily: DailyTechnicalSnapshot | None,
        intraday: IntradaySnapshot | None,
        wyckoff: WyckoffSignal | None = None,
        name: str | None = None,
        score: float | None = None,
    ) -> StockAdvice:
        advice = StockAdvice(symbol=symbol, name=name, score=score)
        if daily is None and intraday is None:
            advice.warnings.append("既无日线也无分钟线数据，无法给出任何判断")
            return advice

        if daily is not None:
            advice.trend_state = daily.trend_state.value
        if wyckoff is not None:
            advice.wyckoff_bias = wyckoff.bias.value

        if intraday is None:
            advice.warnings.append("无分钟线数据，仅能给出日线结构判断，价位为前复权口径、不可直接用于挂单")
            advice.last_price = daily.last_close if daily else None
            return advice

        advice.last_price = intraday.last_price
        advice.session_date = str(intraday.session_date)
        advice.is_current_session = intraday.is_current_session
        advice.warnings.extend(intraday.notes)
        advice.intraday = _intraday_facts(intraday)

        factor = _session_alignment(daily, intraday)
        if daily is not None and factor is None:
            advice.warnings.append("日线快照与分钟线对不上（疑似不同交易日或不同标的），日线位置未纳入价位计算")

        self._levels(advice, daily, intraday, factor)
        self._scenarios(advice, daily, intraday, factor)
        self._reasons(advice, daily, intraday, wyckoff)
        return advice

    # ---- levels ----------------------------------------------------------

    def _levels(
        self,
        advice: StockAdvice,
        daily: DailyTechnicalSnapshot | None,
        intraday: IntradaySnapshot,
        factor: float | None,
    ) -> None:
        last = intraday.last_price
        vwap = intraday.vwap
        low = intraday.session_low
        high = intraday.session_high
        span = max(high - low, last * 0.005)

        # The entry band is a *pullback* zone around the session mean, not a
        # point. Anchoring both edges on VWAP collapsed it to zero width
        # whenever price sat above the mean, which is the normal case in an
        # uptrend and made the band useless exactly when it mattered.
        anchor = vwap if vwap else (low + high) / 2
        band = span * ENTRY_BAND_RATIO
        advice.entry_low = round(max(low, anchor - band), 3)
        advice.entry_high = round(min(max(anchor, advice.entry_low + band), last if last > anchor else high), 3)
        if advice.entry_high <= advice.entry_low:
            advice.entry_high = round(advice.entry_low + band, 3)

        supports = _levels_if_aligned(daily.supports if daily else [], factor)
        below = [level for level in supports if level < low]
        structural_stop = max(below) if below else None

        stop = low - span * STOP_BUFFER
        if structural_stop is not None:
            # Prefer the structural level when it is not absurdly far away.
            if (advice.entry_low - structural_stop) / advice.entry_low <= 0.09:
                stop = min(stop, structural_stop)
        advice.stop = round(stop, 3)

        resistances = _levels_if_aligned(daily.resistances if daily else [], factor)
        above = sorted(level for level in resistances if level > last)
        targets = [high] if high > last else []
        targets.extend(above[:2])
        advice.targets = [round(level, 3) for level in sorted(dict.fromkeys(targets)) if level > last]

        if not advice.targets:
            # At the session high with no overhead daily resistance there is
            # genuinely nothing to aim at. Saying so beats printing a dash.
            advice.warnings.append(
                "现价已是当日最高且上方无历史压力位，无法给出目标价——"
                "属于无参照的追高位置，只能用移动止损跟随"
            )

        if advice.entry_low and advice.stop and advice.targets:
            downside = advice.entry_low - advice.stop
            upside = advice.targets[-1] - advice.entry_low
            if downside > 0 and upside > 0:
                advice.rrr = round(upside / downside, 2)

        if advice.stop and advice.entry_low:
            distance = (advice.entry_low - advice.stop) / advice.entry_low
            if distance < MIN_STOP_DISTANCE_PCT:
                advice.warnings.append(
                    f"止损距离仅 {distance:.2%}，处于日内噪声范围内，容易被无意义扫损"
                )
        if advice.rrr is not None and advice.rrr < 1.5:
            advice.warnings.append(f"盈亏比仅 {advice.rrr}，不满足超短线最低要求")

    # ---- scenarios -------------------------------------------------------

    def _scenarios(
        self,
        advice: StockAdvice,
        daily: DailyTechnicalSnapshot | None,
        intraday: IntradaySnapshot,
        factor: float | None,
    ) -> None:
        vwap = intraday.vwap
        high = intraday.session_high
        low = intraday.session_low

        advice.scenarios.append(
            Scenario(
                name="强势延续",
                trigger=f"放量站上当日高点 {high:.2f}",
                implication="日内多头占优，可沿分时均价持有；跌回均价下方即结束该情景",
                level=round(high, 3),
            )
        )
        if vwap:
            advice.scenarios.append(
                Scenario(
                    name="均价震荡",
                    trigger=f"在分时均价 {vwap:.2f} 与当日高点 {high:.2f} 之间反复",
                    implication="方向未定，只做回均价的低吸，不追高",
                    level=round(vwap, 3),
                )
            )
            advice.scenarios.append(
                Scenario(
                    name="转弱回落",
                    trigger=f"有效跌破分时均价 {vwap:.2f} 且不能快速收回",
                    implication=f"下看当日低点 {low:.2f}；跌破则当日结构破坏，离场不留",
                    level=round(vwap, 3),
                )
            )
        else:
            advice.scenarios.append(
                Scenario(
                    name="转弱回落",
                    trigger=f"跌破当日低点 {low:.2f}",
                    implication="当日结构破坏，离场不留",
                    level=round(low, 3),
                )
            )

    # ---- narrative -------------------------------------------------------

    def _reasons(
        self,
        advice: StockAdvice,
        daily: DailyTechnicalSnapshot | None,
        intraday: IntradaySnapshot,
        wyckoff: WyckoffSignal | None,
    ) -> None:
        if daily is not None:
            advice.reasons.append(f"日线趋势 {daily.trend_state.value}")
            if daily.volume_ratio_20d is not None:
                advice.reasons.append(f"日线量比 {daily.volume_ratio_20d:.2f}")
            if daily.range_position_60d is not None:
                advice.reasons.append(f"60日分位 {daily.range_position_60d:.2f}")
        if wyckoff is not None:
            advice.reasons.append(f"威科夫阶段 {wyckoff.bias.value}（结构分 {wyckoff.score:.0f}）")

        if intraday.price_vs_vwap_pct is not None:
            side = "上方" if intraday.price_vs_vwap_pct >= 0 else "下方"
            advice.reasons.append(f"现价位于分时均价{side} {abs(intraday.price_vs_vwap_pct):.2%}")
        if intraday.opening_drive_pct is not None:
            advice.reasons.append(f"开盘 30 分钟 {intraday.opening_drive_pct:+.2%}")
        if intraday.range_position is not None:
            advice.reasons.append(f"日内位置 {intraday.range_position:.0%}（0=最低 1=最高）")
        if intraday.volume_ratio is not None:
            advice.reasons.append(f"分钟量能比前 5 日 {intraday.volume_ratio:.2f}")
            if intraday.volume_ratio > 3.0:
                advice.warnings.append(f"成交量为近 5 日均量的 {intraday.volume_ratio:.2f} 倍，注意情绪透支")


def _intraday_facts(intraday: IntradaySnapshot) -> dict[str, Any]:
    return {
        "session_date": str(intraday.session_date),
        "is_current_session": intraday.is_current_session,
        "open": intraday.open_price,
        "high": intraday.session_high,
        "low": intraday.session_low,
        "vwap": intraday.vwap,
        "price_vs_vwap_pct": intraday.price_vs_vwap_pct,
        "price_vs_open_pct": intraday.price_vs_open_pct,
        "opening_drive_pct": intraday.opening_drive_pct,
        "closing_drive_pct": intraday.closing_drive_pct,
        "range_position": intraday.range_position,
        "volume_ratio": intraday.volume_ratio,
        "bar_count": intraday.bar_count,
    }


def _session_alignment(
    daily: DailyTechnicalSnapshot | None,
    intraday: IntradaySnapshot,
) -> float | None:
    """日线快照与分钟线是不是**同一只票的同一个交易日**。

    历史上这个函数叫 `_adjustment_factor`，声称把前复权价换算回原始价空间。
    它从来没做过那件事，而且也不需要做：比例前复权锚定最新一根，日线序列本来
    就和分钟线共用今天的价格尺度。2026-08-02 实测 400 只，这个比值偏离 1.0 超过
    0.1% 的有 **0 只**——它恒等于 1.0。

    但它并非无用。恒等于 1.0 正是它的价值所在：**一旦不等于 1.0，就说明两份数据
    对不上**——取错了标的、日线停在上一个交易日而分钟线是今天、或者某一路取数
    退化成了默认值。这时候宁可不发价位，也不能把两个不同交易日的数据拼成一组
    挂单价。0.2~5.0 的窗口开得很宽，是因为它要抓的是「根本对不上」，不是微小误差。

    返回值仍是那个比值而不是布尔量，好让调用方按原样乘一遍——万一将来数据源换成
    后复权口径，这里就是唯一需要改的地方，而乘法本身已经在正确的位置上了。
    """
    if daily is None or not daily.last_close or not intraday.last_price:
        return None
    factor = intraday.last_price / daily.last_close
    if not 0.2 <= factor <= 5.0:
        return None
    return factor


def _levels_if_aligned(levels: list, factor: float | None) -> list[float]:
    """对不上就一个价位都不发。缺价位比发错价位安全。"""
    if factor is None:
        return []
    values: list[float] = []
    for level in levels:
        price = getattr(level, "price", None)
        if isinstance(price, (int, float)) and price > 0:
            values.append(price * factor)
    return values
