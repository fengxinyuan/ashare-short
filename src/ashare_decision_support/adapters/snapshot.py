"""Real-time snapshots with a full five-level order book, from Sina and Tencent.

Why this exists after `sina_intraday.py` already fetches minute bars: the
monitor needs *current price* for N symbols every couple of minutes, and a
minute-bar call costs one HTTP round trip per symbol (measured 1.0 s serial,
0.19 s at 8 workers). These endpoints answer for a **batch** of symbols in one
request — measured 161 ms for one symbol and sub-second for hundreds — and they
carry the order book on top.

Two things this makes possible that the project previously recorded as
impossible:

- **Order-book depth.** `docs/SOLUTION.md` used to state that five-level depth
  needed the paid tier because 东财's `stock_bid_ask_em` (on the unreachable
  `push2` host) was the only known source. Sina's quote host is reachable and
  carries bid1-5/ask1-5. See `docs/DATA_SOURCE_AUDIT.md`.
- **Real limit prices.** Tencent returns 涨停价/跌停价 directly, so limit-up
  detection no longer has to approximate with "rose ≥ 9.5%" — an approximation
  that cannot tell a 10% board from a 20% ChiNext/STAR board at all.

Standard library only. `easyquotation` wraps these same endpoints, but the
parsing is thirty lines and the package deliberately has no HTTP dependency.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ashare_decision_support.symbols import normalize_symbol

SINA_URL = "https://hq.sinajs.cn/list="
SINA_REFERER = "https://finance.sina.com.cn"
TENCENT_URL = "https://qt.gtimg.cn/q="
TENCENT_REFERER = "https://gu.qq.com"

USER_AGENT = "Mozilla/5.0"
DEFAULT_TIMEOUT = 10.0

# Symbols per request. Both hosts accept long comma-separated lists; keeping
# batches bounded avoids a URL long enough to be rejected by an intermediary.
BATCH_SIZE = 400
MAX_WORKERS = 4


class SnapshotError(RuntimeError):
    """Raised only when no symbol at all could be fetched."""


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    volume: float


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    name: str | None
    last_price: float
    prev_close: float | None
    open_price: float | None
    high: float | None
    low: float | None
    volume: float | None
    amount: float | None
    as_of: datetime | None
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    limit_up: float | None = None
    limit_down: float | None = None
    source: str = "sina"

    @property
    def change_pct(self) -> float | None:
        if not self.prev_close:
            return None
        return self.last_price / self.prev_close - 1.0

    @property
    def at_limit_up(self) -> bool | None:
        """True/False when the real limit price is known, None when it is not.

        Deliberately tri-state. Falling back to a percentage rule here would
        silently misclassify every 20% board, which is exactly the failure the
        limit price was fetched to avoid.
        """
        if self.limit_up is None:
            return None
        return self.last_price >= self.limit_up - 1e-6

    @property
    def at_limit_down(self) -> bool | None:
        if self.limit_down is None:
            return None
        return self.last_price <= self.limit_down + 1e-6

    @property
    def spread_pct(self) -> float | None:
        if not self.bids or not self.asks or not self.last_price:
            return None
        return (self.asks[0].price - self.bids[0].price) / self.last_price


class SnapshotAdapter:
    """Batch quotes plus five-level depth. Never raises for a single symbol."""

    name = "sina-tencent-snapshot"

    def __init__(
        self,
        prefer: str = "sina",
        timeout: float = DEFAULT_TIMEOUT,
        opener: Any = None,
        max_workers: int = MAX_WORKERS,
    ) -> None:
        self.prefer = prefer
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self.max_workers = max(1, int(max_workers))

    def get_snapshots(self, symbols: list[str]) -> tuple[dict[str, Snapshot], list[str]]:
        """Return ({symbol: Snapshot}, notes)."""
        codes = [normalize_symbol(item) for item in symbols if normalize_symbol(item)]
        if not codes:
            return {}, []

        fetch = self._fetch_sina if self.prefer == "sina" else self._fetch_tencent
        batches = [codes[index : index + BATCH_SIZE] for index in range(0, len(codes), BATCH_SIZE)]
        workers = min(self.max_workers, len(batches))

        results: dict[str, Snapshot] = {}
        notes: list[str] = []
        if workers == 1:
            pairs = [fetch(batch) for batch in batches]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pairs = list(pool.map(fetch, batches))
        for parsed, batch_notes in pairs:
            results.update(parsed)
            notes.extend(batch_notes)

        missing = [code for code in codes if code not in results]
        if missing:
            notes.append(f"以下标的未取到快照：{'、'.join(missing[:8])}")
        return results, notes

    def get_limit_prices(self, symbols: list[str]) -> tuple[dict[str, tuple[float, float]], list[str]]:
        """涨停价/跌停价 from Tencent, which publishes them directly."""
        codes = [normalize_symbol(item) for item in symbols if normalize_symbol(item)]
        if not codes:
            return {}, []
        parsed, notes = self._fetch_tencent(codes)
        return (
            {
                symbol: (snap.limit_up, snap.limit_down)
                for symbol, snap in parsed.items()
                if snap.limit_up is not None and snap.limit_down is not None
            },
            notes,
        )

    def health_check(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            parsed, _ = self._fetch_sina(["600519.SH"])
            ok = bool(parsed)
            error = None
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"
        return {
            "source": self.name,
            "mode": "free",
            "reachable": ok,
            "error": error,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "capabilities": {
                "supports_batch_quotes": True,
                "supports_orderbook": True,
                "supports_limit_prices": True,
                "raw_prices": True,
            },
        }

    # ---- fetchers --------------------------------------------------------

    def _fetch_sina(self, codes: list[str]) -> tuple[dict[str, Snapshot], list[str]]:
        query = ",".join(_sina_code(code) for code in codes)
        try:
            body = self._get(f"{SINA_URL}{query}", SINA_REFERER, encoding="gbk")
        except Exception as exc:
            return {}, [f"新浪快照请求失败（{type(exc).__name__}），本轮缺 {len(codes)} 只"]

        parsed: dict[str, Snapshot] = {}
        for line, code in zip(body.splitlines(), codes):
            snapshot = _parse_sina(line, code)
            if snapshot is not None:
                parsed[code] = snapshot
        return parsed, []

    def _fetch_tencent(self, codes: list[str]) -> tuple[dict[str, Snapshot], list[str]]:
        query = ",".join(_sina_code(code) for code in codes)
        try:
            body = self._get(f"{TENCENT_URL}{query}", TENCENT_REFERER, encoding="gbk")
        except Exception as exc:
            return {}, [f"腾讯快照请求失败（{type(exc).__name__}），本轮缺 {len(codes)} 只"]

        parsed: dict[str, Snapshot] = {}
        for line, code in zip(body.splitlines(), codes):
            snapshot = _parse_tencent(line, code)
            if snapshot is not None:
                parsed[code] = snapshot
        return parsed, []

    def _get(self, url: str, referer: str, encoding: str) -> str:
        request = urllib.request.Request(
            url, headers={"Referer": referer, "User-Agent": USER_AGENT}
        )
        with self._opener(request, timeout=self.timeout) as response:
            return response.read().decode(encoding, errors="replace")


# ---- parsing -------------------------------------------------------------


def _sina_code(symbol: str) -> str:
    code, _, suffix = normalize_symbol(symbol).partition(".")
    return f"{suffix.lower()}{code}" if suffix else code


def _parse_sina(line: str, symbol: str) -> Snapshot | None:
    """`var hq_str_sh600519="贵州茅台,open,prevclose,last,high,low,...";`"""
    if '"' not in line:
        return None
    payload = line.split('"', 2)[1]
    fields = payload.split(",")
    if len(fields) < 32:
        return None

    last = _float(fields[3])
    if not last:
        # A halted or unlisted symbol answers with zeros. Publishing 0.00 as a
        # price is worse than reporting the symbol as missing.
        return None

    bids = [
        OrderBookLevel(price=_float(fields[11 + i * 2]) or 0.0, volume=_float(fields[10 + i * 2]) or 0.0)
        for i in range(5)
    ]
    asks = [
        OrderBookLevel(price=_float(fields[21 + i * 2]) or 0.0, volume=_float(fields[20 + i * 2]) or 0.0)
        for i in range(5)
    ]
    return Snapshot(
        symbol=symbol,
        name=fields[0] or None,
        last_price=last,
        prev_close=_float(fields[2]),
        open_price=_float(fields[1]),
        high=_float(fields[4]),
        low=_float(fields[5]),
        volume=_float(fields[8]),
        amount=_float(fields[9]),
        as_of=_parse_dt(fields[30], fields[31]),
        bids=[level for level in bids if level.price > 0],
        asks=[level for level in asks if level.price > 0],
        source="sina",
    )


def _parse_tencent(line: str, symbol: str) -> Snapshot | None:
    """`v_sh600519="1~贵州茅台~600519~last~prevclose~open~volume~...";`"""
    if '"' not in line:
        return None
    fields = line.split('"', 2)[1].split("~")
    if len(fields) < 50:
        return None

    last = _float(fields[3])
    if not last:
        return None

    bids = [
        OrderBookLevel(price=_float(fields[9 + i * 2]) or 0.0, volume=_float(fields[10 + i * 2]) or 0.0)
        for i in range(5)
    ]
    asks = [
        OrderBookLevel(price=_float(fields[19 + i * 2]) or 0.0, volume=_float(fields[20 + i * 2]) or 0.0)
        for i in range(5)
    ]
    return Snapshot(
        symbol=symbol,
        name=fields[1] or None,
        last_price=last,
        prev_close=_float(fields[4]),
        open_price=_float(fields[5]),
        high=_float(fields[33]) if len(fields) > 33 else None,
        low=_float(fields[34]) if len(fields) > 34 else None,
        volume=_float(fields[6]),
        amount=_float(fields[37]) if len(fields) > 37 else None,
        as_of=_parse_compact_dt(fields[30] if len(fields) > 30 else ""),
        bids=[level for level in bids if level.price > 0],
        asks=[level for level in asks if level.price > 0],
        limit_up=_float(fields[47]) if len(fields) > 47 else None,
        limit_down=_float(fields[48]) if len(fields) > 48 else None,
        source="tencent",
    )


def _float(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number != 0.0 else None


def _parse_dt(day: str, clock: str) -> datetime | None:
    try:
        return datetime.strptime(f"{day.strip()} {clock.strip()}", "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _parse_compact_dt(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip()[:14], "%Y%m%d%H%M%S")
    except (TypeError, ValueError):
        return None
