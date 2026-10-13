"""按流通市值与成交额下限进行可交易性筛选。

数据由全市场快照提供，阈值可配置。未达门槛时排除；缺失读数按未知处理并说明覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ashare_decision_support.adapters.market_scan import MIN_AMOUNT_YI, MIN_FLOAT_CAP_YI
from ashare_decision_support.scoring.liquidity_factor import calculate_liquidity_factor


@dataclass(frozen=True)
class ScreenVerdict:
    """One liquidity-screen decision."""

    symbol: str
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class LiquidityStats:
    """筛完之后必须能说清楚「筛掉了什么、还有多少没测到」。"""

    checked: int = 0
    rejected: int = 0
    unmeasured: int = 0
    missing_fields: int = 0
    # 成交额门槛因为扫描取自盘中而没开。它和「没测到」不同：市值这一半仍然筛了。
    amount_gate_off: bool = False
    turnover_yi: float | None = None

    def note(self) -> str | None:
        if (
            not self.rejected
            and not self.unmeasured
            and not self.missing_fields
            and not self.amount_gate_off
        ):
            return None
        parts = []
        if self.rejected:
            parts.append(f"可交易性过滤掉 {self.rejected} 只（流通市值或成交额低于下限）")
        if self.unmeasured:
            # 静默放行没测到的票，读起来会像「全都测过且都合格」。必须说出来。
            parts.append(f"{self.unmeasured} 只没有全市场行情数据，本次未经该层过滤")
        if self.missing_fields:
            parts.append(
                f"{self.missing_fields} 只行情缺少必要的流通市值或成交额，本次只按已知字段过滤"
            )
        if self.amount_gate_off:
            parts.append("全市场扫描取自盘中，成交额还在形成中，本次可交易性只按流通市值筛")
        return "；".join(parts)


class LiquidityScreen:
    """按流通市值与成交额下限排除不可交易的票。"""

    name = "liquidity-screen"

    def __init__(
        self,
        scan: Any | None,
        min_float_cap_yi: float = MIN_FLOAT_CAP_YI,
        min_amount_yi: float = MIN_AMOUNT_YI,
        market_turnover_yi: float | None = None,
    ) -> None:
        self.scan = scan
        self.min_float_cap_yi = min_float_cap_yi
        # 盘中取到的成交额是半截交易日的：09:33 跑一次，全场大半都「低于 0.8 亿」。
        # 2026-09-14 实测剔了 958 只——比正常盘前多出六百只，全是正常票。那时成交额
        # 门槛不能用，只留流通市值（它不随时间走）。
        self.amount_gate_off = bool(getattr(scan, "session_in_progress", False))
        turnover = market_turnover_yi or getattr(scan, "total_turnover_yi", None)
        self.liquidity_regime = calculate_liquidity_factor(turnover)
        scaled_amount = self.liquidity_regime.scale_threshold(min_amount_yi)
        self.min_amount_yi = 0.0 if self.amount_gate_off else scaled_amount

    @property
    def available(self) -> bool:
        return self.scan is not None and getattr(self.scan, "ok", False) and len(self.scan) > 0

    def check(self, symbol: str) -> ScreenVerdict:
        if not self.available:
            return ScreenVerdict(symbol, True)
        row = self.scan.get(symbol)
        if row is None:
            return ScreenVerdict(symbol, True)
        if row.tradable(self.min_float_cap_yi, self.min_amount_yi):
            return ScreenVerdict(symbol, True)

        why = []
        if row.float_cap_yi is not None and row.float_cap_yi < self.min_float_cap_yi:
            why.append(f"流通市值 {row.float_cap_yi:.0f} 亿 < {self.min_float_cap_yi:.0f} 亿")
        if row.amount_yi is not None and row.amount_yi < self.min_amount_yi:
            why.append(f"成交额 {row.amount_yi:.2f} 亿 < {self.min_amount_yi:.2f} 亿")
        return ScreenVerdict(symbol, False, f"{row.name} {'、'.join(why)}，超短线进出困难")

    def filter(self, symbols: list[str]) -> tuple[list[str], list[ScreenVerdict], LiquidityStats]:
        if not self.available:
            return list(symbols), [], LiquidityStats(checked=0, unmeasured=len(symbols))

        kept: list[str] = []
        rejected: list[ScreenVerdict] = []
        unmeasured = 0
        missing_fields = 0
        for symbol in symbols:
            row = self.scan.get(symbol)
            if row is None:
                unmeasured += 1
                kept.append(symbol)
                continue
            if row.float_cap_yi is None or (not self.amount_gate_off and row.amount_yi is None):
                missing_fields += 1
            verdict = self.check(symbol)
            if verdict.passed:
                kept.append(symbol)
            else:
                rejected.append(verdict)
        return (
            kept,
            rejected,
            LiquidityStats(
                checked=len(symbols) - unmeasured,
                rejected=len(rejected),
                unmeasured=unmeasured,
                missing_fields=missing_fields,
                amount_gate_off=self.amount_gate_off,
                turnover_yi=self.liquidity_regime.turnover_yi,
            ),
        )
