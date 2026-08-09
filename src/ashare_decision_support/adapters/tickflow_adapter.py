from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from typing import Any

from ashare_decision_support.adapters.base import MarketDataAdapter
from ashare_decision_support.adapters.ratelimit import wrap_client
from ashare_decision_support.models.schemas import AuctionFeatures, OpenFeatures
from ashare_decision_support.numeric import finite, first_finite
from ashare_decision_support.schedule.clock import minutes_since_open

# Exchange timezone. Epoch-millisecond windows must be anchored to it explicitly,
# otherwise a host running in UTC shifts the request window by 8 hours and
# silently drops the first or last trading day of the range.
CN_TZ = timezone(timedelta(hours=8))


class TickFlowAdapterError(RuntimeError):
    """Raised when the TickFlow adapter cannot complete a requested action."""


# (low, high, multiplier) bands used to infer the volume unit from
# amount / (volume * price). Anything outside these bands is treated as
# "unknown unit" instead of being silently passed through.
VOLUME_UNIT_BANDS: tuple[tuple[float, float, float], ...] = (
    (0.8, 1.2, 1.0),  # already in shares
    (8.0, 12.0, 10.0),  # lots of 10
    (80.0, 120.0, 100.0),  # A-share lots of 100
)

VOLUME_UNIT_MULTIPLIERS: dict[str, float] = {"share": 1.0, "lot": 100.0, "lot10": 10.0}

# Calendar-day lookback used by the degraded daily proxy. It must comfortably
# cover the 21-bar baseline needed by the volume-ratio helpers, including
# around long holidays.
DAILY_PROXY_LOOKBACK_DAYS = 150
DAILY_PROXY_MAX_ROWS = 60

# Symbols per batch kline request. The SDK parallelises inside a batch, so the
# ceiling here is request size rather than throughput.
BATCH_CHUNK_SIZE = 100

# 硬上限。调大批次**又慢又静默丢标的**，实测表见
# `get_historical_bars_batch` 的 docstring。夹住而不是报错，是因为调用方传大值
# 通常是想「快一点」，把它压回安全值就够了——为一个性能参数中断盘前链路不值。
BATCH_MAX_CHUNK = 100


class TickFlowAdapter(MarketDataAdapter):
    """Analysis-only adapter backed by the TickFlow Python SDK."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        tickflow_module: Any | None = None,
        auto_connect: bool = True,
        adjust: str = "forward",
        volume_unit: str = "auto",
        rate_limit: int | None = -1,
    ) -> None:
        if volume_unit not in VOLUME_UNIT_MULTIPLIERS and volume_unit != "auto":
            raise TickFlowAdapterError(
                f"unsupported volume_unit {volume_unit!r}; expected 'auto' or one of {sorted(VOLUME_UNIT_MULTIPLIERS)}"
            )
        self.adjust = adjust
        self.volume_unit = volume_unit
        self.api_key = api_key or os.getenv("TICKFLOW_API_KEY")
        self.base_url = base_url or os.getenv("TICKFLOW_BASE_URL") or self._default_base_url()
        self.mode = "full" if self.api_key else "free"
        self.supports_realtime = self.mode == "full"
        self.supports_intraday = self.mode == "full"
        self.supports_depth = self.mode == "full"
        self.supports_daily = True
        self.connected = False

        if tickflow_module is None:
            try:
                import tickflow as tickflow_module  # type: ignore
            except ImportError as exc:
                raise TickFlowAdapterError(
                    "tickflow is not available. Install it locally with `pip install tickflow[all]` "
                    "or `pip install -e .[tickflow]`."
                ) from exc

        tickflow_factory = getattr(tickflow_module, "TickFlow", None)
        if not callable(tickflow_factory):
            raise TickFlowAdapterError("tickflow module does not expose a callable TickFlow client")

        self.tickflow = tickflow_module
        if self.api_key:
            client = tickflow_factory(api_key=self.api_key)
        else:
            free_factory = getattr(tickflow_factory, "free", None)
            client = free_factory() if callable(free_factory) else tickflow_factory()

        # 免费档 60 次/分钟的闸门包在 client 上，不是包在本类的方法上：
        # `sectors/taxonomy.py` 拿的是 `adapter.client` 直接调 `universes.*`，
        # 包方法覆盖不到那条路径。见 `adapters/ratelimit.py`。
        self.client = wrap_client(client, api_key=self.api_key, limit=rate_limit)

        if auto_connect:
            self._connect()

    def get_security_master(self, symbols: list[str]) -> dict[str, dict]:
        normalized_map = self._normalized_symbol_map(symbols)
        rows = self._instruments_rows(list(normalized_map.values()))
        result: dict[str, dict] = {}
        for raw_symbol, normalized_symbol in normalized_map.items():
            result[raw_symbol] = rows.get(normalized_symbol, {"symbol": normalized_symbol})
        return result

    def get_historical_bars(self, symbols: list[str], start: date, end: date) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        for raw_symbol, normalized_symbol in self._normalized_symbol_map(symbols).items():
            payload = self._daily_payload(normalized_symbol, start, end)
            result[raw_symbol] = self._compact_kline_rows(payload, normalized_symbol)
        return result

    def get_historical_bars_batch(
        self,
        symbols: list[str],
        start: date,
        end: date,
        chunk_size: int = BATCH_CHUNK_SIZE,
    ) -> dict[str, list[dict]]:
        """Fetch many symbols per request.

        Sector work needs thousands of series at once, which the one-symbol-per-
        call path cannot deliver in a pre-open window. Falls back to that path
        when the SDK has no batch endpoint, so callers never have to branch.

        **不要调大 `chunk_size`。** 直觉上「一次要更多只」应该更快，实测正相反——
        2026-08-02 付费档全 A 5534 只、120 天日线：

        | chunk | 有数据的标的 | 耗时 |
        |---|---|---|
        | **100（默认）** | **5534 / 5534** | **27.8s** |
        | 500 | 4434 ← 静默少 1100 只 | 53.6s |
        | 1000 | 4034 ← 静默少 1500 只 | 51.2s |
        | 2000 | **0** | 36.2s |

        大批次**又慢又丢数据，而且不报错**——丢掉的标的返回空列表，和「这只票
        确实没有日线」无法区分。小样本（2000 只）测出来的结论是反的，别用小样本
        复核这个参数。`BATCH_MAX_CHUNK` 会把传进来的值夹住，就是为了挡住这条路。
        """
        normalized_map = self._normalized_symbol_map(symbols)
        klines = getattr(self.client, "klines", None)
        batch_fn = getattr(klines, "batch", None) if klines is not None else None
        if not callable(batch_fn):
            return self.get_historical_bars(symbols, start, end)

        start_time = int(datetime.combine(start, time.min, tzinfo=CN_TZ).timestamp() * 1000)
        end_time = int(datetime.combine(end + timedelta(days=1), time.min, tzinfo=CN_TZ).timestamp() * 1000) - 1
        count = min(max((end - start).days * 2 + 16, 64), 10000)
        step = max(1, min(int(chunk_size), BATCH_MAX_CHUNK))

        by_normalized: dict[str, list[dict]] = {}
        unique = list(dict.fromkeys(normalized_map.values()))
        for index in range(0, len(unique), step):
            chunk = unique[index : index + step]
            payload = self._try_call(
                batch_fn,
                ((chunk,), {"period": "1d", "start_time": start_time, "end_time": end_time, "count": count, "adjust": self.adjust}),
                ((chunk,), {"period": "1d", "start_time": start_time, "end_time": end_time, "count": count}),
                ((chunk,), {"period": "1d", "count": count}),
                ((), {"symbols": chunk, "period": "1d", "count": count}),
            )
            if not isinstance(payload, dict):
                continue
            for symbol, rows in payload.items():
                by_normalized[str(symbol)] = self._compact_kline_rows(rows, str(symbol))

        return {
            raw_symbol: by_normalized.get(normalized, [])
            for raw_symbol, normalized in normalized_map.items()
        }

    def get_trading_calendar(self) -> list[date]:
        return []

    def get_auction_features(self, symbols: list[str]) -> dict[str, AuctionFeatures]:
        features: dict[str, AuctionFeatures] = {}
        for raw_symbol, normalized_symbol in self._normalized_symbol_map(symbols).items():
            source = self._quote_or_daily(normalized_symbol) if self.supports_realtime else self._daily_proxy_rows(normalized_symbol)
            depth = self._depth_payload(normalized_symbol) if self.supports_depth else None
            source_rows = self._rows_only(source)
            quote = source_rows[-1] if source_rows else self._first_row(source)
            if quote is None:
                raise TickFlowAdapterError(f"no TickFlow payload found for symbol {raw_symbol} (normalized: {normalized_symbol})")

            bid_total, ask_total = self._depth_totals(depth)
            last_price = first_finite(quote, "last_price", "lastPrice", "price", "close")
            open_price = first_finite(quote, "open", "open_price", "openPrice")
            prev_close = first_finite(quote, "prev_close", "prevClose", "pre_close", "preClose")
            if prev_close is None and len(source_rows) >= 2:
                prev_close = first_finite(source_rows[-2], "close", "last_price", "lastPrice", "price")
            volume = first_finite(quote, "volume", "vol", "matched_qty")
            amount = first_finite(quote, "amount", "turnover")
            source_ts = self._source_datetime(quote)

            if last_price is None:
                last_price = open_price or prev_close or 0.01
            if volume is None:
                volume = 0.0
            if amount is None:
                amount = volume * last_price
            adjusted_volume = self._normalize_volume(volume, amount, last_price)

            open_gap_pct = 0.0
            if open_price is not None and prev_close not in (None, 0):
                open_gap_pct = (open_price / prev_close - 1.0) * 100.0
            elif last_price is not None and prev_close not in (None, 0):
                open_gap_pct = (last_price / prev_close - 1.0) * 100.0

            matched_qty = int(adjusted_volume or 0)
            unmatched_qty = abs(bid_total - ask_total) if bid_total or ask_total else 0
            unmatched_side = "N"
            if bid_total > ask_total:
                unmatched_side = "B"
            elif ask_total > bid_total:
                unmatched_side = "S"

            seal_ratio = None
            if matched_qty > 0 and bid_total > 0:
                seal_ratio = bid_total / matched_qty

            # Without a depth/auction feed there is no such thing as a matched or
            # unmatched auction quantity. Emitting whole-day turnover in those
            # fields would let daily numbers clear thresholds that were designed
            # for the 09:25 auction, so publish nulls instead of plausible lies.
            auction_capable = self.supports_realtime and self.supports_depth
            if not auction_capable:
                matched_qty = 0
                matched_amount = None
                unmatched_qty = 0
                unmatched_side = "N"
                seal_ratio = None
            else:
                matched_amount = amount

            features[raw_symbol] = AuctionFeatures(
                symbol=raw_symbol,
                matched_qty=matched_qty,
                matched_amount=matched_amount,
                unmatched_qty=unmatched_qty,
                unmatched_side=unmatched_side,
                indicative_match_price=last_price,
                open_gap_pct=open_gap_pct,
                cancel_pre920_ratio=None,
                seal_ratio=seal_ratio,
                tail_flow_distortion=False,
                data_stale=not auction_capable,
                source_ts=source_ts,
            )
        return features

    def get_open_features(self, symbols: list[str]) -> dict[str, OpenFeatures]:
        features: dict[str, OpenFeatures] = {}
        for raw_symbol, normalized_symbol in self._normalized_symbol_map(symbols).items():
            if self.supports_intraday:
                intraday_rows = self._intraday_rows(normalized_symbol)
                quote_rows = self._quote_rows(normalized_symbol)
                rows = intraday_rows or quote_rows
                data_stale = not bool(intraday_rows)
            else:
                rows = self._daily_proxy_rows(normalized_symbol)
                quote_rows = rows
                data_stale = True

            row = rows[-1] if rows else None
            if row is None:
                raise TickFlowAdapterError(f"no TickFlow payload found for symbol {raw_symbol} (normalized: {normalized_symbol})")

            reference_row = quote_rows[-1] if quote_rows else row

            current_price = first_finite(row, "close", "last_price", "lastPrice", "price")
            open_price = first_finite(reference_row, "open", "open_price", "openPrice")
            prev_close = first_finite(reference_row, "prev_close", "prevClose", "pre_close", "preClose")
            if prev_close is None and len(rows) >= 2:
                prev_close = first_finite(rows[-2], "close", "last_price", "lastPrice", "price")
            source_ts = self._source_datetime(row)

            if current_price is None:
                current_price = open_price or prev_close or 0.01

            vwap = first_finite(row, "vwap", "vWap", "weighted_avg_price")
            amount = first_finite(row, "amount", "turnover")
            volume = first_finite(row, "volume", "vol")
            volume_multiplier, volume_unit_known = self._volume_multiplier(volume, amount, current_price)
            adjusted_volume = volume if volume_multiplier is None else volume * volume_multiplier
            # Deriving VWAP from amount/volume while the volume unit is unknown is
            # what produces price_vs_vwap_pct = -99%, which makes every name look
            # permanently below VWAP and turns the open-follow engine into a
            # blanket BLOCK. Fall back to the last price instead.
            if vwap is None and volume_unit_known and amount is not None and adjusted_volume not in (None, 0):
                vwap = amount / adjusted_volume
            if vwap is None:
                vwap = current_price

            minute_volume_ratio = self._volume_ratio(self._rows_only(rows))
            if not self.supports_intraday:
                minute_volume_ratio = first_finite(row, "volume_ratio", "volumeRatio") or self._daily_volume_ratio(rows)

            open_gap_pct = 0.0
            if open_price is not None and prev_close not in (None, 0):
                open_gap_pct = (open_price / prev_close - 1.0) * 100.0
            elif prev_close not in (None, 0):
                open_gap_pct = (current_price / prev_close - 1.0) * 100.0

            features[raw_symbol] = OpenFeatures(
                symbol=raw_symbol,
                minutes_since_open=minutes_since_open(source_ts),
                price_vs_open_pct=((current_price / open_price - 1.0) * 100.0) if open_price not in (None, 0) else 0.0,
                price_vs_vwap_pct=((current_price / vwap - 1.0) * 100.0) if vwap not in (None, 0) else 0.0,
                volume_ratio=minute_volume_ratio,
                open_gap_pct=open_gap_pct,
                data_stale=data_stale,
                source_ts=source_ts,
            )
        return features

    def health_check(self) -> dict[str, Any]:
        return {
            "adapter": "TickFlowAdapter",
            "base_url": self.base_url,
            "mode": self.mode,
            "connected": self.connected,
            "capabilities": {
                "supports_realtime_quotes": self.supports_realtime,
                "supports_intraday": self.supports_intraday,
                "supports_depth": self.supports_depth,
                "supports_daily": self.supports_daily,
                "supports_security_master": True,
                "supports_trading_calendar": False,
            },
        }

    def dump_snapshot(self, symbols: list[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "generated_at": datetime.now().isoformat(),
            "symbols": symbols,
            "base_url": self.base_url,
            "mode": self.mode,
            "quotes": {},
            "depth": {},
            "intraday": {},
            "daily": {},
            "instruments": {},
        }
        for symbol in symbols:
            normalized_symbol = self._normalize_symbol(symbol)
            try:
                payload["quotes"][symbol] = self._first_row(self._quote_rows(normalized_symbol))
            except Exception as exc:
                payload["quotes"][symbol] = {"error": str(exc)}
            try:
                payload["depth"][symbol] = self._depth_summary(self._depth_payload(normalized_symbol)) if self.supports_depth else {"stale": True}
            except Exception as exc:
                payload["depth"][symbol] = {"error": str(exc)}
            try:
                payload["intraday"][symbol] = self._compact_kline_rows(self._intraday_payload(normalized_symbol), normalized_symbol)[-20:]
            except Exception as exc:
                payload["intraday"][symbol] = {"error": str(exc)}
            try:
                payload["daily"][symbol] = self._compact_kline_rows(self._daily_payload(normalized_symbol, date.today(), date.today()), normalized_symbol)[-10:]
            except Exception as exc:
                payload["daily"][symbol] = {"error": str(exc)}
            try:
                payload["instruments"][symbol] = self.get_security_master([symbol]).get(symbol, {"symbol": symbol})
            except Exception as exc:
                payload["instruments"][symbol] = {"error": str(exc)}
        return payload

    def _connect(self) -> None:
        connect_fn = getattr(self.client, "connect", None)
        if callable(connect_fn):
            result = connect_fn()
            if result is False:
                raise TickFlowAdapterError("TickFlow connect() returned False")
        self.connected = True

    def _default_base_url(self) -> str:
        return "https://api.tickflow.org" if self.api_key else "https://free-api.tickflow.org"

    def _quote_rows(self, symbol: str) -> list[dict]:
        if not self.supports_realtime:
            return self._daily_rows(symbol)
        quotes = getattr(self.client, "quotes", None)
        if quotes is None:
            return []
        get_fn = getattr(quotes, "get", None) or getattr(quotes, "batch", None)
        if not callable(get_fn):
            return []
        payload = self._try_call(
            get_fn,
            ((symbol,), {}),
            ((), {"symbol": symbol}),
            ((), {"symbols": [symbol]}),
        )
        return self._rows(payload)

    def _depth_payload(self, symbol: str) -> Any:
        depth = getattr(self.client, "depth", None)
        if depth is None:
            return None
        get_fn = getattr(depth, "get", None)
        if not callable(get_fn):
            return None
        return self._try_call(
            get_fn,
            ((symbol,), {}),
            ((), {"symbol": symbol}),
            ((), {"symbols": [symbol]}),
        )

    def _intraday_rows(self, symbol: str) -> list[dict]:
        if not self.supports_intraday:
            return []
        return self._compact_kline_rows(self._intraday_payload(symbol), symbol)

    def _intraday_payload(self, symbol: str) -> Any:
        klines = getattr(self.client, "klines", None)
        if klines is None:
            return None
        intraday_fn = getattr(klines, "intraday", None) or getattr(klines, "get", None)
        if not callable(intraday_fn):
            return None
        return self._try_call(
            intraday_fn,
            ((symbol,), {"period": "1m", "count": 240}),
            ((symbol,), {"period": "1m", "count": 240, "as_dataframe": False}),
            ((symbol,), {"period": "1m"}),
            ((), {"symbol": symbol, "period": "1m", "count": 240}),
        )

    def _daily_rows(self, symbol: str) -> list[dict]:
        return self._compact_kline_rows(self._daily_payload(symbol, date.today(), date.today()), symbol)

    def _daily_proxy_rows(self, symbol: str) -> list[dict]:
        # The first day of the current month is always more recent than
        # "40 days ago", so the previous max(...) collapsed the window to
        # month-to-date and returned 0-2 bars at the start of every month.
        end = date.today()
        start = end - timedelta(days=DAILY_PROXY_LOOKBACK_DAYS)
        rows = self._compact_kline_rows(self._daily_payload(symbol, start, end), symbol)
        return rows[-DAILY_PROXY_MAX_ROWS:]

    def _daily_payload(self, symbol: str, start: date, end: date) -> Any:
        klines = getattr(self.client, "klines", None)
        if klines is None:
            return None
        get_fn = getattr(klines, "get", None) or getattr(klines, "batch", None)
        if not callable(get_fn):
            return None
        start_time = int(datetime.combine(start, time.min, tzinfo=CN_TZ).timestamp() * 1000)
        end_time = int(datetime.combine(end + timedelta(days=1), time.min, tzinfo=CN_TZ).timestamp() * 1000) - 1
        count = min(max((end - start).days * 2 + 16, 64), 10000)
        # `adjust` is passed explicitly: the SDK default is forward adjustment,
        # which rescales history and makes limit-up/limit-down and tick-size
        # checks meaningless. See docs/DATA_SOURCES.md.
        return self._try_call(
            get_fn,
            ((symbol,), {"period": "1d", "start_time": start_time, "end_time": end_time, "count": count, "adjust": self.adjust}),
            ((symbol,), {"period": "1d", "start_time": start_time, "end_time": end_time, "count": count}),
            ((symbol,), {"period": "1d", "start_date": start.isoformat(), "end_date": end.isoformat(), "count": count}),
            ((symbol,), {"period": "1d", "count": count}),
            ((), {"symbol": symbol, "period": "1d", "start_time": start_time, "end_time": end_time, "count": count}),
        )

    def _quote_or_daily(self, symbol: str) -> Any:
        quote_rows = self._quote_rows(symbol)
        if quote_rows:
            return quote_rows
        return self._daily_rows(symbol)

    def _instruments_rows(self, symbols: list[str]) -> dict[str, dict]:
        instruments = getattr(self.client, "instruments", None)
        if instruments is None:
            return {}
        batch_fn = getattr(instruments, "batch", None) or getattr(instruments, "get", None)
        if not callable(batch_fn):
            return {}
        payload = self._try_call(
            batch_fn,
            ((symbols,), {}),
            ((), {"symbols": symbols}),
        )
        rows = self._rows(payload)
        result: dict[str, dict] = {}
        for row in rows:
            symbol = row.get("symbol") or row.get("Symbol") or row.get("code") or row.get("代码")
            if symbol is None:
                continue
            result[str(symbol)] = row
        return result

    def _normalized_symbol_map(self, symbols: list[str]) -> dict[str, str]:
        return {symbol: self._normalize_symbol(symbol) for symbol in symbols}

    def _normalize_symbol(self, symbol: str) -> str:
        text = symbol.strip().upper()
        if "." in text:
            return text
        if not text.isdigit():
            return text
        if len(text) != 6:
            return text
        if text.startswith(("600", "601", "603", "605", "688", "689", "730", "731", "732", "733")):
            return f"{text}.SH"
        if text.startswith(("000", "001", "002", "003", "300", "301")):
            return f"{text}.SZ"
        if text.startswith(("430", "440", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873", "920")):
            return f"{text}.BJ"
        return f"{text}.SH"

    def _depth_totals(self, payload: Any) -> tuple[int, int]:
        rows = self._rows(payload)
        if not rows:
            return 0, 0
        first = rows[0]
        bid_total = 0
        ask_total = 0
        for index in range(1, 11):
            bid_total += int(first_finite(first, f"bidVol{index}", f"BidVol{index}", f"bid_volume_{index}") or 0)
            ask_total += int(first_finite(first, f"askVol{index}", f"AskVol{index}", f"ask_volume_{index}") or 0)
        return bid_total, ask_total

    def _depth_summary(self, payload: Any) -> dict[str, Any]:
        bid_total, ask_total = self._depth_totals(payload)
        return {
            "bid_total": bid_total,
            "ask_total": ask_total,
            "imbalance": bid_total - ask_total,
            "imbalance_ratio": round(bid_total / ask_total, 4) if ask_total > 0 else None,
        }

    def _first_row(self, payload: Any) -> dict | None:
        rows = self._rows(payload)
        return rows[0] if rows else None

    def _rows_only(self, payload: Any) -> list[dict]:
        return self._rows(payload)

    def _rows(self, payload: Any) -> list[dict]:
        if payload is None:
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            if "data" in payload:
                return self._rows(payload["data"])
            if all(isinstance(value, list) for value in payload.values()):
                return self._compact_kline_rows(payload, payload.get("symbol", ""))
            return [payload]
        to_dict = getattr(payload, "to_dict", None)
        if callable(to_dict):
            try:
                rows = to_dict(orient="records")
                if isinstance(rows, list):
                    return [item for item in rows if isinstance(item, dict)]
            except TypeError:
                try:
                    rows = to_dict()
                    if isinstance(rows, list):
                        return [item for item in rows if isinstance(item, dict)]
                except Exception:  # noqa: S110 — 逐个试签名，试不通就换下一种
                    pass
        return []

    def _compact_kline_rows(self, payload: Any, symbol: str) -> list[dict]:
        if isinstance(payload, dict) and "data" in payload:
            return self._compact_kline_rows(payload["data"], symbol)

        if not isinstance(payload, dict):
            rows = self._rows(payload)
            compact_rows: list[dict] = []
            for row in rows:
                timestamp = self._source_datetime(row)
                compact_rows.append(
                    {
                        "symbol": symbol or str(row.get("symbol") or row.get("code") or ""),
                        "timestamp": timestamp,
                        "open": first_finite(row, "open", "Open", "开盘"),
                        "high": first_finite(row, "high", "High", "最高"),
                        "low": first_finite(row, "low", "Low", "最低"),
                        "close": first_finite(row, "close", "Close", "收盘"),
                        "volume": first_finite(row, "volume", "Volume", "成交量"),
                        "amount": first_finite(row, "amount", "Amount", "成交额"),
                        "prev_close": first_finite(row, "prev_close", "prevClose", "pre_close", "preClose"),
                    }
                )
            return [row for row in compact_rows if row.get("close") is not None]

        array_fields = ("timestamp", "time", "open", "high", "low", "close", "volume", "amount", "prev_close")
        if any(isinstance(payload.get(field), list) for field in array_fields):
            timestamps = payload.get("timestamp", []) or payload.get("time", []) or []
            opens = payload.get("open", []) or []
            highs = payload.get("high", []) or []
            lows = payload.get("low", []) or []
            closes = payload.get("close", []) or []
            volumes = payload.get("volume", []) or []
            amounts = payload.get("amount", []) or []
            prev_closes = payload.get("prev_close", []) or []

            compact: list[dict] = []
            for idx, timestamp in enumerate(timestamps):
                compact.append(
                    {
                        "symbol": symbol,
                        "timestamp": self._parse_datetime(timestamp),
                        # 这一支此前是**逐格照抄**，唯独它不过 `finite`——同一个函数
                        # 的另外两支都过。而列式这一支正是 TickFlow 返回数组时走的那
                        # 条，也就是主行情源的日线主路径。
                        #
                        # 漏的那一项是 NaN：下面只按 `close is not None` 过滤，NaN 不
                        # 是 None，照样进 K 线序列。之后它不会报错——`dipbuy` 的
                        # `float(high)` 收得下 NaN，真实波幅变成 NaN，ATR 跟着变成
                        # NaN，而 NaN 的比较永远是 False，「跌破止损」就静默变成
                        # 「没跌破」。这正是 `numeric.py` 开头那段说的那种坏法。
                        "open": finite(_at(opens, idx)),
                        "high": finite(_at(highs, idx)),
                        "low": finite(_at(lows, idx)),
                        "close": finite(_at(closes, idx)),
                        "volume": finite(_at(volumes, idx)),
                        "amount": finite(_at(amounts, idx)),
                        "prev_close": finite(_at(prev_closes, idx)),
                    }
                )
            return [row for row in compact if row.get("close") is not None]

        rows = self._rows(payload)
        compact_rows: list[dict] = []
        for row in rows:
            timestamp = self._source_datetime(row)
            compact_rows.append(
                {
                    "symbol": symbol or str(row.get("symbol") or row.get("code") or ""),
                    "timestamp": timestamp,
                    "open": first_finite(row, "open", "Open", "开盘"),
                    "high": first_finite(row, "high", "High", "最高"),
                    "low": first_finite(row, "low", "Low", "最低"),
                    "close": first_finite(row, "close", "Close", "收盘"),
                    "volume": first_finite(row, "volume", "Volume", "成交量"),
                    "amount": first_finite(row, "amount", "Amount", "成交额"),
                    "prev_close": first_finite(row, "prev_close", "prevClose", "pre_close", "preClose"),
                }
            )
        return [row for row in compact_rows if row.get("close") is not None]

    def _source_datetime(self, payload: dict[str, Any]) -> datetime:
        for key in ("timestamp", "time", "datetime", "date", "日期", "时间"):
            value = payload.get(key)
            if value not in (None, ""):
                return self._parse_datetime(value)
        return datetime.now()

    def _parse_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts)
        text = str(value)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return datetime.now()

    def _volume_ratio(self, rows: list[dict]) -> float:
        if not rows:
            return 1.0
        volumes = [first_finite(row, "volume") for row in rows[:-1]]
        baseline = [value for value in volumes if value not in (None, 0)]
        if not baseline:
            return 1.0
        base = median(baseline[-20:])
        if base <= 0:
            return 1.0
        latest = first_finite(rows[-1], "volume") or 0.0
        return latest / base

    def _daily_volume_ratio(self, rows: list[dict]) -> float:
        if len(rows) < 2:
            return 1.0
        latest = first_finite(rows[-1], "volume") or 0.0
        baseline = [first_finite(row, "volume") or 0.0 for row in rows[-21:-1]]
        baseline = [value for value in baseline if value > 0]
        if not baseline:
            return 1.0
        return latest / median(baseline)

    def _normalize_volume(self, volume: float | None, amount: float | None, price: float | None) -> float | None:
        multiplier, _ = self._volume_multiplier(volume, amount, price)
        if multiplier is None:
            return volume
        return volume * multiplier

    def _volume_multiplier(
        self,
        volume: float | None,
        amount: float | None,
        price: float | None,
    ) -> tuple[float | None, bool]:
        """Resolve the share multiplier for a raw volume field.

        Returns ``(multiplier, confident)``. ``confident`` is False when the unit
        could not be established, so callers must not derive VWAP or any other
        amount/volume ratio from it.
        """
        if volume in (None, 0):
            return None, True
        if self.volume_unit != "auto":
            return VOLUME_UNIT_MULTIPLIERS[self.volume_unit], True
        if amount in (None, 0) or price in (None, 0):
            return None, False
        implied_lot = amount / (volume * price)
        for low, high, multiplier in VOLUME_UNIT_BANDS:
            if low <= implied_lot <= high:
                return multiplier, True
        return None, False

    def _try_call(self, func: Any, *attempts: tuple[tuple, dict]) -> Any:
        """Call ``func`` trying successive signatures.

        Only ``TypeError`` means "wrong signature, try the next shape". Every
        other exception is a real upstream failure (auth, rate limit, network,
        permission) and is surfaced with its original type in the message
        instead of being retried into a misleading generic error.
        """
        last_error: TypeError | None = None
        for args, kwargs in attempts:
            try:
                return func(*args, **kwargs)
            except TypeError as exc:
                last_error = exc
                continue
            except Exception as exc:
                raise TickFlowAdapterError(f"{type(exc).__name__}: {exc}") from exc
        if last_error is not None:
            raise TickFlowAdapterError(
                f"no supported call signature for {getattr(func, '__name__', func)!r}: {last_error}"
            ) from last_error
        raise TickFlowAdapterError("TickFlow call failed without an exception")


def _at(values: Any, index: int) -> Any:
    """列式载荷里按下标取值，越界返回 None。

    各列不保证等长——`timestamp` 有 240 个而 `amount` 只有 238 个是可能的，
    而列式这一支是按 `timestamp` 的长度在循环。越界直接抛会让整只标的的日线
    消失，返回 None 只让那一格缺失，由 `finite` 统一收成「没有这个读数」。
    """
    return values[index] if index < len(values) else None
