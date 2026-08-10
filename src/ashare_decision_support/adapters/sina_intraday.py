"""Minute bars from Sina.

Why Sina and not 东方财富: the 东财 `push2` family (bulk quotes, board lists,
`stock_zh_a_hist_min_em`) and `push2ex` (逐笔成交) are unreachable from this
host — measured, not assumed. Sina's minute endpoint answers reliably and is
what the intraday layer runs on.

`adjust=""` returns **raw prices**, which is what an order book shows. Note
that this is *not* what makes the levels in `advice/` quotable — the daily
series is 比例前复权, which anchors on the latest bar and therefore already
carries history in today's price scale (2026-08-02 实测 400 只，前复权与不复权
的最新一根完全一致). Raw minute bars and 比例前复权 日线 agree on today's scale
by construction; the two series can be compared directly, and `advice/` relies
on exactly that.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ashare_decision_support.fanout import gather
from ashare_decision_support.numeric import finite
from ashare_decision_support.tabular import records

# Sina caps the response at this many bars per period, so the window each
# period covers differs: 1m ≈ 8 sessions, 5m ≈ 41, 60m ≈ 2 years.
SINA_MAX_BARS = 1970

# One symbol per HTTP round trip, and the trip is almost entirely network wait:
# measured 1.0 s/symbol serial, 0.19 s/symbol at 8 workers. The monitor loop
# only closes at that speed, so concurrency here is a requirement rather than
# an optimisation. Kept modest because sustained polling against Sina has not
# been rate-limit tested — see docs/DATA_SOURCES.md §九.
DEFAULT_WORKERS = 8

SUPPORTED_PERIODS: tuple[str, ...] = ("1", "5", "15", "30", "60")


class IntradayDataError(RuntimeError):
    """Raised when no intraday source can serve the request at all."""


class SinaIntradayAdapter:
    """Fetches minute bars. Never raises for a single bad symbol."""

    name = "sina-intraday"

    def __init__(self, ak_module: Any | None = None, max_workers: int = DEFAULT_WORKERS) -> None:
        if ak_module is None:
            try:
                import akshare as ak_module  # type: ignore
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise IntradayDataError(
                    "akshare is not installed. Install it with `pip install -e .[akshare]`."
                ) from exc
        self.ak = ak_module
        self.max_workers = max(1, int(max_workers))

    def get_minute_bars(
        self,
        symbols: list[str],
        period: str = "5",
        max_bars: int | None = None,
        max_workers: int | None = None,
    ) -> tuple[dict[str, list[dict]], list[str]]:
        """Return ({symbol: bars}, notes). A failed symbol yields a note, not an error."""
        if period not in SUPPORTED_PERIODS:
            raise ValueError(f"unsupported period {period!r}; expected one of {SUPPORTED_PERIODS}")
        if not symbols:
            return {}, []

        workers = min(max(1, int(max_workers or self.max_workers)), len(symbols))

        def fetch(symbol: str) -> list[dict]:
            table = self.ak.stock_zh_a_minute(symbol=sina_symbol(symbol), period=period, adjust="")
            bars = _to_bars(table, symbol)
            return bars[-max_bars:] if max_bars is not None else bars

        done, failed = gather(symbols, fetch, max_workers=workers)

        # 「取不到」和「今天没有分时」是两件事：前者要报出原因，后者只是这只票
        # 今天没成交。原先两种都并进一句「拉取失败」，排查时看不出该查网络还是
        # 查标的。
        result = {symbol: bars for symbol, bars in done if bars}
        empty = [symbol for symbol, bars in done if not bars]

        notes: list[str] = []
        if failed:
            reasons = "、".join(f"{symbol}（{type(exc).__name__}）" for symbol, exc in failed[:8])
            notes.append(f"以下标的分钟线拉取失败：{reasons}")
        if empty:
            notes.append(f"以下标的分钟线为空：{'、'.join(empty[:8])}")
        return result, notes

    def health_check(self) -> dict[str, Any]:
        return {
            "source": self.name,
            "mode": "free",
            "capabilities": {
                "supports_minute": True,
                "supports_tick": False,
                "supports_orderbook": False,
                "raw_prices": True,
            },
            "periods": list(SUPPORTED_PERIODS),
            "max_bars_per_request": SINA_MAX_BARS,
        }


def sina_symbol(symbol: str) -> str:
    """`600519.SH` / `600519` -> `sh600519`."""
    text = symbol.strip().lower()
    if text.startswith(("sh", "sz", "bj")):
        return text
    code, _, suffix = text.partition(".")
    if suffix in {"sh", "sz", "bj"}:
        return f"{suffix}{code}"
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return f"sh{code}"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return f"sz{code}"
    return f"bj{code}"


def _to_bars(table: Any, symbol: str) -> list[dict]:
    rows = records(table)
    bars: list[dict] = []
    for row in rows:
        timestamp = _parse_ts(row.get("day"))
        close = finite(row.get("close"))
        if timestamp is None or close is None:
            continue
        bars.append(
            {
                "symbol": symbol,
                "timestamp": timestamp,
                "open": finite(row.get("open")),
                "high": finite(row.get("high")),
                "low": finite(row.get("low")),
                "close": close,
                "volume": finite(row.get("volume")),
                "amount": finite(row.get("amount")),
            }
        )
    return bars



def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None
