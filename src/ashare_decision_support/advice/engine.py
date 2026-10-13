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
from math import isclose
from typing import Any, Protocol

from ashare_decision_support.advice.adjudicate import Adjudication, LevelAdjudicator, rule_default
from ashare_decision_support.advice.candidates import (
    BASIS_NOISE_FLOOR,
    BASIS_STRUCTURE_BREAK,
    CandidateSet,
    build_candidates,
)
from ashare_decision_support.models.enums import Playbook, TrendState
from ashare_decision_support.models.schemas import DailyTechnicalSnapshot, WyckoffSignal
from ashare_decision_support.numeric import finite, positive
from ashare_decision_support.signals.intraday import IntradaySnapshot

# Fraction of the session range used as a buffer under a stop, so a level is
# not placed exactly where everyone else's stop sits.
STOP_BUFFER = 0.15

# Half-width of the entry band, as a fraction of the session range. Wide enough
# to be fillable, narrow enough that the stop below it stays meaningful.
ENTRY_BAND_RATIO = 0.12

# A stop this close to the entry is inside normal intraday noise.
MIN_STOP_DISTANCE_PCT = 0.008

# 计划、问答与监控使用同一门槛；这是规则先验，不是经过收益拟合的参数。
MIN_RRR = 1.5

# 仅替换本层生成的动态价位提示，保留公告、业绩与人工风险说明。
_LEVEL_RISK_PREFIXES = (
    "现价已是当日最高且上方无历史压力位",
    "上方缺少有效目标位",
    "突破价上方没有有效目标位",
    "买入区间、止损或目标无效",
    "止损距离仅 ",
    "盈亏比仅 ",
)


@dataclass
class Scenario:
    name: str
    trigger: str
    implication: str
    level: float | None = None


@dataclass
class TradeContext:
    session_volume: float = 0.0
    session_amount: float | None = None
    turnover_ratio: float | None = None
    float_cap_yi: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_volume": self.session_volume,
            "session_amount": self.session_amount,
            "turnover_ratio": self.turnover_ratio,
            "float_cap_yi": self.float_cap_yi,
        }


@dataclass
class ShortlineView:
    bias: str
    action: str
    score: int
    summary: str
    reasons: list[str] = field(default_factory=list)
    reclaim_level: float | None = None
    support_level: float | None = None
    pressure_level: float | None = None
    defense_level: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bias": self.bias,
            "action": self.action,
            "score": self.score,
            "summary": self.summary,
            "reasons": list(self.reasons),
            "reclaim_level": self.reclaim_level,
            "support_level": self.support_level,
            "pressure_level": self.pressure_level,
            "defense_level": self.defense_level,
        }


@dataclass
class StockAdvice:
    symbol: str
    name: str | None = None
    score: float | None = None
    # 评分的确定性拆分。总分本身不能说明行情结构、资金和事件分别改变了多少，
    # 但这里绝不存模型主观归因。
    score_components: dict[str, float] = field(default_factory=dict)
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
    # 盘口读数（`signals/orderflow.py`）。取不到就是 None——五档缺失只让候选集少
    # 两类，不影响其余部分。
    book: dict[str, Any] = field(default_factory=dict)
    # 盘口读数的成稿行。存渲染好的文本而不是让简报自己拼，是因为同一句话监控
    # 那边已经在用（`OrderBookReading.describe`）——两处各写一份措辞，迟早会
    # 出现「监控说买不进、简报说可以挂单」这种自相矛盾。

    # 不进 `_facts`：它是 `book` 里那些数的复述，混进数字闸门的比对集只会凭空
    # 放宽闸门（多出「68 万元」「56%」这类换算过的数），不增加任何事实。
    book_lines: list[str] = field(default_factory=list)
    # L2 候选集与 L3 裁决。`candidates` 永远有（只要有分时数据），`adjudication`
    # 在没有裁决器时是规则默认那一份，`used_fallback=True`。
    candidates: CandidateSet | None = None
    adjudication: Adjudication | None = None
    # 入场带的宽度。存在 advice 上而不是 engine 上：一个 engine 实例会连续处理
    # 几十只票，把它挂在 self 上等于让上一只的带宽泄漏给下一只。
    entry_band_width: float | None = None
    trade_context: TradeContext | None = None
    shortline: ShortlineView | None = None
    # 打法判据（`strategy/playbook.py`）。**这一层此前不在 advise 上**：盘前主链路
    # 过打法门槛，而人主动问一只票走的是这条路，于是最常被读的那份输出反而只有一个
    # 加权分数和一组价位，答不了「凭哪条打法买」「今天为什么不能买」。

    # 这里**不做过滤**，只做标注：你点名问的票，答案是「今天没有可用打法，因为
    # ……」，不是让它从结果里消失。同一条口径见 `jobs/advise.py` 对 ST 的处理。
    setup: dict[str, Any] = field(default_factory=dict)
    playbook: str | None = None
    # 收盘量价判别（只描述已发生结构，不产生动作）。
    distribution: dict[str, Any] | None = None
    volume_price: dict[str, Any] | None = None
    # 个股相对所属宽基指数的超额收益。只作事实展示，不改变打分或动作。
    relative_strength: dict[str, Any] | None = None
    # 个股新闻与公告提取的事件类别和风险词。展示/复盘用，不产生动作。
    event_evidence: dict[str, Any] | None = None
    # 仅持仓路径填写：回本价相对日线阻力的结构说明。
    breakeven_lines: list[str] = field(default_factory=list)
    # 已登记的持仓（`state.Position`）。有它时止损/目标听你的，成本与浮盈进简报。
    # 此前 `advise` 对持仓票和陌生票给的是同一份输出：登记了成本 63.5、止损 61.85，
    # 简报照样报系统算的 61.08，读的人得自己去对。
    holding: dict[str, Any] | None = None

    @property
    def has_actionable_levels(self) -> bool:
        return self.entry_low is not None and self.stop is not None

    @property
    def abstained(self) -> bool:
        return bool(self.adjudication and self.adjudication.abstained)


class AdviceEngine:
    """Turns daily structure plus intraday behaviour into levels and scenarios.

    三层的落点都在 `build` 里：L1 读数由调用方传进来，L2 候选在 `_levels` 生成，
    L3 裁决由可选的 `adjudicator` 做。

    **`adjudicator` 默认是 None，而且盘中循环永远不要传它。** 它是每标的一次
    LLM 调用：20 只票的一轮监控会变成 20 次网络往返、几十秒延迟、二十个失败
    机会——而告警要的是秒级时效。见 `docs/ARCHITECTURE.md` 的“输出闸门”。它的位置在
    `advise`（你主动问）和盘前执行卡（一天一次），不在 `monitor`。
    """

    def __init__(self, adjudicator: LevelAdjudicator | None = None) -> None:
        self.adjudicator = adjudicator

    def build(
        self,
        symbol: str,
        daily: DailyTechnicalSnapshot | None,
        intraday: IntradaySnapshot | None,
        wyckoff: WyckoffSignal | None = None,
        name: str | None = None,
        score: float | None = None,
        score_components: dict[str, float] | None = None,
        book: Any = None,
        instrument: Any = None,
        match: Any = None,
    ) -> StockAdvice:
        advice = StockAdvice(
            symbol=symbol,
            name=name,
            score=score,
            score_components=dict(score_components or {}),
        )
        if match is not None:
            _apply_setup(advice, match)
        if daily is None and intraday is None:
            advice.warnings.append("既无日线也无分钟线数据，无法给出任何判断")
            return advice

        if daily is not None:
            advice.trend_state = daily.trend_state.value
        if wyckoff is not None:
            advice.wyckoff_bias = wyckoff.bias.value

        if intraday is None:
            advice.warnings.append(
                "无分钟线数据，仅能给出日线结构判断，价位为前复权口径、不可直接用于挂单"
            )
            advice.last_price = daily.last_close if daily else None
            return advice

        advice.last_price = intraday.last_price
        advice.session_date = str(intraday.session_date)
        advice.is_current_session = intraday.is_current_session
        advice.warnings.extend(intraday.notes)
        advice.intraday = _intraday_facts(intraday)
        advice.trade_context = _trade_context(instrument, intraday)
        if book is not None:
            advice.book = book.to_dict() if hasattr(book, "to_dict") else dict(book)
            advice.warnings.extend(getattr(book, "notes", []) or [])
            describe = getattr(book, "describe", None)
            if callable(describe):
                advice.book_lines = list(describe())

        factor = _session_alignment(daily, intraday)
        if daily is not None and factor is None:
            advice.warnings.append(
                "日线快照与分钟线对不上（疑似不同交易日或不同标的），日线位置未纳入价位计算"
            )

        self._levels(advice, daily, intraday, factor, book)
        self._adjudicate(advice)
        refresh_level_risk(advice)
        self._scenarios(advice, intraday)
        self._shortline(advice, daily, intraday)
        self._reasons(advice, daily, intraday, wyckoff)
        return advice

    # levels

    def _levels(
        self,
        advice: StockAdvice,
        daily: DailyTechnicalSnapshot | None,
        intraday: IntradaySnapshot,
        factor: float | None,
        book: Any = None,
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
        anchor = vwap or (low + high) / 2
        band = span * ENTRY_BAND_RATIO
        advice.entry_low = round(max(low, anchor - band), 3)
        advice.entry_high = round(
            min(max(anchor, advice.entry_low + band), last if last > anchor else high), 3
        )
        if advice.entry_high <= advice.entry_low:
            advice.entry_high = round(advice.entry_low + band, 3)
        pullback_entry = advice.entry_low

        supports = _levels_if_aligned(daily.supports if daily else [], factor)
        below = [level for level in supports if level < low]
        structural_stop = max(below) if below else None

        stop = low - span * STOP_BUFFER
        stop_basis = BASIS_NOISE_FLOOR
        # Prefer the structural level when it is not absurdly far away.
        if (
            structural_stop is not None
            and (advice.entry_low - structural_stop) / advice.entry_low <= 0.09
        ):
            if structural_stop < stop:
                stop_basis = BASIS_STRUCTURE_BREAK
            stop = min(stop, structural_stop)
        advice.stop = round(stop, 3)

        resistances = _levels_if_aligned(daily.resistances if daily else [], factor)
        above = sorted(level for level in resistances if level > last)
        targets = [high] if high > last else []
        targets.extend(above[:2])
        advice.targets = [
            round(level, 3) for level in sorted(dict.fromkeys(targets)) if level > last
        ]

        # 突破与低吸的执行条件相反：前者必须等价格确认越过触发位，后者才是在
        # 现价下方挂回踩单。此前两条策略共用 `entry_low`，导致已经选为突破的票
        # 仍报出均价回踩位。只用当日高点作触发位，不从日线压力位凭空推一个价。
        breakout_trigger = (
            high if advice.playbook == Playbook.BREAKOUT.value and high >= last else None
        )
        if breakout_trigger is not None and (breakout_trigger - last) / last <= 0.03:
            advice.entry_low = round(breakout_trigger, 3)
            advice.entry_high = advice.entry_low
            # 当日高点是突破触发位，不是突破后可兑现的目标。若没有更高的
            # 压力位，就没有可计算的正向收益空间。
            advice.targets = [target for target in advice.targets if target > advice.entry_low]

        # L2：把上面这些数变成带出处的候选
        # 顺序很重要：规则的两个数先算完，再作为 rank 0 传进去，候选集的默认
        # 答案才和改造前逐位相同。
        advice.candidates = build_candidates(
            symbol=advice.symbol,
            last_price=last,
            vwap=vwap,
            session_low=low,
            session_high=high,
            span=span,
            daily_supports=supports,
            daily_resistances=resistances,
            book=book,
            entry_band=pullback_entry,
            breakout_trigger=breakout_trigger,
            rule_stop=advice.stop,
            rule_stop_basis=stop_basis,
        )
        # 带宽存下来：L3 换掉买入价之后，要用同一个带宽重建上沿，而不是
        # 让「买入区间」的宽度随模型的选择漂移。
        advice.entry_band_width = (
            0.0
            if breakout_trigger is not None and (breakout_trigger - last) / last <= 0.03
            else round(band, 3)
        )

    # L3 裁决

    def _adjudicate(self, advice: StockAdvice) -> None:
        """让模型在候选集里做取舍。没有裁决器时这里只是记录规则的默认选择。

        不收 `IntradaySnapshot`：裁决器要的是**事实包**（`advice.intraday`，由
        `_intraday_facts` 摊平成 dict），不是快照对象。两者同名不同物，一起出现
        在签名里只会让人以为可以随手换一个传。
        """
        candidates = advice.candidates
        if candidates is None:
            return
        if self.adjudicator is None:
            advice.adjudication = rule_default(candidates)
            return

        verdict = self.adjudicator.adjudicate(candidates, facts=advice.intraday)
        advice.adjudication = verdict
        advice.warnings.extend(verdict.notes)
        if verdict.used_fallback:
            return

        if verdict.abstained:
            # 弃权只清买入侧。已持仓的人仍然需要止损位——「今天不该买」和
            # 「今天不该拿」是两个问题，模型回答的是前一个。
            advice.entry_low = None
            advice.entry_high = None
            advice.rrr = None
            advice.warnings.insert(0, f"⚖️ 模型裁决：今天不该买入——{verdict.reason}")
            return

        if verdict.entry is not None:
            advice.entry_low = verdict.entry.price
            advice.entry_high = round(verdict.entry.price + (advice.entry_band_width or 0.0), 3)
        if verdict.stop is not None:
            advice.stop = verdict.stop.price
        if verdict.target is not None:
            # 模型选了**一个**目标就只报这一个：它做的取舍就是「这个位置才是
            # 该看的」，把规则那串阶梯留着等于把它的结论稀释回原样。
            advice.targets = [verdict.target.price]
        if verdict.reason and verdict.entry is not None:
            advice.reasons.append(f"价位裁决：{verdict.reason}（{verdict.entry.basis}）")

    # scenarios

    def _scenarios(
        self,
        advice: StockAdvice,
        intraday: IntradaySnapshot,
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

    # narrative

    def _reasons(
        self,
        advice: StockAdvice,
        daily: DailyTechnicalSnapshot | None,
        intraday: IntradaySnapshot,
        wyckoff: WyckoffSignal | None,
    ) -> None:
        advice.reasons.extend(_daily_reasons(daily, wyckoff))
        advice.reasons.extend(_intraday_reasons(intraday))
        if intraday.volume_ratio is not None and intraday.volume_ratio > 3.0:
            advice.warnings.append(
                f"成交量为近 5 日均量的 {intraday.volume_ratio:.2f} 倍，注意情绪透支"
            )
        advice.reasons.extend(_trade_reasons(advice.trade_context))

    def _shortline(
        self,
        advice: StockAdvice,
        daily: DailyTechnicalSnapshot | None,
        intraday: IntradaySnapshot,
    ) -> None:
        trade = advice.trade_context or TradeContext(
            session_volume=intraday.session_volume,
            session_amount=intraday.session_amount,
        )
        levels = _shortline_levels(advice, daily, intraday)

        tally = _ShortlineTally()
        _score_daily_trend(daily, tally)
        _score_daily_volume(daily, tally)
        _score_intraday(intraday, tally)
        _describe_trade(trade, tally)

        bias, action, summary = _shortline_verdict(advice, intraday, tally, levels)
        if action in {"breakout_follow", "buy_pullback"}:
            if advice.playbook is None:
                action, summary = "wait_trigger", "形态可观察，但今日无可用打法，暂不构成买点"
            elif not meets_min_rrr(risk_reward(advice, rounded=False)):
                action, summary = (
                    "wait_trigger",
                    "形态可观察，但缺少满足最低盈亏比的目标与止损，暂不构成买点",
                )

        advice.shortline = ShortlineView(
            bias=bias,
            action=action,
            score=tally.score,
            summary=summary,
            reasons=_unique(tally.reasons)[:6],
            reclaim_level=levels.reclaim,
            support_level=levels.support,
            pressure_level=levels.pressure,
            defense_level=levels.defense,
        )


def _apply_setup(advice: StockAdvice, match: Any) -> None:
    """把打法判据链的结论挂到这条建议上。

    判据链的理由排在价位理由之前——它回答「为什么现在可以买」，价位只回答
    「买在哪」。没有一条打法成立时，拒绝理由进 warnings 而不是 reasons：
    那不是买入依据，是不买的依据。
    """
    advice.setup = match.to_dict()
    primary = match.primary
    advice.playbook = primary.value if primary else None
    if match.ok:
        advice.reasons.extend(match.reasons)
        advice.warnings.extend(match.warnings)
        return
    advice.warnings.extend(f"无可用打法：{text}" for text in match.reject_reasons)
    blocked = advice.setup.get("blocked_by_regime") or []
    if blocked:
        advice.warnings.append(f"{'、'.join(blocked)} 被今天的情绪周期挡掉")


def _daily_reasons(
    daily: DailyTechnicalSnapshot | None,
    wyckoff: WyckoffSignal | None,
) -> list[str]:
    lines: list[str] = []
    if daily is not None:
        lines.append(f"日线趋势 {daily.trend_state.value}")
        if daily.volume_ratio_20d is not None:
            lines.append(f"日线量比 {daily.volume_ratio_20d:.2f}")
        if daily.range_position_60d is not None:
            lines.append(f"60日分位 {daily.range_position_60d:.2f}")
    if wyckoff is not None:
        lines.append(f"威科夫阶段 {wyckoff.bias.value}（结构分 {wyckoff.score:.0f}）")
    return lines


def _intraday_reasons(intraday: IntradaySnapshot) -> list[str]:
    lines: list[str] = []
    if intraday.price_vs_vwap_pct is not None:
        side = "上方" if intraday.price_vs_vwap_pct >= 0 else "下方"
        lines.append(f"现价位于分时均价{side} {abs(intraday.price_vs_vwap_pct):.2%}")
    if intraday.opening_drive_pct is not None:
        lines.append(f"开盘 30 分钟 {intraday.opening_drive_pct:+.2%}")
    if intraday.range_position is not None:
        lines.append(f"日内位置 {intraday.range_position:.0%}（0=最低 1=最高）")
    if intraday.volume_ratio is not None:
        lines.append(f"分钟量能比前 5 日 {intraday.volume_ratio:.2f}")
    return lines


def _trade_reasons(trade: TradeContext | None) -> list[str]:
    if trade is None:
        return []
    lines: list[str] = []
    if trade.turnover_ratio is not None:
        lines.append(f"换手率 {trade.turnover_ratio:.2f}%")
    if trade.session_amount is not None:
        lines.append(f"当日成交额 {trade.session_amount / 1e8:.2f} 亿")
    return lines


@dataclass(frozen=True)
class _ShortlineLevels:
    """短线判断要引用的四个价位。它们只是**最近的那个结构位**，不是预测。"""

    reclaim: float | None
    support: float | None
    pressure: float | None
    defense: float | None


@dataclass
class _ShortlineTally:
    """打分过程的累积量。

    分数、理由、以及被判定语句复用的那几个布尔——`short_ma_weak` / `under_vwap`
    这类既参与加减分、又参与最后的分支选择。分成两处各算一遍就会漂移，所以
    只算一次，装在这里传下去。
    """

    score: int = 0
    reasons: list[str] = field(default_factory=list)
    short_ma_weak: int = 0
    trend_up: bool = False
    trend_down: bool = False
    under_vwap: bool = False
    above_vwap: bool = False
    near_low: bool = False
    near_high: bool = False


def _shortline_levels(
    advice: StockAdvice,
    daily: DailyTechnicalSnapshot | None,
    intraday: IntradaySnapshot,
) -> _ShortlineLevels:
    support = _nearest_below(
        intraday.last_price,
        advice.entry_low,
        daily.stop_reference if daily else None,
        intraday.session_low,
    )
    return _ShortlineLevels(
        reclaim=_nearest_above(
            intraday.last_price,
            intraday.vwap,
            advice.entry_high,
            daily.ma5 if daily else None,
            daily.ma10 if daily else None,
        ),
        support=support,
        pressure=_nearest_above(
            intraday.last_price,
            intraday.session_high,
            advice.targets[0] if advice.targets else None,
        ),
        defense=round(advice.stop, 3) if advice.stop is not None else support,
    )


def _score_daily_trend(daily: DailyTechnicalSnapshot | None, tally: _ShortlineTally) -> None:
    """中期结构（MA20/MA60）与短期均线（MA5/MA10）。"""
    if daily is None:
        return
    tally.trend_up = daily.trend_state in {TrendState.UPTREND, TrendState.REBOUND}
    tally.trend_down = daily.trend_state == TrendState.DOWNTREND
    _score_medium_term_ma(daily, tally)
    _score_short_term_ma(daily, tally)


def _score_medium_term_ma(daily: DailyTechnicalSnapshot, tally: _ShortlineTally) -> None:
    if daily.ma20 is None or daily.ma60 is None:
        return
    if daily.last_close > daily.ma20 > daily.ma60:
        tally.score += 2
        tally.reasons.append("日线仍在 MA20/MA60 上方，中期趋势未坏")
    elif daily.last_close > daily.ma20 and daily.ma20 <= daily.ma60:
        tally.score += 1
        tally.reasons.append("日线处于反弹修复阶段")
    elif daily.last_close < daily.ma20 < daily.ma60:
        tally.score -= 2
        tally.reasons.append("日线仍在 MA20/MA60 下方，中期偏弱")
    elif daily.last_close < daily.ma20:
        tally.score -= 1
        tally.reasons.append("日线仍在 MA20 下方，修复未完成")


def _score_short_term_ma(daily: DailyTechnicalSnapshot, tally: _ShortlineTally) -> None:
    for average in (daily.ma5, daily.ma10):
        if average is None:
            continue
        if daily.last_close >= average:
            tally.score += 1
        else:
            tally.score -= 1
            tally.short_ma_weak += 1
    if tally.short_ma_weak >= 2:
        tally.reasons.append("收盘压在 MA5/MA10 下方，短线主动性不足")
    elif tally.short_ma_weak == 0 and daily.ma5 is not None and daily.ma10 is not None:
        tally.reasons.append("收盘站在 MA5/MA10 上方，短线趋势尚可")


def _score_daily_volume(daily: DailyTechnicalSnapshot | None, tally: _ShortlineTally) -> None:
    if daily is None or daily.volume_ratio_20d is None:
        return
    if daily.volume_ratio_20d >= 1.2:
        tally.score += 1
        tally.reasons.append(f"日线量比 {daily.volume_ratio_20d:.2f}，有增量配合")
    elif daily.volume_ratio_20d < 0.9:
        tally.score -= 1
        tally.reasons.append(f"日线量比 {daily.volume_ratio_20d:.2f}，承接偏弱")


def _score_intraday(intraday: IntradaySnapshot, tally: _ShortlineTally) -> None:
    """盘中三件：相对分时均价、日内位置、分钟量能。"""
    tally.under_vwap = (
        intraday.price_vs_vwap_pct is not None and intraday.price_vs_vwap_pct <= -0.003
    )
    tally.above_vwap = (
        intraday.price_vs_vwap_pct is not None and intraday.price_vs_vwap_pct >= 0.003
    )
    tally.near_low = intraday.range_position is not None and intraday.range_position <= 0.35
    tally.near_high = intraday.range_position is not None and intraday.range_position >= 0.7

    if tally.above_vwap:
        tally.score += 1
        tally.reasons.append(f"现价位于分时均价上方 {intraday.price_vs_vwap_pct:.2%}")
    elif tally.under_vwap:
        tally.score -= 1
        tally.reasons.append(f"现价位于分时均价下方 {abs(intraday.price_vs_vwap_pct):.2%}")

    if tally.near_high:
        tally.score += 1
        tally.reasons.append(f"日内位置 {intraday.range_position:.0%}，接近高位")
    elif tally.near_low:
        tally.score -= 1
        tally.reasons.append(f"日内位置 {intraday.range_position:.0%}，偏近低位")

    if intraday.volume_ratio is None:
        return
    if intraday.volume_ratio >= 1.2:
        tally.score += 1
        tally.reasons.append(f"分钟量能比前 5 日 {intraday.volume_ratio:.2f}，资金参与偏强")
    elif intraday.volume_ratio < 0.8:
        tally.score -= 1
        tally.reasons.append(f"分钟量能比前 5 日 {intraday.volume_ratio:.2f}，跟随资金不足")


def _describe_trade(trade: TradeContext, tally: _ShortlineTally) -> None:
    """换手与成交额只进理由，不加减分——它们说的是「有没有人在交易」，不是方向。"""
    if trade.turnover_ratio is not None:
        if 1.5 <= trade.turnover_ratio <= 12.0:
            tally.reasons.append(f"换手率 {trade.turnover_ratio:.2f}%，筹码交换较充分")
        elif trade.turnover_ratio < 0.8:
            tally.reasons.append(f"换手率 {trade.turnover_ratio:.2f}%，换手偏低")
        else:
            tally.reasons.append(f"换手率 {trade.turnover_ratio:.2f}%")
    if trade.session_amount is not None:
        tally.reasons.append(f"成交额 {trade.session_amount / 1e8:.2f} 亿")


def _shortline_verdict(
    advice: StockAdvice,
    intraday: IntradaySnapshot,
    tally: _ShortlineTally,
    levels: _ShortlineLevels,
) -> tuple[str, str, str]:
    """结构判定优先于分数判定。

    分数是把散落的读数压成一个标量，压完就分不出「弱在哪」了。所以先让四条
    **结构**上说得清的形态自己认领（压在均线下方、强势延续、回踩带内、趋势仍弱），
    认领不到才退回分数阶梯——那一档本来就是「信号不够集中」的意思。
    """
    structural = _shortline_structural_verdict(advice, intraday, tally, levels)
    if structural is not None:
        return structural
    return _shortline_score_verdict(tally, levels)


def _shortline_structural_verdict(
    advice: StockAdvice,
    intraday: IntradaySnapshot,
    tally: _ShortlineTally,
    levels: _ShortlineLevels,
) -> tuple[str, str, str] | None:
    if tally.short_ma_weak >= 2 and tally.under_vwap and tally.near_low:
        summary = "短线偏弱，不宜追买"
        if levels.reclaim is not None:
            summary += f"；先等收回 {levels.reclaim:.2f} 再看修复"
        return "谨慎", "wait_reclaim", summary

    if (
        tally.trend_up
        and tally.above_vwap
        and tally.near_high
        and (intraday.volume_ratio or 0.0) >= 1.0
    ):
        if levels.pressure is not None:
            return "偏多", "breakout_follow", f"强势延续，放量过 {levels.pressure:.2f} 可跟随"
        return "偏多", "breakout_follow", "强势延续，优先沿分时均价看承接"

    in_entry_band = (
        advice.entry_low is not None
        and advice.entry_high is not None
        and advice.entry_low * 0.995 <= intraday.last_price <= advice.entry_high * 1.01
    )
    if tally.trend_up and in_entry_band:
        return (
            "偏多",
            "buy_pullback",
            f"趋势未坏，回踩 {advice.entry_low:.2f}-{advice.entry_high:.2f} 可观察承接，不必追价",
        )

    if tally.trend_down and tally.under_vwap:
        summary = "趋势仍弱，反弹先按修复看，优先防守而不是抢反抽"
        if levels.reclaim is not None:
            summary += f"；至少先看 {levels.reclaim:.2f} 能否站回"
        return "谨慎", "reduce_risk", summary

    return None


def _shortline_score_verdict(
    tally: _ShortlineTally,
    levels: _ShortlineLevels,
) -> tuple[str, str, str]:
    if tally.score >= 3:
        summary = "结构尚可，优先看回踩承接，突破再加速"
        if levels.pressure is not None:
            summary += f"；上破 {levels.pressure:.2f} 更主动"
        return "中性偏多", "wait_trigger", summary

    if tally.score <= -3:
        summary = "信号偏弱，以等待确认或防守为主"
        if levels.reclaim is not None:
            summary += f"；先看 {levels.reclaim:.2f} 能否收回"
        return "谨慎", "wait_reclaim", summary

    summary = "信号不够集中，先等关键位触发"
    if levels.reclaim is not None and levels.support is not None:
        summary += f"；上看 {levels.reclaim:.2f}，下看 {levels.support:.2f}"
    return "中性", "wait_trigger", summary


class _LevelPlan(Protocol):
    entry_low: float | None
    entry_high: float | None
    stop: float | None
    targets: list[float]


def risk_reward(
    advice: _LevelPlan, *, entry_price: float | None = None, rounded: bool = True
) -> float | None:
    """按买入区间上沿及最近目标评估；盘中可显式传入当刻现价。

    区间内不能假定总在最低价成交。价位需满足正数、有限且止损低于整个买入带；
    目标的输入顺序不影响结果。闸门传 ``rounded=False``，避免 1.499 被舍入后放行。
    这是未计交易费用与跳空滑点的结构盈亏比，不代表成交收益。
    """
    low = positive(getattr(advice, "entry_low", None))
    raw_high = getattr(advice, "entry_high", None)
    high = low if raw_high is None else positive(raw_high)
    stop = positive(advice.stop)
    if low is None or high is None or stop is None or high < low or stop >= low:
        return None
    entry = high if entry_price is None else positive(entry_price)
    raw_targets = getattr(advice, "targets", None)
    if not isinstance(raw_targets, (list, tuple)):
        return None
    targets = [positive(value) for value in raw_targets]
    if entry is None or entry <= stop or not targets or any(value is None for value in targets):
        return None
    nearest = min(targets)
    if nearest <= entry:
        return None
    ratio = finite((nearest - entry) / (entry - stop))
    return round(ratio, 2) if rounded and ratio is not None else ratio


def meets_min_rrr(ratio: float | None) -> bool:
    """比较未舍入比值，只容忍浮点减法误差，不放宽两位小数的展示门槛。"""
    value = finite(ratio)
    return value is not None and (
        value >= MIN_RRR or isclose(value, MIN_RRR, rel_tol=0.0, abs_tol=1e-12)
    )


def refresh_level_risk(advice: StockAdvice) -> None:
    """裁决或锁定价位后，同时刷新比值与价位警告，避免文字仍引用旧方案。"""
    advice.warnings = [
        warning for warning in advice.warnings if not warning.startswith(_LEVEL_RISK_PREFIXES)
    ]
    ratio = risk_reward(advice, rounded=False)
    advice.rrr = None if ratio is None else round(ratio, 2)
    if advice.entry_low is None:
        return
    if not advice.targets:
        advice.warnings.append("上方缺少有效目标位，无法给出目标价；等待可核验的压力位后重新评估")
    if ratio is None:
        advice.warnings.append(
            "突破价上方没有有效目标位，无法核算正向盈亏比"
            if advice.playbook == Playbook.BREAKOUT.value
            else "买入区间、止损或目标无效，无法核算正向盈亏比，仅供观察"
        )
    low, stop = positive(advice.entry_low), positive(advice.stop)
    if low is not None and stop is not None:
        distance = (low - stop) / low
        if distance < MIN_STOP_DISTANCE_PCT:
            advice.warnings.append(
                f"止损距离仅 {distance:.2%}，处于日内噪声范围内，容易被无意义扫损"
            )
    if ratio is not None and not meets_min_rrr(ratio):
        advice.warnings.append(
            f"盈亏比仅 {ratio:.4f}，不满足超短线最低要求"
            "（按买入区间上沿与最近目标核算，未计费用与滑点）"
        )


def _intraday_facts(intraday: IntradaySnapshot) -> dict[str, Any]:
    return {
        "session_date": str(intraday.session_date),
        "is_current_session": intraday.is_current_session,
        "open": intraday.open_price,
        "high": intraday.session_high,
        "low": intraday.session_low,
        "vwap": intraday.vwap,
        "session_volume": intraday.session_volume,
        "session_amount": intraday.session_amount,
        # 今开相对**昨收**的跳空。它一直算着却没被带出来，于是「今天低开了怎么看」
        # 这类问题在下游只能拿现价对今开去凑——那是开盘之后走了多少，不是跳空。
        "open_gap_pct": intraday.open_gap_pct,
        # 现价相对昨收，也就是行情软件上那个「涨幅」。**必须自己带出来，不能让
        # 下游拿上面两个数去凑**：凑出来的数不在事实里，`unverified_numbers` 会把
        # 整段论证丢掉——于是模型写一句「今天涨了 3.3%」就等于这段白写了。
        "day_change_pct": intraday.day_change_pct,
        "price_vs_vwap_pct": intraday.price_vs_vwap_pct,
        "price_vs_open_pct": intraday.price_vs_open_pct,
        "opening_drive_pct": intraday.opening_drive_pct,
        "closing_drive_pct": intraday.closing_drive_pct,
        # 和 closing_drive_pct 必须成对带出来：尾盘涨幅一样、量能相反的两只票，
        # 只给涨幅会让下游把「做 K 线诱多」读成「吸筹」。
        "closing_volume_ratio": intraday.closing_volume_ratio,
        "range_position": intraday.range_position,
        "volume_ratio": intraday.volume_ratio,
        "bar_count": intraday.bar_count,
        # 开盘以来走了多少**交易**分钟（午休不计）。它一直算着却没被带出来，而
        # 上面几个比率的可信度全取决于它：10:00 的量比 1.8 和 14:50 的量比 1.8
        # 是两回事，只给比率等于把「才走了 30 分钟」这件事藏起来。
        "minutes_since_open": intraday.minutes_since_open,
        "last_timestamp": (
            intraday.last_timestamp.isoformat(timespec="seconds")
            if intraday.last_timestamp is not None
            else None
        ),
    }


def _trade_context(instrument: Any, intraday: IntradaySnapshot) -> TradeContext:
    float_shares = _nested_number(
        instrument,
        ("ext", "float_shares"),
        ("float_shares",),
        ("circulating_shares",),
        ("流通股本",),
    )

    turnover_ratio = None
    if float_shares is not None and float_shares > 0 and intraday.session_volume > 0:
        turnover_ratio = intraday.session_volume / float_shares * 100.0
    else:
        turnover_ratio = _turnover_ratio_from_instrument(instrument)

    float_cap_yi = None
    if float_shares is not None and float_shares > 0 and intraday.last_price > 0:
        float_cap_yi = float_shares * intraday.last_price / 1e8

    return TradeContext(
        session_volume=intraday.session_volume,
        session_amount=intraday.session_amount,
        turnover_ratio=round(turnover_ratio, 4) if turnover_ratio is not None else None,
        float_cap_yi=round(float_cap_yi, 2) if float_cap_yi is not None else None,
    )


def _turnover_ratio_from_instrument(instrument: Any) -> float | None:
    tickflow_ratio = _nested_number(instrument, ("ext", "turnover_rate"), ("turnover_rate",))
    if tickflow_ratio is not None:
        return tickflow_ratio * 100.0 if tickflow_ratio <= 1.0 else tickflow_ratio

    return _nested_number(instrument, ("换手率",), ("turnover_ratio",), ("turnoverRatio",))


def _nested_number(payload: Any, *paths: tuple[str, ...]) -> float | None:
    for path in paths:
        value = payload
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        number = _to_number(value)
        if number is not None:
            return number
    return None


def _to_number(value: Any) -> float | None:
    """收成有限 float。比 `numeric.finite` 只多认两种**写法**：千分位逗号与
    百分号后缀——这条路读的是 akshare 交出来的展示用字段（`换手率` 给
    `"3.52%"`、股本给 `"1,234.5"`）。

    判据本身仍然只有 `finite` 那一条。原实现自己写了 try/float，于是
    `"nan"` 收成 NaN、`True` 收成 1.0，两者都会一路流进换手率与流通股本，
    再从那里进打分——而 NaN 的比较永远是 False，不会有任何一处报错。

    百分号只脱不除：`"3.52%"` 是 3.52，因为上游这一格的单位本来就是「个百分点」。
    """
    if isinstance(value, str):
        value = value.strip().replace(",", "").removesuffix("%")
    return finite(value)


def _nearest_above(last_price: float, *levels: float | None) -> float | None:
    usable = [
        float(level) for level in levels if isinstance(level, (int, float)) and level > last_price
    ]
    return round(min(usable), 3) if usable else None


def _nearest_below(last_price: float, *levels: float | None) -> float | None:
    usable = [
        float(level) for level in levels if isinstance(level, (int, float)) and level < last_price
    ]
    return round(max(usable), 3) if usable else None


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


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
