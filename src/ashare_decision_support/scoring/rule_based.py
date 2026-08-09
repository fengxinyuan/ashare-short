"""Rule-based scoring baseline，按**低吸**口径。

打分先于任何模型存在，权重是人工设定的启发式，不是拟合出来的参数——把输出当
排序辅助看，不是校准过的概率。台账攒够 200 条已兑现记录（`journal/stats.py:
MODEL_READY_SAMPLES`）之后才谈拟合——在那之前，按统计调这里的权重调的是噪声。

**为什么整体口径是低吸而不是追涨。** 2026-08-02 实测：这套打分选出的 30 只候选，
按标的自身波动归一化后，落在低吸理想窗口的 **0 只**、缩量的 **0/30**、19 只根本
没有回调。也就是说它一直在选「正在涨的票」而不是「回调到位的票」。三处根因：

1. 威科夫 MARKUP（主升段）给 +12，而 LPS（回踩承接）只给 +4 —— 权重把追高排在
   了回踩前面。
2. 量能 `ratio >= 1.0` 一律 +8，缩量 `< 0.6` 反而 -5。**低吸最健康的形态恰恰是
   缩量回调**（回调放量意味着出货），符号是反的。
3. 全部用阶跃函数，量比 1.02 和 3.99 同分，而 4.0 处有 13 分悬崖。项目在大盘层
   （`sectors/predict.py`）已经明确否定过阶跃，个股层没同步。

现在的口径：**趋势还在（L1）× 回调到位（L2）× 缩量（回调质量）**，斜坡取代阶跃。
"""

from __future__ import annotations

import math

from ashare_decision_support.models.enums import TrendState, WyckoffBias
from ashare_decision_support.models.schemas import DailyTechnicalSnapshot, WyckoffSignal
from ashare_decision_support.news.base import SymbolSentiment
from ashare_decision_support.numeric import clamp, finite
from ashare_decision_support.scoring.base import ScoreInput, ScoreProvider, ScoreResult
from ashare_decision_support.scoring.context import read_catalysts
from ashare_decision_support.signals.dipbuy import DipFeatures

TREND_WEIGHTS: dict[TrendState, float] = {
    TrendState.UPTREND: 12.0,
    TrendState.REBOUND: 6.0,
    TrendState.RANGE: 0.0,
    TrendState.DOWNTREND: -15.0,
}

# 威科夫阶段权重。MARKUP 从 12 降到 7、ACCUMULATION 保持 10：主升段是**追高**的
# 位置，吸筹末端才是低吸该待的地方。阶段本身只说明「趋势没坏」，真正的进场信号
# 是下面的事件权重。
BIAS_WEIGHTS: dict[WyckoffBias, float] = {
    WyckoffBias.MARKUP: 7.0,
    WyckoffBias.ACCUMULATION: 10.0,
    WyckoffBias.NEUTRAL: 0.0,
    WyckoffBias.DISTRIBUTION: -12.0,
    WyckoffBias.MARKDOWN: -15.0,
}

# 事件权重。Spring（假跌破后收回）与 LPS（最后支撑点回踩承接）**就是低吸信号**，
# 提权；SOS（放量上推突破）是强势确认，对追涨有用、对低吸是「已经晚了」，降到 +3。
SPRING_POINTS = 8.0
LPS_POINTS = 7.0
SOS_POINTS = 3.0
UPTHRUST_POINTS = -8.0

MIN_BARS_FOR_CONFIDENCE = 60

# Ceiling on what per-stock news may contribute, in either direction. Kept small
# on purpose: the reading is keyword matching over headlines, it mis-attributes,
# and it must never outweigh what the tape actually did. Same discipline as the
# ±12 sector-sentiment cap in `sectors/predict.py`, scaled to this component
# budget. Its job is to break ties between names the price data ranks equally,
# not to promote a name the price data does not support.
NEWS_CAP = 6.0

# Below this the reading is one weak keyword in one stale headline. Reporting it
# as a reason makes the brief noisier without changing any ordering.
NEWS_MIN_POINTS = 1.0

# Cross-layer context is deliberately a secondary ranking layer. It can break
# ties between technically viable names, but it must not turn a broken chart
# into a buy merely because a headline is exciting.
CONTEXT_DELTA_CAP = 20.0
CONTEXT_POSITIVE_BROKEN_CAP = 6.0
SECTOR_RELATION_CAP = 5.0
CAPITAL_FLOW_CAP = 5.0
VOLUME_PRICE_CAP = 6.0
# 业绩没有上限常量，因为它不产生分数。删掉而不是设成 0.0：一个存在的 `EARNINGS_CAP`
# 迟早会有人把它接回 `total`，而那正是这一层刻意不做的事。见 `_score_earnings`。
CATALYST_CAP = 6.0

# 回调质量的分值上限。这是整套打分里唯一直接回答「这是不是一个低吸位置」的一项，
# 给得比量能重，但压不过趋势——趋势坏了的回调是接刀，L1 必须能否决 L2。
DIP_CAP = 10.0
VOLUME_CAP = 6.0
COST_CAP = 5.0

# 盈亏比分母的下限：止损距离至少要有一个日波幅，否则算出来的比值反映的是止损贴
# 得多近，不是机会有多好。第二个是绝对兜底，防止 ATR 本身异常小的情况。
MIN_RRR_DENOMINATOR_ATR = 1.0
MIN_RRR_DENOMINATOR_PCT = 0.015

# Best and worst reachable component totals, excluding the base. Kept explicit
# rather than derived so a weight change that breaks the mapping shows up as a
# failing test instead of as silently squashed scores.
#
#   trend        +12 / -15
#   wyckoff      +10 bias +10 structure +8 spring +7 LPS +3 SOS  /  -15 -10 -8
#   dip          +10 / -8
#   成交成本代理 +5 / -5（可选，不计入基础分值映射范围）
#   volume        +6 /  -6
#   range         +5 /  -6
#   risk-reward   +8 /  -8
#   news          +6 /  -6
# 成交成本代理不是所有数据源都能提供。它作为增量读数参与排序，但不计入基础
# 分值映射范围，否则成本数据缺失会让原本的满分样例凭空降分。
MAX_COMPONENT_TOTAL = 12.0 + 38.0 + DIP_CAP + VOLUME_CAP + 5.0 + 8.0 + NEWS_CAP
MIN_COMPONENT_TOTAL = -15.0 - 33.0 - 8.0 - VOLUME_CAP - 6.0 - 8.0 - NEWS_CAP


def _ramp(value: float, onset: float, full: float) -> float:
    """[onset, full] 线性映射到 [0, 1]，两端夹住。

    和 `sectors/predict.py` 的同名函数是同一个东西——阶跃函数在阈值两侧给出差
    很多的分，而阈值本身是拍脑袋定的，市场读数在它附近来回穿越时排序就会抖。
    大盘层早就换成斜坡了，这里是补上个股层。
    """
    if full == onset:
        return 1.0 if value >= full else 0.0
    return clamp((value - onset) / (full - onset), 0.0, 1.0)


class RuleBasedScoreProvider(ScoreProvider):
    name = "rule-based"

    def __init__(self, base_score: float = 50.0) -> None:
        self.base_score = base_score

    def score(self, item: ScoreInput) -> ScoreResult:
        reasons: list[str] = []
        warnings: list[str] = []
        raw = 0.0

        raw += self._score_trend(item.snapshot, reasons)
        raw += self._score_wyckoff(item.wyckoff, reasons)
        raw += self._score_dip(item.dip, reasons, warnings)
        raw += self._score_cost_basis(item.dip, reasons, warnings)
        raw += self._score_volume(item.snapshot, item.dip, reasons, warnings)
        raw += self._score_range_position(item.snapshot, reasons, warnings)
        raw += self._score_risk_reward(item.snapshot, reasons, warnings)
        news_points = self._score_news(item.news, reasons, warnings)
        raw += news_points

        if item.snapshot.bar_count < MIN_BARS_FOR_CONFIDENCE:
            warnings.append(f"历史样本仅 {item.snapshot.bar_count} 根，结构判断置信度偏低")

        technical_score = self._to_scale(raw)
        # `technical` 过去把个股舆情也包进去了，展示时看不出新闻到底改变了
        # 多少。按同一条缩放曲线重算一次无舆情基准，差额就是新闻的真实边际影响；
        # 不改变总分、排序或任何门槛。
        price_structure_score = self._to_scale(raw - news_points)
        news_delta = technical_score - price_structure_score
        context_delta, components = self._score_context(item, reasons, warnings)
        score = clamp(technical_score + context_delta, 0.0, 100.0)
        components = {
            "technical": round(price_structure_score, 2),
            "news": round(news_delta, 2),
            **components,
            "context_delta": round(context_delta, 2),
        }
        return ScoreResult(
            symbol=item.symbol,
            score=round(score, 2),
            reasons=reasons,
            warnings=warnings,
            components=components,
        )

    def _to_scale(self, raw: float) -> float:
        """Map the component total onto 0-100 without clipping the top.

        Adding the components to a base of 50 and clamping at 100 reaches the
        ceiling at +50, but the components can total +70 — so every reasonably
        good name lands on exactly 100.0 and the ranking stops discriminating
        precisely among the candidates it is being asked to choose between.
        Scaling by the reachable range keeps 50 as neutral and leaves the top
        of the scale for a name that actually earns it.
        """
        span = MAX_COMPONENT_TOTAL if raw >= 0 else abs(MIN_COMPONENT_TOTAL)
        headroom = (100.0 - self.base_score) if raw >= 0 else self.base_score
        return clamp(self.base_score + raw / span * headroom, 0.0, 100.0)

    def _score_trend(self, snapshot: DailyTechnicalSnapshot, reasons: list[str]) -> float:
        weight = TREND_WEIGHTS.get(snapshot.trend_state, 0.0)
        if weight:
            reasons.append(f"日线趋势 {snapshot.trend_state.value}（{weight:+.0f}）")
        return weight

    def _score_wyckoff(self, wyckoff: WyckoffSignal | None, reasons: list[str]) -> float:
        if wyckoff is None:
            return 0.0

        total = BIAS_WEIGHTS.get(wyckoff.bias, 0.0)
        if total:
            reasons.append(f"威科夫阶段 {wyckoff.bias.value}（{total:+.0f}）")

        # WyckoffSignal.score is already bounded to [-100, 100].
        structure = wyckoff.score * 0.1
        if abs(structure) >= 1.0:
            reasons.append(f"结构评分 {wyckoff.score:.0f}（{structure:+.1f}）")
        total += structure

        for flag, delta, label in (
            (wyckoff.spring, SPRING_POINTS, "Spring 弹簧（假跌破收回）"),
            (wyckoff.last_point_of_support, LPS_POINTS, "LPS 回踩承接"),
            (wyckoff.sign_of_strength, SOS_POINTS, "SOS 放量上推"),
            (wyckoff.upthrust, UPTHRUST_POINTS, "Upthrust 假突破"),
        ):
            if flag:
                total += delta
                reasons.append(f"{label}（{delta:+.0f}）")
        return total

    def _score_dip(
        self,
        dip: DipFeatures | None,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        """回调是否到位——这是整套打分里唯一直接回答「这是不是低吸位置」的一项。

        档位来自 `signals/dipbuy.py`，单位是标的自身的日均真实波幅，不是绝对
        百分比：同样跌 6%，对日振幅 1.8% 的长江电力是深度回调，对日振幅 6.2%
        的题材股还在日常噪声里。

        `none` 不给分而不是给负分：没回调本身不是缺点，只是**这不是低吸的时机**。
        真正要扣分的是 `deep`——那时「回调」和「趋势已经走坏」分不开了。
        """
        if dip is None or dip.pullback_band is None:
            return 0.0

        band = dip.pullback_band
        depth = dip.pullback_atr
        if band == "none":
            reasons.append(f"尚未回调（{depth:.1f} 个日波幅），非低吸时机（+0）")
            return 0.0
        if band == "shallow":
            points = DIP_CAP * 0.4
            reasons.append(f"浅回调 {dip.pullback_pct:+.1%}（{depth:.1f} 个日波幅，{points:+.1f}）")
            return points
        if band == "ideal":
            # 窗口内再按深度给一条斜坡：越接近窗口中段越好，不在边界上跳。
            points = DIP_CAP * (0.7 + 0.3 * _ramp(depth, 2.0, 3.0))
            reasons.append(
                f"回调到位 {dip.pullback_pct:+.1%}（{depth:.1f} 个日波幅，{points:+.1f}）"
            )
            return points

        warnings.append(
            f"回撤已达 {dip.pullback_pct:.1%}（{depth:.1f} 个日波幅），"
            "回调与趋势破坏已难以区分，低吸转为接刀"
        )
        points = -8.0 * _ramp(depth, 4.0, 7.0)
        reasons.append(f"回撤过深 {dip.pullback_pct:+.1%}（{points:+.1f}）")
        return points

    def _score_cost_basis(
        self,
        dip: DipFeatures | None,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        """把成交成本代理当作支撑读数，不把它写成真实筹码成本。

        30 日线负责识别中段成本，15 日线负责识别更近的交易密集区。只有在价格
        接近成本、或盘中跌破后收回时才加分；明显跌穿 30 日成本则转为风险提示。
        """
        if dip is None:
            return 0.0

        points = 0.0
        distance_30 = dip.dist_to_turnover_cost_30_pct
        distance_15 = dip.dist_to_turnover_cost_15_pct
        down_day = dip.ret_1d is not None and dip.ret_1d < 0

        if dip.turnover_cost_30_reclaimed:
            points += 4.5 if down_day else 3.5
            label = "当日下跌踩回" if down_day else "踩回"
            reasons.append(
                f"{label}30日成交成本代理 {dip.turnover_cost_30:.2f}（{points:+.1f}）"
            )
        elif distance_30 is not None:
            if distance_30 < -0.05:
                penalty = -COST_CAP * _ramp(-distance_30, 0.05, 0.12)
                points += penalty
                warnings.append(
                    f"收盘低于30日成交成本代理 {abs(distance_30):.1%}，"
                    "成本线承接未确认"
                )
                reasons.append(f"跌破30日成交成本代理 {distance_30:+.1%}（{penalty:+.1f}）")
            elif abs(distance_30) <= 0.03:
                proximity = 2.5 * (1.0 - abs(distance_30) / 0.03)
                if down_day:
                    proximity += 0.5
                points += proximity
                reasons.append(
                    f"接近30日成交成本代理 {dip.turnover_cost_30:.2f}"
                    f"（偏离 {distance_30:+.1%}，{proximity:+.1f}）"
                )

        if distance_15 is not None and abs(distance_15) <= 0.025:
            fine = 1.5 * (1.0 - abs(distance_15) / 0.025)
            points += fine
            reasons.append(
                f"接近15日成交成本代理 {dip.turnover_cost_15:.2f}"
                f"（偏离 {distance_15:+.1%}，{fine:+.1f}）"
            )

        return clamp(points, -COST_CAP, COST_CAP)

    def _score_volume(
        self,
        snapshot: DailyTechnicalSnapshot,
        dip: DipFeatures | None,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        """量能——**含义取决于当前处在回调段还是启动段，同一个量比意义相反。**

        回调途中要的是缩量：放量下跌是出货，不是低吸机会。而没有回调时（`none`）
        放量才是启动确认。此前这里只有一个静态阈值，`ratio >= 1.0` 一律 +8、
        缩量 -5，对低吸恰好选反。
        """
        ratio = snapshot.volume_ratio_20d
        if ratio is None:
            return 0.0

        in_pullback = dip is not None and dip.pullback_band in ("shallow", "ideal", "deep")

        if ratio > 4.0:
            warnings.append(f"量比 {ratio:.2f} 异常放大，注意情绪透支")

        if in_pullback:
            # 缩量斜坡：1.0 倍量无分，0.5 倍量拿满分。放量回调反向扣分。
            if ratio <= 1.0:
                points = VOLUME_CAP * _ramp(-ratio, -1.0, -0.5)
                reasons.append(f"回调缩量 {ratio:.2f}，抛压有限（{points:+.1f}）")
                return points
            points = -VOLUME_CAP * _ramp(ratio, 1.2, 2.5)
            if points:
                reasons.append(f"回调放量 {ratio:.2f}，疑似出货（{points:+.1f}）")
            return points

        # 未回调：温和放量是启动确认，过度放量是透支，两端都用斜坡。
        if ratio >= 1.0:
            points = VOLUME_CAP * _ramp(ratio, 1.0, 1.8) - VOLUME_CAP * _ramp(ratio, 2.5, 4.5)
            if abs(points) >= 0.5:
                label = "温和放量" if points > 0 else "放量过度"
                reasons.append(f"{label} {ratio:.2f}（{points:+.1f}）")
            return points
        points = -VOLUME_CAP * _ramp(-ratio, -0.8, -0.4)
        if points:
            reasons.append(f"量能萎缩 {ratio:.2f}（{points:+.1f}）")
        return points

    def _score_range_position(
        self,
        snapshot: DailyTechnicalSnapshot,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        """60 日区间位置，斜坡而非阶跃。

        原来 0.75 和 0.76 差 5 分、0.95 和 0.96 差 6 分。这些边界都是拍脑袋定的，
        读数在附近来回穿越时排序会跟着抖。
        """
        position = snapshot.range_position_60d
        if position is None:
            return 0.0

        # 甜区 0.3–0.75：上方有空间。两侧线性衰减，高位再叠一层追高惩罚。
        if position < 0.3:
            points = 5.0 * _ramp(position, 0.05, 0.3) - 3.0 * _ramp(-position, -0.2, -0.05)
        else:
            points = 5.0 * (1.0 - _ramp(position, 0.75, 0.92))
        points -= 6.0 * _ramp(position, 0.90, 1.0)

        if position > 0.95:
            warnings.append("已贴近 60 日高点，追高风险偏大")
        if abs(points) >= 0.5:
            reasons.append(f"60日分位 {position:.2f}（{points:+.1f}）")
        return points

    def _score_risk_reward(
        self,
        snapshot: DailyTechnicalSnapshot,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        """盈亏比，分母带一个按波动定的下限，且改成斜坡。

        没有下限时会算出荒谬的数：2026-08-02 实测 603508.SH 现价 23.16、止损
        23.15，止损距离 0.04% → 盈亏比 **172.84**，直接拿满分。止损位本身已在
        `signals/daily_technical.py:_stop_reference` 加了最小距离，这里再兜一道，
        因为 provider 也可能收到别处构造的 snapshot。
        """
        close = snapshot.last_close
        stop = snapshot.stop_reference
        target = snapshot.target_reference
        if not stop or not target or close <= 0:
            warnings.append("缺少有效支撑或压力位，无法评估盈亏比")
            return 0.0

        downside = 1.0 - stop / close
        upside = target / close - 1.0
        if downside <= 0 or upside <= 0:
            warnings.append("当前价已跌破支撑或站上压力，盈亏比参考失效")
            return 0.0

        unit = (snapshot.atr14 / close) if snapshot.atr14 and close > 0 else 0.02
        floor = max(MIN_RRR_DENOMINATOR_ATR * unit, MIN_RRR_DENOMINATOR_PCT)
        if downside < floor:
            warnings.append(
                f"止损距离仅 {downside:.2%}，不足一个日波幅，"
                "按最小有效止损重算盈亏比（否则会算出虚高的比值）"
            )
            downside = floor

        rrr = upside / downside
        # 1.0 无分，2.5 满分；低于 0.8 起负分，0.3 处触底。不再在 2.0 处跳 8 分。
        points = 8.0 * _ramp(rrr, 1.0, 2.5) - 8.0 * _ramp(-rrr, -0.8, -0.3)
        if rrr < 0.8:
            warnings.append(f"盈亏比仅 {rrr:.2f}，风险收益不划算")
        if abs(points) >= 0.5:
            reasons.append(f"盈亏比 {rrr:.2f}（{points:+.1f}）")
        return points

    def _score_news(
        self,
        news: SymbolSentiment | None,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        """Per-stock news, capped at ±NEWS_CAP and multiplied by its own intensity.

        `intensity` is the confidence dial: one matched keyword in one headline
        gives a small number even when the polarity is ±1.0, so a single
        loosely-worded item cannot move the ranking on its own.
        """
        if news is None or news.is_neutral:
            return 0.0

        points = clamp(news.polarity * news.intensity * NEWS_CAP * 2.0, -NEWS_CAP, NEWS_CAP)
        if abs(points) < NEWS_MIN_POINTS:
            return 0.0

        hits = news.bullish_hits if points > 0 else news.bearish_hits
        label = "偏多" if points > 0 else "偏空"
        detail = f"个股舆情 {news.doc_count} 条{label}（{points:+.1f}，关键词：{'、'.join(hits[:4])}）"
        reasons.append(detail)
        if points <= -NEWS_CAP / 2:
            # Loud enough to survive into the pre-open card, where warnings are
            # what actually gets read at decision time.
            warnings.append(f"个股舆情偏空：{'、'.join(hits[:4])}")
        return points

    def _score_context(
        self,
        item: ScoreInput,
        reasons: list[str],
        warnings: list[str],
    ) -> tuple[float, dict[str, float]]:
        """Score cross-layer evidence after the technical score.

        The components are independently capped and then capped again as a
        group. This prevents a single stale earnings headline or a single
        large-flow print from dominating price structure.
        """
        context = item.context
        if context is None:
            return 0.0, {}

        components: dict[str, float] = {}
        total = 0.0
        sector = context.sector
        if sector is not None:
            sector_points = self._score_sector_relation(item, sector, reasons, warnings)
            components["sector_relation"] = round(sector_points, 2)
            total += sector_points

        flow_points = self._score_capital_flow(context, reasons)
        if flow_points:
            components["capital_flow"] = round(flow_points, 2)
            total += flow_points

        volume_points = self._score_volume_price_context(item.volume_price, reasons, warnings)
        if volume_points:
            components["volume_price"] = round(volume_points, 2)
            total += volume_points

        # 业绩**刻意不计入分数**，只写进理由与风险。
        #
        # 这不是遗漏，是 `DECISIONS.md` §六b 那条教训的直接应用：上一轮把缩量做成
        # ±6 分的权重，结果 60 条建议里 52 条是放量回调——**只要它是权重，就能被
        # 别的项买回来**。业绩层的正面读数（预增/扭亏）在样本够之前和缩量当时的
        # 处境一模一样：看起来该加分，但没有任何回测支持这个幅度。
        #
        # 否决那一侧照旧是硬的（`screens/earnings.py`，在打分之前就把标的移除），
        # 所以「不给权重」削弱的只有加分方向。两侧不对称是设计，不是不一致。
        #
        # 它的可验证出口是台账：`Recommendation.earnings` 存下完整裁决，
        # `journal/stats.py:_veto_cost` 与 `by_group` 负责在样本够之后回答
        # 「带业绩催化的那批兑现率高多少」。**先落台账，等 edge，再谈权重。**
        self._score_earnings(context.earnings, reasons, warnings)
        catalyst_points = self._score_catalysts(item, reasons, warnings)
        if catalyst_points:
            components["catalyst"] = round(catalyst_points, 2)
            total += catalyst_points

        total = clamp(total, -CONTEXT_DELTA_CAP, CONTEXT_DELTA_CAP)
        if item.snapshot.trend_state == TrendState.DOWNTREND and total > CONTEXT_POSITIVE_BROKEN_CAP:
            warnings.append("个股处于下跌趋势，板块/事件利好只作观察，正向上下文加分已限额")
            total = CONTEXT_POSITIVE_BROKEN_CAP
        return total, components

    def _score_sector_relation(
        self,
        item: ScoreInput,
        sector: object,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        trusted = bool(getattr(sector, "trusted", False))
        if not trusted:
            warnings.append("所属板块覆盖率不足，未使用板块关系分")
            return 0.0

        points = 0.0
        score = finite(getattr(sector, "score", None))
        if score is not None:
            sector_points = clamp((score - 50.0) / 50.0 * 2.5, -2.5, 2.5)
            points += sector_points
            if abs(sector_points) >= 0.5:
                reasons.append(f"板块评分 {score:.1f}，个股与板块方向一致（{sector_points:+.1f}）")

        breadth = finite(getattr(sector, "breadth", None))
        if breadth is not None:
            breadth_points = clamp((breadth - 0.5) * 4.0, -2.0, 2.0)
            points += breadth_points
            if abs(breadth_points) >= 0.5:
                reasons.append(f"板块上涨宽度 {breadth:.0%}（{breadth_points:+.1f}）")

        flow_norm = finite(getattr(sector, "flow_norm", None))
        if flow_norm is not None:
            flow_points = clamp(flow_norm * 1.5, -1.5, 1.5)
            points += flow_points
            if abs(flow_points) >= 0.5:
                reasons.append(f"板块资金相对强度 {flow_norm:+.2f}（{flow_points:+.1f}）")

        stock_ret_5d = _bars_return(item.bars, 5)
        sector_ret_5d = finite(getattr(sector, "ret_5d", None))
        if stock_ret_5d is not None and sector_ret_5d is not None:
            excess = stock_ret_5d - sector_ret_5d
            relative_points = clamp(excess * 45.0, -1.5, 1.5)
            points += relative_points
            if abs(relative_points) >= 0.5:
                label = "跑赢" if relative_points > 0 else "跑输"
                reasons.append(
                    f"个股5日相对板块{label} {excess:+.2%}（{relative_points:+.1f}）"
                )

        return clamp(points, -SECTOR_RELATION_CAP, SECTOR_RELATION_CAP)

    def _score_capital_flow(self, context: object, reasons: list[str]) -> float:
        flow_norm = finite(getattr(context, "flow_norm", None))
        flow_ratio = finite(getattr(context, "flow_ratio", None))
        if flow_norm is None and flow_ratio is None:
            return 0.0

        # Prefer cross-sectional normalization. The ratio is only a fallback
        # because the raw flow window and the turnover window may differ.
        basis = flow_norm
        if basis is None and flow_ratio is not None:
            basis = math.tanh(flow_ratio / 0.03)
        points = clamp((basis or 0.0) * CAPITAL_FLOW_CAP, -CAPITAL_FLOW_CAP, CAPITAL_FLOW_CAP)
        if abs(points) >= 0.75:
            raw = getattr(context, "net_inflow", None)
            amount = getattr(context, "amount", None)
            detail = ""
            if isinstance(raw, (int, float)):
                detail = f"，净流入 {raw / 1e8:+.2f} 亿"
            if isinstance(amount, (int, float)) and amount > 0:
                detail += f"，占当日成交额 {raw / amount:.1%}" if isinstance(raw, (int, float)) else ""
            reasons.append(f"个股资金流相对强度 {basis:+.2f}{detail}（{points:+.1f}）")
        return points

    def _score_volume_price_context(
        self,
        features: object | None,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        if features is None:
            return 0.0
        points = 0.0
        quadrant = getattr(features, "quadrant", None)
        if quadrant == "放量上涨":
            points += 2.0
        elif quadrant == "缩量上涨":
            points += 1.0
        elif quadrant == "放量下跌":
            points -= 2.5
        elif quadrant == "缩量下跌":
            points -= 0.5

        if getattr(features, "expanding", None) is True:
            points += 1.5
        if getattr(features, "up_down_vol_ratio", None) is not None:
            ratio = float(features.up_down_vol_ratio)
            points += clamp((ratio - 1.0) * 2.0, -2.0, 2.0)
            if ratio >= 1.25:
                reasons.append(f"近20日上涨量强于下跌量（量能比 {ratio:.2f}）")
        if getattr(features, "climax_stall", None):
            points -= 3.5
            warnings.append("天量滞涨，资金流入可能是高位换手，不按正向量价加分")
        elif getattr(features, "climax", None):
            points -= 1.5
        if getattr(features, "breakdown_on_volume", None):
            points -= 3.0
            warnings.append("放量跌破均线，量能读数转为风险而非支持")

        points = clamp(points, -VOLUME_PRICE_CAP, VOLUME_PRICE_CAP)
        if abs(points) >= 0.75:
            reasons.append(f"持续量价关系 {quadrant or '未分类'}（{points:+.1f}）")
        return points

    def _score_earnings(
        self,
        verdict: object | None,
        reasons: list[str],
        warnings: list[str],
    ) -> None:
        """把业绩裁决写进理由与风险。**返回 None——这一层不产生分数。**

        名字保留 `_score_` 前缀会误导，但改名会波及调用方；真正防止它悄悄变回
        权重的是签名：返回 `None` 的函数没法被 `total +=` 用上，改回去必须是
        一次显式的、看得见的改动。上限常量也已删除，见 `EARNINGS_CAP` 的去处。
        """
        if verdict is None:
            return
        if getattr(verdict, "is_strong_catalyst", False):
            reasons.append("新鲜且可盈利的业绩催化（不计分）")
        elif getattr(verdict, "catalysts", None):
            reasons.append("业绩正向披露（不计分）")
        elif getattr(verdict, "warnings", None):
            warnings.append("业绩披露存在不确定性，未按业绩催化处理")
        if getattr(verdict, "blocking", None):
            # 走到这里说明标的绕过了 `screens/earnings.py`（例如 `advise` 手工
            # 指定一只已被否决的票）。必须显形，不能因为「没进分数」就不说。
            warnings.append("⚠️ 该标的触发业绩风险否决，盘前链路不会选它")

    def _score_catalysts(
        self,
        item: ScoreInput,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        sector = item.context.sector if item.context is not None else None
        sector_catalyst = getattr(sector, "catalyst", None)
        stock_catalyst = read_catalysts(
            getattr(item.news, "headlines", ()) if item.news is not None else ()
        )
        sector_value = float(getattr(sector_catalyst, "net_score", 0.0) or 0.0)
        stock_value = float(stock_catalyst.net_score)
        points = clamp((sector_value * 0.65) + (stock_value * 0.85), -CATALYST_CAP, CATALYST_CAP)
        if abs(points) >= 0.75:
            hits = tuple(dict.fromkeys(
                (*getattr(sector_catalyst, "hits", ()), *stock_catalyst.hits)
            ))
            risk_hits = tuple(dict.fromkeys(
                (*getattr(sector_catalyst, "risk_hits", ()), *stock_catalyst.risk_hits)
            ))
            if points > 0:
                reasons.append(f"板块/个股事件催化：{'、'.join(hits[:5])}（{points:+.1f}）")
            else:
                warnings.append(f"板块/个股风险事件：{'、'.join(risk_hits[:5])}（{points:+.1f}）")
        return points


def _bars_return(bars: list[dict], window: int) -> float | None:
    closes = [
        float(row["close"])
        for row in bars
        if isinstance(row.get("close"), (int, float)) and row["close"] > 0
    ]
    if len(closes) <= window:
        return None
    return closes[-1] / closes[-window - 1] - 1.0


