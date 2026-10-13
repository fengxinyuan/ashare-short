"""净利润口径的业绩风险筛选与正面信息标注。

亏损和大幅预减可否决标的，正面信息不能抵消风险门槛。
缺失数据放行并说明未经筛选；续亏与首亏分别报告，续亏是否拦截由配置决定。
否决样本进入台账，供后续核对规则效果。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ashare_decision_support.adapters.earnings import (
    DIRECTION_POSITIVE,
    SEVERE_DECLINE_PCT,
    STRONG_GROWTH_PCT,
    EarningsForecast,
    EarningsSnapshot,
)
from ashare_decision_support.symbols import normalize_symbol

# 「预减」到什么幅度才升级成否决。单纯的「预减」可能只是同比 -5%，那是波动不是
# 业绩杀；过半的下滑才是。用类型 + 幅度两个条件而不是只看幅度，是因为幅度字段
# 实测有 +265430 / -31094 这种极值，单独用它做判据会被异常值支配。
SEVERE_DECLINE_THRESHOLD = SEVERE_DECLINE_PCT

# 只标注、不否决的类型。
WARNING_TYPES: frozenset[str] = frozenset({"预减", "略减", "不确定"})


@dataclass
class EarningsVerdict:
    """一个标的的业绩裁决。"""

    symbol: str
    forecast: EarningsForecast | None = None
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    catalysts: list[str] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocking)

    @property
    def is_strong_catalyst(self) -> bool:
        item = self.forecast
        return (
            item is not None
            and item.is_profitable_growth
            and item.is_fresh
            and item.change_pct is not None
            and item.change_pct >= STRONG_GROWTH_PCT
        )

    def to_dict(self) -> dict:
        return {
            "forecast": self.forecast.to_dict() if self.forecast is not None else None,
            "blocking": list(self.blocking),
            "warnings": list(self.warnings),
            "catalysts": list(self.catalysts),
            "strong_catalyst": self.is_strong_catalyst,
        }


@dataclass
class EarningsStats:
    """这一层实际做了什么。**被挡掉多少必须说出来。**"""

    evaluated: int = 0
    covered: int = 0
    blocked: int = 0
    warned: int = 0
    catalysts: int = 0
    period: str = ""
    active: bool = True

    def note(self) -> str | None:
        if not self.active:
            return "业绩数据不可用，**本次没有业绩风险否决**，候选未经该层过滤"
        if not self.evaluated:
            return None
        return (
            f"业绩层（{self.period} 报告期）：{self.evaluated} 只中 {self.covered} 只有披露，"
            f"否决 {self.blocked} 只、提示 {self.warned} 只、标注业绩催化 {self.catalysts} 只；"
            f"其余 {self.evaluated - self.covered} 只无预告（未披露不等于业绩差，按放行处理）"
        )


class EarningsScreen:
    """把一个报告期的业绩读数变成逐标的裁决。"""

    name = "earnings-screen"

    def __init__(
        self,
        snapshot: EarningsSnapshot | None = None,
        block_persistent_loss: bool = True,
        severe_decline_pct: float = SEVERE_DECLINE_THRESHOLD,
    ) -> None:
        self.snapshot = snapshot
        self.block_persistent_loss = block_persistent_loss
        self.severe_decline_pct = severe_decline_pct

    @property
    def active(self) -> bool:
        return self.snapshot is not None and self.snapshot.ok and bool(self.snapshot.forecasts)

    def evaluate(self, symbols: list[str]) -> dict[str, EarningsVerdict]:
        """逐标的裁决。**没有业绩数据的标的不出现在结果里**（= 放行）。"""
        if not self.active:
            return {}
        assert self.snapshot is not None

        result: dict[str, EarningsVerdict] = {}
        for symbol in symbols:
            item = self.snapshot.get(symbol)
            if item is None:
                continue
            verdict = self._judge(normalize_symbol(symbol), item)
            if verdict.blocking or verdict.warnings or verdict.catalysts:
                result[verdict.symbol] = verdict
        return result

    def filter(
        self, symbols: list[str]
    ) -> tuple[list[str], dict[str, EarningsVerdict], EarningsStats]:
        """返回 (放行的标的, 全部裁决, 统计)。

        取不到数据时**原样放行**并把这件事标进 `EarningsStats.active`，
        而不是返回一个看起来「全都通过了」的结果。
        """
        stats = EarningsStats(
            evaluated=len(symbols),
            period=self.snapshot.period if self.snapshot is not None else "",
            active=self.active,
        )
        if not self.active:
            return list(symbols), {}, stats

        verdicts = self.evaluate(symbols)
        assert self.snapshot is not None
        stats.covered = sum(1 for symbol in symbols if self.snapshot.get(symbol) is not None)
        stats.blocked = sum(1 for item in verdicts.values() if item.is_blocked)
        stats.warned = sum(1 for item in verdicts.values() if item.warnings)
        stats.catalysts = sum(1 for item in verdicts.values() if item.catalysts)

        blocked = {symbol for symbol, item in verdicts.items() if item.is_blocked}
        kept = [symbol for symbol in symbols if normalize_symbol(symbol) not in blocked]
        return kept, verdicts, stats

    def _judge(self, symbol: str, item: EarningsForecast) -> EarningsVerdict:
        verdict = EarningsVerdict(symbol=symbol, forecast=item)

        # 护栏 1：非净利润口径不参与否决，只提示。营收「略减」不是业绩杀。
        if not item.is_net_profit_basis:
            if item.direction != DIRECTION_POSITIVE:
                verdict.warnings.append(f"{item.headline}——该读数不是净利润口径，不作否决依据")
            return verdict

        if item.is_deteriorating_loss:
            verdict.blocking.append(f"{item.headline}——中报窗口内由盈转亏/亏损扩大，业绩杀风险")
            return verdict

        # 「减亏」：方向为正但净利润仍为负。既不该否决（在改善），也不该当成
        # 业绩催化（还在亏钱）。这是唯一一类必须落在两者之间的。
        if item.is_improving_loss:
            verdict.warnings.append(f"{item.headline}——仍处亏损，只是同比减亏，不构成业绩支撑")
            return verdict

        if item.is_loss:
            if self.block_persistent_loss:
                verdict.blocking.append(
                    f"{item.headline}——持续亏损（非新增恶化，但披露窗口内仍是被杀的一类）"
                )
            else:
                verdict.warnings.append(f"{item.headline}——持续亏损，未按否决处理")
            return verdict

        if (
            item.kind in WARNING_TYPES
            and item.change_pct is not None
            and item.change_pct <= self.severe_decline_pct
        ):
            verdict.blocking.append(f"{item.headline}——净利润同比下滑过半")
            return verdict

        if item.kind in WARNING_TYPES:
            verdict.warnings.append(item.headline)
            return verdict

        if item.direction == DIRECTION_POSITIVE:
            tier = "业绩催化" if item.is_fresh else "业绩支撑（非近期披露，多已消化）"
            verdict.catalysts.append(f"{tier}：{item.headline}")
        return verdict
