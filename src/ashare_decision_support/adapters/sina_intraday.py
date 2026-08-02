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

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

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
        result: dict[str, list[dict]] = {}
        failed: list[str] = []

        def fetch(symbol: str) -> tuple[str, list[dict] | None]:
            try:
                table = self.ak.stock_zh_a_minute(
                    symbol=sina_symbol(symbol), period=period, adjust=""
                )
            except Exception:
                return symbol, None
            bars = _to_bars(table, symbol)
            return symbol, bars[-max_bars:] if max_bars is not None else bars

        if workers == 1:
            pairs = [fetch(symbol) for symbol in symbols]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pairs = list(pool.map(fetch, symbols))

        for symbol, bars in pairs:
            if bars:
                result[symbol] = bars
            else:
                failed.append(symbol)

        notes = [f"以下标的分钟线拉取失败：{'、'.join(failed)}"] if failed else []
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
    rows = _rows(table)
    bars: list[dict] = []
    for row in rows:
        timestamp = _parse_ts(row.get("day"))
        close = _float(row.get("close"))
        if timestamp is None or close is None:
            continue
        bars.append(
            {
                "symbol": symbol,
                "timestamp": timestamp,
                "open": _float(row.get("open")),
                "high": _float(row.get("high")),
                "low": _float(row.get("low")),
                "close": close,
                "volume": _float(row.get("volume")),
                "amount": _float(row.get("amount")),
            }
        )
    return bars


def _rows(table: Any) -> list[dict]:
    if table is None:
        return []
    if isinstance(table, list):
        return [row for row in table if isinstance(row, dict)]
    to_dict = getattr(table, "to_dict", None)
    if callable(to_dict):
        return list(table.to_dict(orient="records"))
    return []


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


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number
