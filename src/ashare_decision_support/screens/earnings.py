"""业绩风险否决与业绩催化标注。**刻意不对称。**

否决与标注不是同一个决定的两面，所以这一层对它们的处置强度不同：

| | 处置 | 理由 |
|---|---|---|
| 亏损 / 大幅预减 | **移除标的** | 中报窗口里最典型的「业绩杀」。这是公司自己签字披露的事实，不是推断 |
| 预增 / 扭亏 | **只标注，不加分** | 未经回测的读数不给权重（`DECISIONS.md` §六b） |

不对称的理由：**否决的错误是漏掉一只本可以做的票，加分的错误是买进一只不该买的票。**
前者可以被下一个交易日补回来，后者不能。而且加分那一侧有前科——上一轮把缩量做成
±6 分的权重，结果 60 条建议里 52 条是放量回调，**只要它是权重，就能被别的项买回来**。
标注不会被买回来。

**三条护栏**，每条都对应一种「这一层可能造成的静默错误」：

1. **只有净利润口径能否决。** 营业收入口径的「略减」不是业绩杀——一家公司可以
   营收微降而净利润翻倍。判据见 `EarningsForecast.is_net_profit_basis`。
2. **取不到数据一律放行。** 和 `LiquidityScreen` 同一条纪律：拿不到业绩去否决
   标的，等于让数据缺失冒充风险判断。放行并把「未经该层过滤」写进 note。
3. **续亏与首亏分开报。** 「早就在亏」和「刚开始亏」对市场是两件事。默认两者
   都否决（中报窗口里市场对亏损一视同仁地杀），但 `block_persistent_loss=False`
   可以只否决恶化的那批——**这个开关存在是因为它的正确取值尚未被台账验证**。

**已知代价，写在这里而不是藏起来：** 这道闸门会挡掉一部分**亏损但正当红的题材股**，
而它们在超短线上完全可能是当期最强的品种。这是一个真实的取舍，不是免费的改进。
台账（`journal/`）记录了每一条被否决的理由，60 个交易日后可以回答「被业绩否决的
那批，如果买了会怎样」——在那之前，这个取舍是按保守方向拍的板，不是被验证过的。
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
            return (
                "业绩数据不可用，**本次没有业绩风险否决**，候选未经该层过滤"
            )
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

    def filter(self, symbols: list[str]) -> tuple[list[str], dict[str, EarningsVerdict], EarningsStats]:
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

    # ---- internals -------------------------------------------------------

    def _judge(self, symbol: str, item: EarningsForecast) -> EarningsVerdict:
        verdict = EarningsVerdict(symbol=symbol, forecast=item)

        # 护栏 1：非净利润口径不参与否决，只提示。营收「略减」不是业绩杀。
        if not item.is_net_profit_basis:
            if item.direction != DIRECTION_POSITIVE:
                verdict.warnings.append(
                    f"{item.headline}——该读数不是净利润口径，不作否决依据"
                )
            return verdict

        if item.is_deteriorating_loss:
            verdict.blocking.append(
                f"{item.headline}——中报窗口内由盈转亏/亏损扩大，业绩杀风险"
            )
            return verdict

        # 「减亏」：方向为正但净利润仍为负。既不该否决（在改善），也不该当成
        # 业绩催化（还在亏钱）。这是唯一一类必须落在两者之间的。
        if item.is_improving_loss:
            verdict.warnings.append(
                f"{item.headline}——仍处亏损，只是同比减亏，不构成业绩支撑"
            )
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
            verdict.blocking.append(
                f"{item.headline}——净利润同比下滑过半"
            )
            return verdict

        if item.kind in WARNING_TYPES:
            verdict.warnings.append(item.headline)
            return verdict

        if item.direction == DIRECTION_POSITIVE:
            tier = "业绩催化" if item.is_fresh else "业绩支撑（非近期披露，多已消化）"
            verdict.catalysts.append(f"{tier}：{item.headline}")
        return verdict