"""Hard screens applied before a symbol is allowed into the candidate list.

A screen is not a score. Anything here removes a symbol outright, because the
failure mode it guards against cannot be compensated for by a good chart: no
amount of technical strength makes a 退市风险警示 stock a sound ultra-short
position, and a scoring penalty would eventually be outvoted by other factors.
"""

from __future__ import annotations

from dataclasses import dataclass

# Name markers used by both exchanges for risk-flagged listings.
ST_NAME_MARKERS: tuple[str, ...] = ("ST", "*ST", "S*ST", "SST", "退市", "退")

# Board prefixes that an ultra-short retail workflow normally cannot or should
# not trade. Kept configurable because eligibility depends on your account.
DEFAULT_BLOCKED_PREFIXES: tuple[str, ...] = (
    "8",    # 北交所 830/831/833/836/838/839/870/871/872/873
    "4",    # 新三板 430/400
    "9",    # B 股 900 / 北交所 920
    "2",    # B 股 200
)


@dataclass(frozen=True)
class ScreenVerdict:
    symbol: str
    passed: bool
    reason: str = ""


class StScreen:
    """Filters risk-flagged and out-of-scope listings by name and code.

    Name matching is the primary test because the exchanges rename the security
    the moment the flag is applied, which makes it the earliest observable
    signal — earlier than the announcement feed, which lands the same evening.
    """

    name = "st-screen"

    def __init__(
        self,
        blocked_prefixes: tuple[str, ...] = DEFAULT_BLOCKED_PREFIXES,
        block_st: bool = True,
        block_delisting: bool = True,
    ) -> None:
        self.blocked_prefixes = blocked_prefixes
        self.block_st = block_st
        self.block_delisting = block_delisting

    def check(self, symbol: str, name: str | None = None) -> ScreenVerdict:
        code = symbol.split(".")[0].strip()
        label = (name or "").replace(" ", "").upper()

        if self.block_st and label:
            if "*ST" in label:
                return ScreenVerdict(symbol, False, f"{name} 为 *ST 退市风险警示股，直接排除")
            if "ST" in label:
                return ScreenVerdict(symbol, False, f"{name} 为 ST 风险警示股，直接排除")
        if self.block_delisting and label and "退" in label:
            return ScreenVerdict(symbol, False, f"{name} 处于退市整理阶段，直接排除")

        if code and code[0] in self.blocked_prefixes:
            return ScreenVerdict(symbol, False, f"{symbol} 属于非主板/非可交易板块（代码段 {code[:3]}），排除")

        return ScreenVerdict(symbol, True)

    def filter(self, names_by_symbol: dict[str, str | None]) -> tuple[list[str], list[ScreenVerdict]]:
        """Return (kept symbols, rejection verdicts)."""
        kept: list[str] = []
        rejected: list[ScreenVerdict] = []
        for symbol, name in names_by_symbol.items():
            verdict = self.check(symbol, name)
            if verdict.passed:
                kept.append(symbol)
            else:
                rejected.append(verdict)
        return kept, rejected
