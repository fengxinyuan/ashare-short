"""Rank sectors for the next session.

What this is: a same-day strength ranking that fuses three independent readings
— how the sector actually traded, what the news said about it, and where
dragon-tiger money went. Sectors that score high agree on all three.

What this is not: a validated forecast. None of these weights have been fitted
or backtested, and the sentiment input in particular is a keyword heuristic.
Read the ranking as "where to look first", and let the per-stock layer and your
own judgement decide anything further.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from ashare_decision_support.news.base import NewsDoc, SentimentAnalyzer
from ashare_decision_support.news.events import detect_events
from ashare_decision_support.numeric import clamp
from ashare_decision_support.sectors.base import SectorDefinition, SectorMetrics, SectorOutlook
from ashare_decision_support.sectors.keywords import MARKET_LEVEL_KEYWORDS, sector_terms

# Component ceilings. The sum of the three caps plus the base has to stay inside
# [0, 100], and no single reading may carry the score on its own — that is the
# point of fusing three of them.
MOMENTUM_CAP = 30.0

# 板块回调窗口的档位，单位与个股层一致：成分股「距 20 日高回撤 ÷ 自身日波幅」的
# 中位数。板块整体的回撤幅度小于单只股票（成分股的低点不同时发生），所以档位比
# `signals/dipbuy.py` 的个股档位收窄。
PULLBACK_NONE = 0.6
PULLBACK_IDEAL = 2.0
PULLBACK_DEEP = 3.5
PULLBACK_CAP = 10.0
SENTIMENT_CAP = 12.0
CAPITAL_CAP = 8.0
BASE_SCORE = 50.0

# Below this fraction of constituents with usable bars, the momentum reading is
# reported but not trusted as a ranking input.
MIN_TRUSTED_COVERAGE = 0.3

# Ceiling for any *suggested* position factor. No single unbacktested reading
# should ever propose a full book, however good the day looked.
MAX_SUGGESTED_FACTOR = 0.8

# Breadth and 5-day move above which the market is treated as extended rather
# than strong. For an ultra-short book, a session where nearly everything rose
# after a week of gains is a late entry, not a green light — mapping breadth
# straight to position size is pro-cyclical exactly when it hurts most.
#
# Graduated, not a threshold. A step function here has the same defect this
# project rejected in the original plan's `F_market_emotion` (a cliff at
# N_down=50): readings either side of the line get sizes 3x apart, and a market
# oscillating across it flips the recommendation back and forth all session.
# The haircut ramps in between onset and full instead, and `min()` keeps the
# "both must be elevated" requirement that an AND of two thresholds expressed.
OVERHEAT_BREADTH_ONSET = 0.70
OVERHEAT_BREADTH_FULL = 0.90
OVERHEAT_RET_5D_ONSET = 0.03
OVERHEAT_RET_5D_FULL = 0.08
OVERHEAT_MAX_HAIRCUT = 0.30

# 拿满舆情分所需的文档数。
#
# 为什么需要独立的一道闸：`SentimentScore.intensity` 是命中**密度**（按文档数
# 归一），不是证据量。2 条措辞强烈的快讯，平均密度必然高于 22 条褒贬混杂的，
# 于是样本越小反而越容易顶到上限——实测一天里基础化工靠 2 条拿满 +12，计算机
# 22 条只拿到 +2.0，证据越多得分越低，方向正好反了。
#
# 密度回答「这批文档说得多强」，条数回答「有多少人在说」。两个问题都要过，所以
# 用一条独立的样本量斜坡相乘，而不是去改密度的定义（那会连带影响个股舆情）。
SENTIMENT_FULL_WEIGHT_DOCS = 8


def sentiment_confidence(doc_count: int) -> float:
    """样本量置信度，落在 [0, 1]。文档数达到 SENTIMENT_FULL_WEIGHT_DOCS 才给满权。"""
    if doc_count <= 0:
        return 0.0
    return min(1.0, doc_count / float(SENTIMENT_FULL_WEIGHT_DOCS))


def overheat_factor(breadth: float | None, ret_5d: float | None) -> float:
    """Multiplier in [1 - OVERHEAT_MAX_HAIRCUT, 1.0]. 1.0 means no haircut."""
    if breadth is None or ret_5d is None:
        return 1.0
    breadth_ramp = _ramp(breadth, OVERHEAT_BREADTH_ONSET, OVERHEAT_BREADTH_FULL)
    ret_ramp = _ramp(ret_5d, OVERHEAT_RET_5D_ONSET, OVERHEAT_RET_5D_FULL)
    extension = min(breadth_ramp, ret_ramp)
    return 1.0 - extension * OVERHEAT_MAX_HAIRCUT


def _ramp(value: float, onset: float, full: float) -> float:
    if full <= onset:
        return 1.0 if value >= full else 0.0
    return clamp((value - onset) / (full - onset), 0.0, 1.0)


@dataclass(frozen=True)
class MarketSentiment:
    """Market-wide reading used to suggest (never to force) a position factor."""

    polarity: float
    intensity: float
    doc_count: int
    suggested_position_factor: float
    reasons: list[str]


# Market-level score components. Same discipline as the sector score: every
# reading is capped, and no single one can carry the verdict alone.
MARKET_BREADTH_CAP = 20.0
MARKET_RET_CAP = 12.0
MARKET_MA20_CAP = 10.0
MARKET_SENTIMENT_CAP = 8.0

# 涨停池情绪读数的上限。对超短线这是最直接的情绪指标——「昨天封住的板今天还封得住
# 吗」只有炸板率回答——但它是**当日**读数，对次日只有参考意义，所以给的权重和舆情
# 同级，不和宽度同级。
MARKET_LIMIT_CAP = 10.0

# 炸板率分界。低于 onset 视作封板结实，高于 full 视作情绪退潮，中间线性过渡。
# 和过热折减一样用斜坡而不是阈值：50% 与 51% 不该给出差一倍的结论。
BLOWUP_HEALTHY = 0.25
BLOWUP_STRESSED = 0.55

# 连板高度分界，用来衡量有没有资金愿意接力。
STREAK_WEAK = 2
STREAK_STRONG = 6

# Score bands for the direction label. Deliberately wide in the middle: an
# unbacktested composite should say "中性" far more often than it says anything
# else, and a label that is almost always directional is a label nobody trusts.
DIRECTION_BANDS = (
    (68.0, "偏多"),
    (57.0, "中性偏多"),
    (43.0, "中性"),
    (32.0, "中性偏空"),
    (0.0, "偏空"),
)

# Fraction of sectors that must clear the coverage floor before a market-level
# reading is trusted at all.
MIN_TRUSTED_SECTOR_RATIO = 0.5


@dataclass(frozen=True)
class MarketOutlook:
    """Explicit whole-market direction, separate from the position factor.

    The position factor answers "how much"; this answers "which way". They are
    not the same question — a market can be readably weak (a direction) while
    still justifying a small position (a factor), and collapsing the two into
    one number loses that.
    """

    direction: str
    score: float
    trusted: bool
    breadth: float | None
    median_ret_1d: float | None
    median_ret_5d: float | None
    above_ma20: float | None
    sectors_trusted: int
    sectors_total: int
    constituents_covered: int
    sentiment: MarketSentiment
    # 涨停池读数。None 表示本次取不到，不是「今天没有涨停」——后者是 0。
    limit_up_count: int | None = None
    blowup_count: int | None = None
    limit_down_count: int | None = None
    blowup_rate: float | None = None
    max_streak: int | None = None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def suggested_position_factor(self) -> float:
        return self.sentiment.suggested_position_factor


def limit_pool_points(
    blowup_rate: float | None,
    max_streak: int | None,
) -> tuple[float, list[str]]:
    """涨停池 → 分值与说明。落在 [-MARKET_LIMIT_CAP, +MARKET_LIMIT_CAP]。

    两个读数分工不同：炸板率说的是**今天**的封板质量，连板高度说的是**有没有资金
    愿意持续接力**。前者退潮但后者还在，和两个都塌了，是完全不同的两种盘面。
    """
    points = 0.0
    reasons: list[str] = []

    if blowup_rate is not None:
        # 炸板率越高越扣分。健康线以下给正分，退潮线以上给满额负分。
        extension = _ramp(blowup_rate, BLOWUP_HEALTHY, BLOWUP_STRESSED)
        seal = (1.0 - 2.0 * extension) * (MARKET_LIMIT_CAP * 0.6)
        points += seal
        verdict = "封板结实" if seal > 0 else "封板意愿弱"
        reasons.append(f"炸板率 {blowup_rate:.0%}，{verdict}（{seal:+.1f}）")

    if max_streak is not None and max_streak > 0:
        if max_streak >= STREAK_STRONG:
            streak = MARKET_LIMIT_CAP * 0.4
            verdict = "有资金接力"
        elif max_streak <= STREAK_WEAK:
            streak = -MARKET_LIMIT_CAP * 0.4
            verdict = "缺乏持续性资金"
        else:
            streak = 0.0
            verdict = "接力一般"
        points += streak
        reasons.append(f"最高连板 {max_streak} 板，{verdict}（{streak:+.1f}）")

    return clamp(points, -MARKET_LIMIT_CAP, MARKET_LIMIT_CAP), reasons


class SectorPredictor:
    def __init__(
        self,
        analyzer: SentimentAnalyzer,
        min_coverage: float = MIN_TRUSTED_COVERAGE,
    ) -> None:
        self.analyzer = analyzer
        self.min_coverage = min_coverage

    # ---- sector ranking --------------------------------------------------

    def predict(
        self,
        definitions: list[SectorDefinition],
        metrics: dict[str, SectorMetrics],
        market_docs: list[NewsDoc] | None = None,
        net_inflow_by_symbol: dict[str, float] | None = None,
        attribution: dict[str, tuple[str, ...]] | None = None,
        fallback_empty_labels: bool = False,
    ) -> list[SectorOutlook]:
        docs_by_sector = self.attribute_docs(
            definitions,
            market_docs or [],
            attribution,
            fallback_empty_labels=fallback_empty_labels,
        )
        inflow_by_sector = self._attribute_inflow(definitions, net_inflow_by_symbol or {})
        inflow_scale = max((abs(value) for value in inflow_by_sector.values()), default=0.0)

        outlooks: list[SectorOutlook] = []
        for definition in definitions:
            metric = metrics.get(definition.name)
            if metric is None:
                continue

            reasons: list[str] = []
            warnings: list[str] = []
            score = BASE_SCORE

            trusted = metric.coverage_ratio >= self.min_coverage
            momentum = self._momentum_component(metric, reasons, warnings)
            score += momentum

            docs = docs_by_sector.get(definition.name, [])
            sentiment = self.analyzer.score_docs(docs)
            evidence = detect_events(doc.text for doc in docs)
            confidence = sentiment_confidence(len(docs))
            sentiment_points = clamp(
                sentiment.polarity * sentiment.intensity * confidence * SENTIMENT_CAP * 2.0,
                -SENTIMENT_CAP,
                SENTIMENT_CAP,
            )
            if abs(sentiment_points) >= 1.0:
                hits = sentiment.bullish_hits if sentiment_points > 0 else sentiment.bearish_hits
                thin = "" if confidence >= 1.0 else f"，样本仅 {len(docs)} 条已按 {confidence:.2f} 折权"
                reasons.append(
                    f"舆情 {len(docs)} 条，{'偏多' if sentiment_points > 0 else '偏空'}"
                    f"（{sentiment_points:+.1f}，关键词：{'、'.join(hits[:4])}{thin}）"
                )
            score += sentiment_points
            if evidence.event_types:
                reasons.append(
                    "行业事件 "
                    + "、".join(evidence.event_types[:4])
                    + "："
                    + "、".join(evidence.hits[:5])
                )
            if evidence.risk_hits:
                warnings.append("新闻风险词：" + "、".join(evidence.risk_hits[:5]))

            inflow = inflow_by_sector.get(definition.name)
            capital_points = 0.0
            if inflow is not None and inflow_scale > 0:
                capital_points = clamp(inflow / inflow_scale * CAPITAL_CAP, -CAPITAL_CAP, CAPITAL_CAP)
                if abs(capital_points) >= 1.0:
                    reasons.append(f"龙虎榜净额 {inflow / 1e8:+.2f} 亿（{capital_points:+.1f}）")
            score += capital_points

            outlooks.append(
                SectorOutlook(
                    name=definition.name,
                    score=round(clamp(score, 0.0, 100.0), 2),
                    metrics=metric,
                    level=definition.level,
                    sentiment_polarity=sentiment.polarity,
                    sentiment_intensity=sentiment.intensity,
                    net_inflow=inflow,
                    trusted=trusted,
                    reasons=reasons,
                    warnings=warnings,
                    headlines=[doc.title for doc in docs[:3]],
                    news_doc_count=len(docs),
                    news_event_types=evidence.event_types,
                    news_hits=evidence.hits,
                    news_risk_hits=evidence.risk_hits,
                )
            )

        # Untrusted sectors sort to the bottom regardless of score, so a caller
        # taking the top N never picks one up by accident.
        return sorted(outlooks, key=lambda item: (item.trusted, item.score), reverse=True)

    # ---- market reading --------------------------------------------------

    def market_outlook(
        self,
        market_docs: list[NewsDoc],
        metrics: dict[str, SectorMetrics],
        limit_pool: Any = None,
    ) -> MarketOutlook:
        """Whole-market direction, from measured breadth plus macro sentiment.

        Deliberately *not* a forecast. Every input is same-day and the weights
        are hand-set, so what this states is "the market currently reads as X",
        which is the strongest claim the data supports.

        `limit_pool` 是可选的涨停池快照。它回答的是别的读数都回答不了的问题：
        指数可以全线上涨而封板意愿同时崩塌，这两件事经常同一天发生。
        """
        sentiment = self.market_sentiment(market_docs, metrics)
        trusted = [
            metric
            for metric in metrics.values()
            if metric.coverage_ratio >= self.min_coverage
        ]
        covered = sum(metric.covered for metric in trusted)

        reasons: list[str] = []
        warnings: list[str] = []

        # Refusing to label is the point. A neutral default here would let "we
        # could not measure the market" read exactly like "the market is
        # balanced", and those are opposite messages for position sizing.
        if not metrics or not trusted or len(trusted) < len(metrics) * MIN_TRUSTED_SECTOR_RATIO:
            warnings.append(
                f"仅 {len(trusted)}/{len(metrics)} 个板块覆盖率达标，"
                "不足以支撑大盘方向判断，本次不给方向"
            )
            return MarketOutlook(
                direction="数据不足",
                score=0.0,
                trusted=False,
                breadth=None,
                median_ret_1d=None,
                median_ret_5d=None,
                above_ma20=None,
                sectors_trusted=len(trusted),
                sectors_total=len(metrics),
                constituents_covered=covered,
                sentiment=sentiment,
                limit_up_count=getattr(limit_pool, "limit_up_count", None),
                blowup_count=getattr(limit_pool, "blowup_count", None),
                limit_down_count=getattr(limit_pool, "limit_down_count", None),
                blowup_rate=getattr(limit_pool, "blowup_rate", None),
                max_streak=getattr(limit_pool, "max_streak", None),
                reasons=reasons,
                warnings=warnings,
            )

        breadth = _weighted(trusted, "breadth")
        above_ma20 = _weighted(trusted, "above_ma20")
        ret_1d = _median_of(trusted, "ret_1d")
        ret_5d = _median_of(trusted, "ret_5d")

        score = BASE_SCORE
        if breadth is not None:
            points = clamp((breadth - 0.5) * 2.0 * MARKET_BREADTH_CAP, -MARKET_BREADTH_CAP, MARKET_BREADTH_CAP)
            score += points
            reasons.append(f"{len(trusted)} 个板块 {covered} 只成分股，加权上涨占比 {breadth:.0%}（{points:+.1f}）")
        if ret_1d is not None:
            points = clamp(ret_1d * 400.0, -MARKET_RET_CAP, MARKET_RET_CAP)
            score += points
            reasons.append(f"板块当日中位涨幅 {ret_1d:+.2%}（{points:+.1f}）")
        if ret_5d is not None:
            points = clamp(ret_5d * 150.0, -MARKET_RET_CAP, MARKET_RET_CAP)
            score += points
            reasons.append(f"板块 5 日中位涨幅 {ret_5d:+.2%}（{points:+.1f}）")
        if above_ma20 is not None:
            points = clamp((above_ma20 - 0.5) * 2.0 * MARKET_MA20_CAP, -MARKET_MA20_CAP, MARKET_MA20_CAP)
            score += points
            reasons.append(f"站上 20 日均线占比 {above_ma20:.0%}（{points:+.1f}）")
        if sentiment.doc_count:
            confidence = sentiment_confidence(sentiment.doc_count)
            points = clamp(
                sentiment.polarity * sentiment.intensity * confidence * MARKET_SENTIMENT_CAP * 2.0,
                -MARKET_SENTIMENT_CAP,
                MARKET_SENTIMENT_CAP,
            )
            score += points
            reasons.append(f"宏观舆情 {sentiment.doc_count} 条，情绪 {sentiment.polarity:+.2f}（{points:+.1f}）")

        blowup_rate = getattr(limit_pool, "blowup_rate", None)
        max_streak = getattr(limit_pool, "max_streak", None)
        limit_points, limit_reasons = limit_pool_points(blowup_rate, max_streak)
        if limit_reasons:
            score += limit_points
            reasons.extend(limit_reasons)
            if blowup_rate is not None and blowup_rate >= BLOWUP_STRESSED:
                warnings.append(
                    f"炸板率 {blowup_rate:.0%}，冲板的票过半没守住——指数读数与封板质量背离时，"
                    "以封板质量为准降低预期"
                )

        score = round(clamp(score, 0.0, 100.0), 2)
        direction = next(label for threshold, label in DIRECTION_BANDS if score >= threshold)

        haircut = overheat_factor(breadth, ret_5d)
        if haircut < 1.0:
            warnings.append(
                f"上涨面 {breadth:.0%} 且 5 日中位涨幅 {ret_5d:+.1%}，越靠近普涨末段，"
                f"方向读数偏多但追高风险同步升高（仓位建议已按 {haircut:.2f} 折减）"
            )

        return MarketOutlook(
            direction=direction,
            score=score,
            trusted=True,
            breadth=breadth,
            median_ret_1d=ret_1d,
            median_ret_5d=ret_5d,
            above_ma20=above_ma20,
            sectors_trusted=len(trusted),
            sectors_total=len(metrics),
            constituents_covered=covered,
            sentiment=sentiment,
            limit_up_count=getattr(limit_pool, "limit_up_count", None),
            blowup_count=getattr(limit_pool, "blowup_count", None),
            limit_down_count=getattr(limit_pool, "limit_down_count", None),
            blowup_rate=blowup_rate,
            max_streak=max_streak,
            reasons=reasons,
            warnings=warnings,
        )

    def market_sentiment(
        self,
        market_docs: list[NewsDoc],
        metrics: dict[str, SectorMetrics],
    ) -> MarketSentiment:
        """Suggest a position factor from macro news plus measured breadth.

        Advisory by design: the returned factor is a starting point for the
        operator, and the caller keeps whatever manual value it was given. An
        unbacktested keyword score should not be silently sizing real money.
        """
        macro_docs = [
            doc for doc in market_docs if any(term in doc.text for term in MARKET_LEVEL_KEYWORDS)
        ]
        sentiment = self.analyzer.score_docs(macro_docs)

        # Weight by how many constituents each sector actually measured, and
        # drop the ones below the coverage floor entirely. A sector with 2 of
        # 427 names covered must not carry the same weight as one with 300 —
        # an unweighted mean here was reading 0.91 off a thin sample.
        trusted = [
            metric
            for metric in metrics.values()
            if metric.breadth is not None and metric.coverage_ratio >= self.min_coverage
        ]
        covered_total = sum(metric.covered for metric in trusted)
        breadth = (
            sum(metric.breadth * metric.covered for metric in trusted) / covered_total
            if covered_total
            else None
        )

        reasons: list[str] = []
        # Breadth is the anchor because it is measured; news only nudges it.
        if breadth is None:
            factor = 0.5
            reasons.append(
                f"仅 {len(trusted)}/{len(metrics)} 个板块覆盖率达标，无可信宽度读数，建议系数取中性 0.50"
            )
        else:
            factor = clamp(0.15 + breadth * 0.75, 0.1, MAX_SUGGESTED_FACTOR)
            reasons.append(
                f"{len(trusted)} 个板块（{covered_total} 只成分股）加权上涨占比 {breadth:.0%}，"
                f"基准建议 {factor:.2f}（上限 {MAX_SUGGESTED_FACTOR:.2f}）"
            )

            extended_rets = [metric.ret_5d for metric in trusted if metric.ret_5d is not None]
            median_5d = median(extended_rets) if extended_rets else None
            haircut = overheat_factor(breadth, median_5d)
            if haircut < 1.0:
                factor = clamp(factor * haircut, 0.1, MAX_SUGGESTED_FACTOR)
                reasons.append(
                    f"5 日板块中位涨幅已达 {median_5d:+.1%} 且上涨面 {breadth:.0%}，"
                    f"越靠近普涨末段追高风险越高，按 {haircut:.2f} 折减至 {factor:.2f}"
                )

        if macro_docs and abs(sentiment.polarity) > 0.2:
            confidence = sentiment_confidence(len(macro_docs))
            adjust = clamp(sentiment.polarity * sentiment.intensity * confidence * 0.3, -0.15, 0.15)
            factor = clamp(factor + adjust, 0.1, MAX_SUGGESTED_FACTOR)
            reasons.append(
                f"宏观舆情 {len(macro_docs)} 条，情绪 {sentiment.polarity:+.2f}，"
                f"调整 {adjust:+.2f} 后为 {factor:.2f}"
            )

        reasons.append("该系数为建议值，未经回测验证；实际仓位以你的手工设定为准")
        return MarketSentiment(
            polarity=sentiment.polarity,
            intensity=sentiment.intensity,
            doc_count=len(macro_docs),
            suggested_position_factor=round(factor, 2),
            reasons=reasons,
        )

    # ---- internals -------------------------------------------------------

    def _momentum_component(
        self,
        metric: SectorMetrics,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        if metric.coverage_ratio < self.min_coverage:
            warnings.append(
                f"仅 {metric.covered}/{metric.constituents} 只成分股有可用日线，动量读数不可信"
            )
            return 0.0

        total = 0.0

        # ---- L1 中期趋势：这个板块值不值得看 --------------------------------
        # 用 20 日而不是 5 日。5 日涨幅高说明的恰恰是**已经涨过了**，把它当主要
        # 权重就是在按「谁涨得最多」排序——2026-08-02 实测，前五名板块的 5 日中位
        # 是 +10.16%/+10.48%/+9.48%/+8.03%/+4.80%，排序几乎就是已涨幅度倒序。
        if metric.ret_20d is not None:
            points = clamp(metric.ret_20d * 60.0, -8.0, 10.0)
            total += points
            reasons.append(f"20日中位涨幅 {metric.ret_20d:+.2%}，中期趋势（{points:+.1f}）")
        if metric.above_ma20 is not None:
            points = clamp((metric.above_ma20 - 0.5) * 12.0, -5.0, 5.0)
            total += points
            reasons.append(f"站上20日均线占比 {metric.above_ma20:.0%}（{points:+.1f}）")

        # ---- L2 回调窗口：现在是不是进场时机 --------------------------------
        # 低吸要的板块画像是**中期强 + 短期弱**。回调用成分股中位的归一化深度，
        # 和个股层同口径（`signals/dipbuy.py`）。
        total += self._pullback_component(metric, reasons)

        # ---- 短期读数降权为辅助 ----------------------------------------------
        # 当日涨幅与上涨家数仍然有用（说明板块正在被关注），但权重砍到不再主导排序。
        if metric.ret_1d is not None:
            points = clamp(metric.ret_1d * 100.0, -4.0, 3.0)
            total += points
            reasons.append(f"当日中位涨幅 {metric.ret_1d:+.2%}（{points:+.1f}）")
        if metric.breadth is not None:
            points = clamp((metric.breadth - 0.5) * 8.0, -3.0, 3.0)
            total += points
            reasons.append(f"上涨家数占比 {metric.breadth:.0%}（{points:+.1f}）")
        if metric.strong_count > 0:
            points = min(2.0, metric.strong_count * 0.5)
            total += points
            reasons.append(f"涨幅超 9.5% 个股 {metric.strong_count} 只（{points:+.1f}）")
        if metric.volume_ratio is not None:
            if metric.volume_ratio >= 3.0:
                warnings.append(f"量比中位数 {metric.volume_ratio:.2f}，放量过猛需防高位接盘")
            elif metric.volume_ratio >= 1.2:
                total += 3.0
                reasons.append(f"量比中位数 {metric.volume_ratio:.2f}，温和放量（+3.0）")
            elif metric.volume_ratio < 0.7:
                total -= 3.0
                reasons.append(f"量比中位数 {metric.volume_ratio:.2f}，缩量（-3.0）")

        return clamp(total, -MOMENTUM_CAP, MOMENTUM_CAP)

    def _pullback_component(self, metric: SectorMetrics, reasons: list[str]) -> float:
        """板块回调窗口。**这是低吸口径下唯一回答「现在该不该进」的一项。**

        没有回调不扣分——那只是说明时机未到，不是板块不好；回调过深才扣分，因为
        那时板块级的「回调」和「趋势转折」已经分不开。

        用归一化深度（除以成分股各自的日波幅）而不是绝对回撤：银行板块回调 3%
        和半导体板块回调 3% 完全不是一回事。
        """
        depth = metric.pullback_atr
        if depth is None:
            return 0.0

        if depth < PULLBACK_NONE:
            reasons.append(f"板块尚未回调（中位 {depth:.1f} 个日波幅，+0.0）")
            return 0.0
        if depth <= PULLBACK_DEEP:
            points = PULLBACK_CAP * _ramp(depth, PULLBACK_NONE, PULLBACK_IDEAL)
            label = f"{metric.pullback:+.2%}" if metric.pullback is not None else f"{depth:.1f} 个日波幅"
            reasons.append(f"板块回调到位 {label}（中位 {depth:.1f} 个日波幅，{points:+.1f}）")
            return points

        points = -6.0 * _ramp(depth, PULLBACK_DEEP, PULLBACK_DEEP * 1.8)
        reasons.append(f"板块回撤过深（中位 {depth:.1f} 个日波幅，{points:+.1f}）")
        return points

    def attribute_docs(
        self,
        definitions: list[SectorDefinition],
        docs: list[NewsDoc],
        attribution: dict[str, tuple[str, ...]] | None = None,
        fallback_empty_labels: bool = False,
    ) -> dict[str, list[NewsDoc]]:
        """Assign each document to every sector whose vocabulary it matches.

        给了 `attribution`（大模型归因）就优先用它，只有模型没给结论的那条才退回
        关键词。两者混用是刻意的：模型漏一条不该让这条消息彻底消失，而模型给了
        结论时不该再让词表去覆盖它。

        **公开的原因**：这份「哪条消息算在哪个板块头上」的映射是板块预测的
        可测试边界。让调用方自己按 `sector_terms` 再匹配一遍，等于让同一条规则
        有两份实现，而它们会各自演化。
        """
        from ashare_decision_support.news.dedup import dedup_key

        result: dict[str, list[NewsDoc]] = {definition.name: [] for definition in definitions}
        known = set(result)
        terms_by_sector = {definition.name: sector_terms(definition.name) for definition in definitions}
        labels = attribution or {}

        for doc in docs:
            text = doc.text
            if not text:
                continue
            key = dedup_key(doc)
            if labels and key in labels:
                # 模型给了结论就以它为准。盘前的定向补抓可以显式开启
                # `fallback_empty_labels`，让产业链价格/供给词重新作为召回证据；
                # 默认仍保留「空归属就是结论」的严格行为，避免普通宏观消息误挂板块。
                if not labels[key] and not fallback_empty_labels:
                    continue
                if not labels[key] and fallback_empty_labels:
                    for sector, terms in terms_by_sector.items():
                        if any(term in text for term in terms):
                            result[sector].append(doc)
                    continue
                for sector in labels[key]:
                    if sector in known:
                        result[sector].append(doc)
                continue
            for sector, terms in terms_by_sector.items():
                if any(term in text for term in terms):
                    result[sector].append(doc)
        return result

    def _attribute_inflow(
        self,
        definitions: list[SectorDefinition],
        net_inflow_by_symbol: dict[str, float],
    ) -> dict[str, float]:
        if not net_inflow_by_symbol:
            return {}
        # Dragon-tiger rows carry bare 6-digit codes while the taxonomy carries
        # exchange-suffixed ones, so both sides are reduced to the bare code.
        by_code = {_bare(symbol): value for symbol, value in net_inflow_by_symbol.items()}
        totals: dict[str, float] = {}
        for definition in definitions:
            total = sum(by_code.get(_bare(symbol), 0.0) for symbol in definition.symbols)
            if total:
                totals[definition.name] = total
        return totals


def _bare(symbol: str) -> str:
    return symbol.split(".", maxsplit=1)[0].strip()


def _weighted(metrics: list[SectorMetrics], attribute: str) -> float | None:
    """Coverage-weighted mean. A sector with 2 measured names must not carry
    the same weight as one with 300."""
    rows = [
        (getattr(metric, attribute), metric.covered)
        for metric in metrics
        if getattr(metric, attribute) is not None
    ]
    total = sum(count for _, count in rows)
    if not rows or not total:
        return None
    return sum(value * count for value, count in rows) / total


def _median_of(metrics: list[SectorMetrics], attribute: str) -> float | None:
    values = [getattr(metric, attribute) for metric in metrics if getattr(metric, attribute) is not None]
    return median(values) if values else None
