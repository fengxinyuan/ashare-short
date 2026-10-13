"""TickFlow 付费档批量分钟线，与新浪分钟线使用同一接口。

通过 klines.batch 取数，单次最多 5000 根；免费档由 build_intraday_adapter 回退新浪。
输出成交量统一为股，时间统一为北京时间，价格默认不复权。
OHLC 保留四位小数以消除复权浮点尾差；不同厂商的分钟线不混入同一次计算。
接口实测与数据口径见 docs/DATA_SOURCES.md。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from ashare_decision_support.adapters.base import SUPPORTED_PERIODS
from ashare_decision_support.adapters.validate import LOTS_TO_SHARES
from ashare_decision_support.numeric import finite
from ashare_decision_support.schedule.timezone import beijing_from_epoch

# 仓内沿用的周期写法（"1"/"5"/…）到 TickFlow 写法（"1m"/"5m"/…）。
PERIOD_SUFFIX = "m"

# 服务端单次返回上限，实测钉死在这个数上，调大 `count` 无效。
MAX_BARS_PER_REQUEST = 5000

# 每次批量请求的标的数。SDK 内部还会再分批，这里只是不把一个 URL 撑爆。
SYMBOL_BATCH = 100

# 显式请求不复权价格，与新浪分钟线和实际挂单价保持一致。
ADJUST_RAW = "none"


class TickFlowIntradayAdapter:
    """分钟线，接口与 `SinaIntradayAdapter` 一致（`get_minute_bars`）。"""

    name = "tickflow-intraday"

    def __init__(self, client: Any, default_bars: int = 1200, adjust: str = ADJUST_RAW) -> None:
        self.client = client
        # 新浪那路一次给 1970 根，取相近量级，好让两条路径的下游行为一致。
        self.default_bars = default_bars
        self.adjust = adjust

    def get_minute_bars(
        self,
        symbols: list[str],
        period: str = "5",
        max_bars: int | None = None,
        # 保留与新浪适配器一致的签名；本实现使用批量接口，不创建线程池。
        max_workers: int | None = None,
    ) -> tuple[dict[str, list[dict]], list[str]]:
        if period not in SUPPORTED_PERIODS:
            raise ValueError(f"unsupported period {period!r}; expected one of {SUPPORTED_PERIODS}")
        if not symbols:
            return {}, []

        count = min(max_bars or self.default_bars, MAX_BARS_PER_REQUEST)
        result: dict[str, list[dict]] = {}
        failed: list[str] = []

        for start in range(0, len(symbols), SYMBOL_BATCH):
            chunk = symbols[start : start + SYMBOL_BATCH]
            try:
                payload = self.client.klines.batch(
                    symbols=chunk,
                    period=f"{period}{PERIOD_SUFFIX}",
                    count=count,
                    adjust=self.adjust,
                )
            except Exception:
                failed.extend(chunk)
                continue
            if not isinstance(payload, dict):
                failed.extend(chunk)
                continue
            for symbol in chunk:
                bars = to_bars(payload.get(symbol), symbol)
                if bars:
                    result[symbol] = bars[-max_bars:] if max_bars is not None else bars
                else:
                    failed.append(symbol)

        notes = [f"以下标的分钟线拉取失败：{'、'.join(failed[:8])}"] if failed else []
        return result, notes

    def health_check(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            bars, _ = self.get_minute_bars(["600519.SH"], period="1", max_bars=5)
            ok, error = bool(bars), None
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"
        return {
            "source": self.name,
            "mode": "paid",
            "reachable": ok,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "capabilities": {
                "supports_minute": True,
                "supports_tick": False,
                "supports_orderbook": False,
                "raw_prices": self.adjust == ADJUST_RAW,
            },
            "periods": list(SUPPORTED_PERIODS),
            "max_bars_per_request": MAX_BARS_PER_REQUEST,
            "error": error,
        }


def to_bars(compact: Any, symbol: str) -> list[dict]:
    """TickFlow 的**列式**响应 → 仓内通用的行式 bar。

    注意返回的是 `{'timestamp': [...], 'open': [...], ...}` 这种按字段分列的结构，
    和日线接口的行式不一样，直接当行处理会得到一条巨大的假记录。
    """
    if not isinstance(compact, dict):
        return []
    stamps = compact.get("timestamp")
    closes = compact.get("close")
    if not isinstance(stamps, list) or not isinstance(closes, list):
        return []

    opens = compact.get("open") if isinstance(compact.get("open"), list) else []
    highs = compact.get("high") if isinstance(compact.get("high"), list) else []
    lows = compact.get("low") if isinstance(compact.get("low"), list) else []
    volumes = compact.get("volume") if isinstance(compact.get("volume"), list) else []
    amounts = compact.get("amount") if isinstance(compact.get("amount"), list) else []

    bars: list[dict] = []
    for index, stamp in enumerate(stamps):
        close = _at(closes, index)
        when = _to_dt(stamp)
        if close is None or when is None:
            continue
        open_price = _at(opens, index)
        volume_lots = _at(volumes, index)
        bars.append(
            {
                "symbol": symbol,
                "timestamp": when,
                "open": _px(close if open_price is None else open_price),
                "high": _px(_at(highs, index)),
                "low": _px(_at(lows, index)),
                "close": _px(close),
                # 手 → 股。不换算 `_vwap` 会把 amount/volume 算大 100 倍。
                "volume": None if volume_lots is None else volume_lots * LOTS_TO_SHARES,
                "amount": _at(amounts, index),
            }
        )
    return bars


def _px(value: float | None) -> float | None:
    """价格保留四位小数，消除独立浮点计算造成的 OHLC 尾差倒挂。"""
    return None if value is None else round(value, 4)


def _at(values: list, index: int) -> float | None:
    """按列下标读取有限数值；缺失、NaN 和 Infinity 返回 None。"""
    return finite(values[index]) if index < len(values) else None


def _to_dt(value: Any) -> datetime | None:
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return None
    # 交易所本地时间，与新浪那路保持一致的 naive datetime。
    return beijing_from_epoch(stamp / 1000.0)


# 选路


def build_intraday_adapter(data_adapter: Any = None) -> Any | None:
    """按 supports_intraday 选择 TickFlow 或新浪；两路均不可用时返回 None。"""
    client = getattr(data_adapter, "client", None)
    if client is not None and getattr(data_adapter, "supports_intraday", False):
        return TickFlowIntradayAdapter(client)
    try:
        from ashare_decision_support.adapters.sina_intraday import SinaIntradayAdapter

        return SinaIntradayAdapter()
    except Exception:
        # akshare 没装。分钟线缺了链路仍然能跑（只出日线结构判断），所以不抛。
        return None
