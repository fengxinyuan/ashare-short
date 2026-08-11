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
  carries bid1-5/ask1-5. See `docs/DATA_SOURCES.md`.
- **Real limit prices.** Tencent returns 涨停价/跌停价 directly, so limit-up
  detection no longer has to approximate with "rose ≥ 9.5%" — an approximation
  that cannot tell a 10% board from a 20% ChiNext/STAR board at all.

腾讯那一路**同时**带五档和涨跌停价，所以要五档的调用方直接 `prefer="tencent"`
一次请求拿全，不需要为了涨跌停价再打一次新浪。代价是它的量纲是手/万元——已在
解析层归一化，见 `Snapshot` 的量纲契约。

Standard library only. `easyquotation` wraps these same endpoints, but the
parsing is thirty lines and the package deliberately has no HTTP dependency.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ashare_decision_support.fanout import gather
from ashare_decision_support.numeric import positive
from ashare_decision_support.symbols import normalize_symbol

SINA_URL = "https://hq.sinajs.cn/list="
SINA_REFERER = "https://finance.sina.com.cn"
TENCENT_URL = "https://qt.gtimg.cn/q="
TENCENT_REFERER = "https://gu.qq.com"

USER_AGENT = "Mozilla/5.0"
DEFAULT_TIMEOUT = 10.0

# 腾讯的量纲和新浪不一样：成交量与五档挂单量记**手**，成交额记**万元**。
# 2026-08-02 实测 600519，两边同一时刻的读数比值是 99.999 和 10000.004——不是
# 近似，是单位。归一化放在解析层，因为 `Snapshot` 只有一个 `volume` 字段，让它
# 的含义取决于 `source` 等于把量纲错误埋进每一个下游消费者。
TENCENT_VOLUME_TO_SHARES = 100.0
TENCENT_AMOUNT_TO_YUAN = 10000.0

# Symbols per request. Both hosts accept long comma-separated lists; keeping
# batches bounded avoids a URL long enough to be rejected by an intermediary.
BATCH_SIZE = 400
MAX_WORKERS = 4


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    volume: float


@dataclass(frozen=True)
class Snapshot:
    """一个时点的行情快照。

    **量纲契约**：`volume` 一律为**股**，`amount` 一律为**元**，五档的 `volume`
    一律为**股**——无论 `source` 是新浪还是腾讯。腾讯原始口径是手/万元，在解析
    层就换算掉了（见 `TENCENT_VOLUME_TO_SHARES`）。

    代价是腾讯的五档挂单量只精确到手：实测同一时刻新浪报 930020 股、腾讯报
    9300 手（= 930000 股），差的是那 20 股零股。做委比、做挂单墙都用不到这个
    精度，但如果哪天要按股数对账，得知道腾讯那一路是截断过的。
    """

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

        results: dict[str, Snapshot] = {}
        notes: list[str] = []
        done, failed = gather(batches, fetch, max_workers=self.max_workers)
        for _batch, (parsed, batch_notes) in done:
            results.update(parsed)
            notes.extend(batch_notes)
        # `_fetch_sina` / `_fetch_tencent` 自己兜住了请求异常，走到这里的是解析
        # 阶段的意外。这一批整批缺失，但别的批次照常——类名要留下，否则「未取到
        # 快照」那条 note 读起来像是服务端少回了几行。
        for batch, exc in failed:
            notes.append(f"快照批次（{len(batch)} 只）解析失败（{type(exc).__name__}）")

        missing = [code for code in codes if code not in results]
        if missing:
            notes.append(f"以下标的未取到快照：{'、'.join(missing[:8])}")
        return results, notes

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
        return self._fetch(codes, SINA_URL, SINA_REFERER, "新浪", _parse_sina)

    def _fetch_tencent(self, codes: list[str]) -> tuple[dict[str, Snapshot], list[str]]:
        return self._fetch(codes, TENCENT_URL, TENCENT_REFERER, "腾讯", _parse_tencent)

    def _fetch(
        self,
        codes: list[str],
        url: str,
        referer: str,
        label: str,
        parse: Callable[[str, str], Snapshot | None],
    ) -> tuple[dict[str, Snapshot], list[str]]:
        """一路快照的取数与解析。两路只差 URL / Referer / 标签 / 解析器。

        两个入口保留成独立方法是因为 `get_snapshots` 把它们当**可互换的策略**选
        （`fetch = self._fetch_sina if ... else self._fetch_tencent`），`health_check`
        也直接点名新浪那一路。差异收在参数里，取数与「截断即缺失」那段循环只有一份。
        """
        query = ",".join(_sina_code(code) for code in codes)
        try:
            body = self._get(f"{url}{query}", referer, encoding="gbk")
        except Exception as exc:
            return {}, [f"{label}快照请求失败（{type(exc).__name__}），本轮缺 {len(codes)} 只"]

        parsed: dict[str, Snapshot] = {}
        # 服务端可能少回几行；截断即缺失，缺哪几只由 `get_snapshots` 统一报。
        for line, code in zip(body.splitlines(), codes, strict=False):
            snapshot = parse(line, code)
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

    last = positive(fields[3])
    if not last:
        # A halted or unlisted symbol answers with zeros. Publishing 0.00 as a
        # price is worse than reporting the symbol as missing.
        return None

    bids = [
        OrderBookLevel(price=positive(fields[11 + i * 2]) or 0.0, volume=positive(fields[10 + i * 2]) or 0.0)
        for i in range(5)
    ]
    asks = [
        OrderBookLevel(price=positive(fields[21 + i * 2]) or 0.0, volume=positive(fields[20 + i * 2]) or 0.0)
        for i in range(5)
    ]
    return Snapshot(
        symbol=symbol,
        name=fields[0] or None,
        last_price=last,
        prev_close=positive(fields[2]),
        open_price=positive(fields[1]),
        high=positive(fields[4]),
        low=positive(fields[5]),
        volume=positive(fields[8]),
        amount=positive(fields[9]),
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

    last = positive(fields[3])
    if not last:
        return None

    bids = [
        OrderBookLevel(
            price=positive(fields[9 + i * 2]) or 0.0,
            volume=(positive(fields[10 + i * 2]) or 0.0) * TENCENT_VOLUME_TO_SHARES,
        )
        for i in range(5)
    ]
    asks = [
        OrderBookLevel(
            price=positive(fields[19 + i * 2]) or 0.0,
            volume=(positive(fields[20 + i * 2]) or 0.0) * TENCENT_VOLUME_TO_SHARES,
        )
        for i in range(5)
    ]
    volume = positive(fields[6])
    amount = positive(fields[37]) if len(fields) > 37 else None
    return Snapshot(
        symbol=symbol,
        name=fields[1] or None,
        last_price=last,
        prev_close=positive(fields[4]),
        open_price=positive(fields[5]),
        high=positive(fields[33]) if len(fields) > 33 else None,
        low=positive(fields[34]) if len(fields) > 34 else None,
        volume=volume * TENCENT_VOLUME_TO_SHARES if volume else None,
        amount=amount * TENCENT_AMOUNT_TO_YUAN if amount else None,
        as_of=_parse_compact_dt(fields[30] if len(fields) > 30 else ""),
        bids=[level for level in bids if level.price > 0],
        asks=[level for level in asks if level.price > 0],
        limit_up=positive(fields[47]) if len(fields) > 47 else None,
        limit_down=positive(fields[48]) if len(fields) > 48 else None,
        source="tencent",
    )


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


def default_snapshot_adapter() -> SnapshotAdapter | None:
    """腾讯那一路：同一份响应里既有五档也有真实涨跌停价，一次请求拿全。

    构造失败返回 None——盘口是加分项，不该让一个可选数据源决定监控能不能开跑。
    """
    try:
        return SnapshotAdapter(prefer="tencent")
    except Exception:
        return None
