"""可交易性硬筛：流通市值与成交额的下限。

和 `st_filter.py` 同一类东西——**排除，不是扣分**。理由也一样：技术形态再好也
救不回一只买得进卖不出的票，而扣分迟早会被别的因子投票盖过去。

**为什么这一层此前是空的。** 候选池原来只过 ST 与致命公告，一只 15 亿流通、日成交
3000 万的票和茅台在筛选层面完全等价。对超短线这是个真窟窿：

- **流通市值太小**——20 亿以下拉一个板只要几千万，价格由不了基本面，也由不了你；
- **成交额太小**——8000 万以下的票，几百万的仓位就能打穿盘口。**超短线的核心风险
  不是买错，是卖不掉**，而这一层是唯一看这件事的地方。

门槛取得偏松（真做超短通常要求更高），因为这一层的职责是**排除不可交易**，不是
替使用者选票。要收紧在调用处传参。

数据来自 `adapters/market_scan.py`（新浪全市场扫描，一次 5.3 秒拿全 5529 只）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ashare_decision_support.adapters.market_scan import MIN_AMOUNT_YI, MIN_FLOAT_CAP_YI
from ashare_decision_support.screens.st_filter import ScreenVerdict


@dataclass(frozen=True)
class LiquidityStats:
    """筛完之后必须能说清楚「筛掉了什么、还有多少没测到」。"""

    checked: int = 0
    rejected: int = 0
    unmeasured: int = 0

    def note(self) -> str | None:
        if not self.rejected and not self.unmeasured:
            return None
        parts = []
        if self.rejected:
            parts.append(f"可交易性过滤掉 {self.rejected} 只（流通市值或成交额低于下限）")
        if self.unmeasured:
            # 静默放行没测到的票，读起来会像「全都测过且都合格」。必须说出来。
            parts.append(f"{self.unmeasured} 只没有全市场行情数据，本次未经该层过滤")
        return "；".join(parts)


class LiquidityScreen:
    """按流通市值与成交额下限排除不可交易的票。"""

    name = "liquidity-screen"

    def __init__(
        self,
        scan: Any | None,
        min_float_cap_yi: float = MIN_FLOAT_CAP_YI,
        min_amount_yi: float = MIN_AMOUNT_YI,
    ) -> None:
        self.scan = scan
        self.min_float_cap_yi = min_float_cap_yi
        self.min_amount_yi = min_amount_yi

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
        for symbol in symbols:
            if self.scan.get(symbol) is None:
                unmeasured += 1
                kept.append(symbol)
                continue
            verdict = self.check(symbol)
            if verdict.passed:
                kept.append(symbol)
            else:
                rejected.append(verdict)
        return kept, rejected, LiquidityStats(
            checked=len(symbols) - unmeasured,
            rejected=len(rejected),
            unmeasured=unmeasured,
        )
