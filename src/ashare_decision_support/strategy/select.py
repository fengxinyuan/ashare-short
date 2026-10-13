"""共享选股流程：日线分析、打法门槛与评分。

analyze_universe 计算可复用读数，select_from 按上下文评分，供盘前粗排和精排共用。
调用方提供标的和周期约束；主动单票分析可用 allowed=None 省略周期约束。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.models.schemas import DailyTechnicalSnapshot, WyckoffSignal
from ashare_decision_support.scoring import (
    RuleBasedScoreProvider,
    ScoreInput,
    ScoreProvider,
    StockFactorContext,
)
from ashare_decision_support.signals import (
    DailyTechnicalAnalyzer,
    WyckoffAnalyzer,
    measure_volume_price,
)
from ashare_decision_support.signals.chips import ChipDistributionEstimate, estimate_chips
from ashare_decision_support.signals.dipbuy import DipFeatures, measure_dip
from ashare_decision_support.signals.volume_price import VolumePriceFeatures
from ashare_decision_support.strategy.exits import ExitAssessment, assess_exits
from ashare_decision_support.strategy.playbook import PlaybookMatch, evaluate_playbooks
from ashare_decision_support.strategy.regime import RegimeVerdict

# 打法门槛过滤后至少要剩这么多标的，否则说明今天没有像样的位置。

# **不足时不放宽门槛。** 改造前 morning 的行为是「少于最低样本就放宽低吸门槛并按
# 分数排序」，那等于在最该收手的日子里输出最多的追高票。现在少就是少。
MIN_SETUP_CANDIDATES = 20


@dataclass(frozen=True)
class SelectedSymbol:
    """一只票走完选股段之后的全部读数。"""

    symbol: str
    score: float
    snapshot: DailyTechnicalSnapshot
    match: PlaybookMatch
    dip: DipFeatures
    name: str | None = None
    sector: str | None = None
    wyckoff: WyckoffSignal | None = None
    volume_price: VolumePriceFeatures | None = None
    chips: ChipDistributionEstimate | None = None
    exits: ExitAssessment | None = None
    news: Any | None = None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    score_components: dict[str, float] = field(default_factory=dict)
    factor_context: StockFactorContext | None = None

    @property
    def playbook(self) -> str | None:
        primary = self.match.primary
        return primary.value if primary else None

    def to_row(self) -> dict[str, Any]:
        """生成台账、候选和渲染共用的记录，保持字段契约一致。"""
        return {
            "symbol": self.symbol,
            "name": self.name,
            "sector": self.sector,
            "score": self.score,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "score_components": dict(self.score_components),
            "factor_context": (
                self.factor_context.to_dict() if self.factor_context is not None else None
            ),
            "last_close": self.snapshot.last_close,
            # `last_close` 是哪一根日线的收盘。台账拿它锚定百分比价位——盘前跑的
            # 晨报，这一天是上一交易日，不是计划要执行的那天（`journal/verify.py`）。
            "bar_date": self.snapshot.source_ts.date().isoformat(),
            "trend_state": self.snapshot.trend_state.value,
            "stop_reference": self.snapshot.stop_reference,
            "target_reference": self.snapshot.target_reference,
            "wyckoff_bias": self.wyckoff.bias.value if self.wyckoff is not None else None,
            "news": self.news.to_dict() if self.news is not None else None,
            "dip": self.dip.to_dict(),
            # 量价画像。进台账是为了让统计能按量价形态分组——「放量突破和缩量
            # 回踩哪个兑现率高」必须能被分开回答，混在一起只会得到一个平均数。
            "volume_price": (
                self.volume_price.to_dict() if self.volume_price is not None else None
            ),
            # 卖出判据（`strategy/exits.py`）。买入侧和卖出侧并排落台账，事后才能
            # 回答「进场判据对的时候，离场判据是不是也在同一天亮起来」。
            "exits": self.exits.to_dict() if self.exits is not None else None,
            # **估算**筹码分布（换手衰减法）。字段名一律带 _est，且与
            # `dip.turnover_cost_30`（成交成本代理）严格分开——混用一个名字，
            # 事后查不出某个数当时是从哪来的。
            "chips": self.chips.to_dict() if self.chips is not None else None,
            "playbook": self.playbook,
            "setup": self.match.to_dict(),
        }


@dataclass(frozen=True)
class UniverseAnalysis:
    """结构读数，与打分无关。贵的那一半，算一次全程复用。"""

    bars: dict[str, list[dict]] = field(default_factory=dict)
    snapshots: dict[str, DailyTechnicalSnapshot] = field(default_factory=dict)
    wyckoff: dict[str, WyckoffSignal] = field(default_factory=dict)
    dips: dict[str, DipFeatures] = field(default_factory=dict)
    volume_price: dict[str, VolumePriceFeatures] = field(default_factory=dict)
    # 估算筹码要用的流通股本。留在这里是因为筹码改成了按需算（见 `chips_of`），
    # 而按需算的那一刻必须还能拿到它。
    float_shares: dict[str, float] = field(default_factory=dict)
    # `chips_of` 的记忆。frozen 挡的是**重新绑定字段**，不挡对字典本身的写入，
    # 所以缓存放在这里是合法的，也是 morning 两遍打分能共用同一份读数的原因。
    chip_cache: dict[str, ChipDistributionEstimate] = field(default_factory=dict)

    @property
    def scope(self) -> list[str]:
        """既有日线又算出结构快照的那批。"""
        return [symbol for symbol in self.bars if symbol in self.snapshots]

    def chips_of(self, symbol: str) -> ChipDistributionEstimate | None:
        """这只票的估算筹码分布，**按需算并记住**。

        **为什么不在 `analyze_universe` 里一次算完。** 筹码是这条链上最贵的一项
        （实测占 `analyze_universe` 的 57%：400 只 240 根要 1.25 秒），而它
        **既不进打法门槛也不进打分**——全仓只有一个读它的地方，就是把它挂到
        `SelectedSymbol` 上进简报与台账。也就是说过不了门槛的那几百只，筹码算完
        就直接扔了。

        按需算之后，它只发生在真正被打分的那批票上。缓存让 morning 的粗排/精排
        两遍不会重算同一只——那正是 `analyze_universe` 与 `select_from` 拆开的理由。
        """
        cached = self.chip_cache.get(symbol)
        if cached is not None:
            return cached
        rows = self.bars.get(symbol)
        if not rows:
            return None
        estimate = estimate_chips(rows, self.float_shares.get(symbol))
        self.chip_cache[symbol] = estimate
        return estimate


@dataclass
class UniverseResult:
    """选股段的产物。"""

    selected: list[SelectedSymbol] = field(default_factory=list)
    # 有结构读数但今天没有任何可用打法的。**保留而不是丢弃**——「这只票为什么不
    # 选」的答案就在它的 `match.rejected` 里，那是 advise 与复盘都要的东西。
    rejected: list[SelectedSymbol] = field(default_factory=list)
    matches: dict[str, PlaybookMatch] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def rows(self) -> list[dict[str, Any]]:
        return [item.to_row() for item in self.selected]


def analyze_universe(
    bars_by_symbol: dict[str, list[dict]],
    float_shares: dict[str, float] | None = None,
) -> UniverseAnalysis:
    """日线 → 技术快照 + 威科夫 + 低吸画像 + 量价画像。纯计算，不碰网络。

    `float_shares`（流通股本，单位股）影响两项：换手率与筹码分布。取不到时这两项
    整体弃权，其余读数照算——没有流通股本就没有换手率，而换手率是筹码衰减的速度，
    硬算等于给一个任意的记忆长度。`instruments.batch` 全 A 覆盖率 100%，但免费档
    不一定拿得到。

    **筹码不在这里算**，它按需走 `UniverseAnalysis.chips_of`——理由写在那个方法上：
    它是这条链最贵的一项，却不进门槛也不进打分，为几百只过不了门槛的票算一遍
    再扔掉是纯浪费。
    """
    usable = {symbol: rows for symbol, rows in (bars_by_symbol or {}).items() if rows}
    if not usable:
        return UniverseAnalysis()

    snapshots = DailyTechnicalAnalyzer().analyze_many(usable)
    wyckoff = {signal.symbol: signal for signal in WyckoffAnalyzer().analyze_many(usable)}
    dips = {symbol: measure_dip(rows, snapshots.get(symbol)) for symbol, rows in usable.items()}
    shares = float_shares or {}
    volume_price = {
        symbol: measure_volume_price(rows, snapshots.get(symbol), shares.get(symbol))
        for symbol, rows in usable.items()
    }
    return UniverseAnalysis(
        bars=usable,
        snapshots=snapshots,
        wyckoff=wyckoff,
        dips=dips,
        volume_price=volume_price,
        float_shares=dict(shares),
    )


def select_from(
    analysis: UniverseAnalysis,
    *,
    regime: RegimeVerdict | None = None,
    scope: list[str] | None = None,
    names: dict[str, str | None] | None = None,
    sectors: dict[str, str] | None = None,
    sentiment: dict[str, Any] | None = None,
    contexts: dict[str, StockFactorContext] | None = None,
    provider: ScoreProvider | None = None,
    require_playbook: bool = True,
) -> UniverseResult:
    """在已有的结构读数上过打法门槛并打分。

    `require_playbook=False` 时门槛只做**标注**不做过滤：`advise` 走这条路，因为
    人工问一只票时该看到完整判断，包括「今天这只票没有可用打法，因为……」这句话
    本身——那正是「这只跌了很多的票为什么不买」的答案。

    `regime=None` 表示不施加情绪周期约束（同样是 `advise`）。这与
    `strategy/playbook.py:evaluate_playbooks` 的 `allowed=None` 是同一个语义。
    """
    result = UniverseResult()
    candidates = list(scope) if scope is not None else analysis.scope
    candidates = [symbol for symbol in candidates if symbol in analysis.snapshots]
    if not candidates:
        return result

    allowed = regime.allowed if regime is not None else None
    for symbol in candidates:
        result.matches[symbol] = evaluate_playbooks(
            analysis.snapshots[symbol],
            analysis.dips.get(symbol),
            analysis.wyckoff.get(symbol),
            allowed,
            analysis.volume_price.get(symbol),
        )

    qualified = (
        [symbol for symbol in candidates if result.matches[symbol].ok]
        if require_playbook
        else list(candidates)
    )
    result.notes.extend(_funnel_notes(candidates, qualified, result.matches, regime))
    if not qualified:
        return result

    readings = sentiment or {}
    scored = (provider or RuleBasedScoreProvider()).score_many(
        [
            ScoreInput(
                symbol=symbol,
                bars=analysis.bars[symbol],
                snapshot=analysis.snapshots[symbol],
                wyckoff=analysis.wyckoff.get(symbol),
                news=readings.get(symbol),
                dip=analysis.dips.get(symbol),
                volume_price=analysis.volume_price.get(symbol),
                context=(contexts or {}).get(symbol),
            )
            for symbol in qualified
            if symbol in analysis.bars
        ]
    )
    # `ScoreProvider.score_many()` promises best-first for the built-in provider, but
    # external/model providers may batch and return in service order. Candidate
    # selection is where ranking becomes a trading decision, so the order is
    # normalised here as the final gate.
    scored = sorted(scored, key=lambda item: (-item.score, item.symbol))

    for item in scored:
        match = result.matches[item.symbol]
        selected = SelectedSymbol(
            symbol=item.symbol,
            score=item.score,
            snapshot=analysis.snapshots[item.symbol],
            match=match,
            dip=analysis.dips.get(item.symbol) or measure_dip([]),
            name=(names or {}).get(item.symbol),
            sector=(sectors or {}).get(item.symbol),
            wyckoff=analysis.wyckoff.get(item.symbol),
            volume_price=analysis.volume_price.get(item.symbol),
            chips=analysis.chips_of(item.symbol),
            # 卖出判据对**每一只**都算，不只对持仓算：一只票的进场判据成立、
            # 同时天量滞涨也成立，这件事本身就该在买之前被看见。
            exits=assess_exits(
                volume_price=analysis.volume_price.get(item.symbol),
                snapshot=analysis.snapshots[item.symbol],
                regime=regime,
            ),
            news=readings.get(item.symbol),
            score_components=dict(item.components),
            factor_context=(contexts or {}).get(item.symbol),
            # 打法的判据写在打分理由**前面**：它回答「为什么这只票现在可以买」，
            # 而分数只回答「排第几」。读简报的人先要的是前者。
            reasons=match.reasons + item.reasons,
            warnings=item.warnings + match.warnings,
        )
        (result.selected if match.ok else result.rejected).append(selected)

    return result


def evaluate_universe(
    bars_by_symbol: dict[str, list[dict]],
    float_shares: dict[str, float] | None = None,
    **kwargs: Any,
) -> UniverseResult:
    """一次性入口：分析 + 选。只需要打一遍分的调用方用这个。"""
    return select_from(analyze_universe(bars_by_symbol, float_shares), **kwargs)


def _funnel_notes(
    scope: list[str],
    qualified: list[str],
    matches: dict[str, PlaybookMatch],
    regime: RegimeVerdict | None,
) -> list[str]:
    """把打法门槛的漏斗写进 notes。

    **被挡掉多少、为什么挡，必须说出来。** 只报「选出 N 只」会让读的人以为剩下的
    都不够好，而实际上今天可能是整个市场没有位置——这两件事该导向完全不同的动作。
    """
    if not scope:
        return []

    cycle = f"情绪周期 {regime.cycle.value}" if regime is not None else "无情绪周期约束"
    if not qualified:
        blocked = sorted(
            {item.value for match in matches.values() for item in match.blocked_by_regime}
        )
        detail = f"；其中 {'、'.join(blocked)} 被周期挡掉" if blocked else ""
        return [
            (f"⚠️ {cycle}：入围 {len(scope)} 只无一满足允许打法的入场判据{detail}，今日不产出候选")
        ]

    tally: dict[str, int] = {}
    for symbol in qualified:
        primary = matches[symbol].primary
        if primary is not None:
            tally[primary.value] = tally.get(primary.value, 0) + 1
    breakdown = "、".join(f"{name} {count} 只" for name, count in sorted(tally.items()))
    breakdown_text = f"（{breakdown}）" if breakdown else ""
    notes = [
        (
            f"{cycle}：{len(scope)} 只入围中 {len(qualified)} 只满足入场判据{breakdown_text}，"
            f"其余 {len(scope) - len(qualified)} 只无可用打法"
        )
    ]
    if len(qualified) < MIN_SETUP_CANDIDATES:
        notes.append(
            f"⚠️ 满足判据的仅 {len(qualified)} 只，少于最低样本 {MIN_SETUP_CANDIDATES}——"
            "今天位置本来就少，不放宽门槛回填"
        )
    return notes
