from __future__ import annotations

from datetime import date, datetime, time
import json
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.base import MarketDataAdapter
from ashare_decision_support.adapters.mapping import MappingProfile, PayloadNormalizer
from ashare_decision_support.models.schemas import AuctionFeatures, OpenFeatures


class QmtAdapterError(RuntimeError):
    """Raised when the xtdata adapter cannot complete a requested action."""


class QmtXtDataAdapter(MarketDataAdapter):
    """Best-effort xtdata adapter for local QMT and MiniQMT environments."""

    def __init__(
        self,
        profile: MappingProfile | None = None,
        xtdata_module: Any | None = None,
        auto_connect: bool = True,
        calendar_market: str = "SH",
    ) -> None:
        if xtdata_module is None:
            try:
                from xtquant import xtdata as xtdata_module  # type: ignore
            except ImportError as exc:
                raise QmtAdapterError(
                    "xtquant.xtdata is not available. Install and run this adapter on the local "
                    "machine that has MiniQMT/QMT and the XtQuant Python package."
                ) from exc

        self.xtdata = xtdata_module
        self.normalizer = PayloadNormalizer(profile)
        self.calendar_market = calendar_market
        self.subscription_ids: list[int] = []

        if auto_connect:
            self.connect()

    def connect(self) -> None:
        connect_fn = getattr(self.xtdata, "connect", None)
        if callable(connect_fn):
            result = connect_fn()
            if result is False:
                raise QmtAdapterError("xtdata.connect() returned False")

    def subscribe_quotes(self, symbols: list[str]) -> None:
        subscribe_fn = getattr(self.xtdata, "subscribe_quote", None)
        if not callable(subscribe_fn):
            raise QmtAdapterError("xtdata.subscribe_quote is not available")
        for symbol in symbols:
            sub_id = self._try_call(
                subscribe_fn,
                ((symbol,), {"period": "tick", "count": 0, "callback": None}),
                ((symbol,), {"period": "tick", "count": 0}),
                ((symbol, "tick", 0), {}),
            )
            if isinstance(sub_id, int):
                self.subscription_ids.append(sub_id)

    def subscribe_l2_snapshot(self, symbols: list[str]) -> None:
        subscribe_fn = getattr(self.xtdata, "subscribe_whole_quote", None)
        if callable(subscribe_fn):
            sub_id = self._try_call(
                subscribe_fn,
                ((symbols,), {"callback": None}),
                ((symbols,), {}),
            )
            if isinstance(sub_id, int):
                self.subscription_ids.append(sub_id)
            return
        self.subscribe_quotes(symbols)

    def subscribe_ticks(self, symbols: list[str]) -> None:
        self.subscribe_quotes(symbols)

    def subscribe_orders(self, symbols: list[str]) -> None:
        # Order-detail subscription support varies by local QMT environment.
        return None

    def get_security_master(self, symbols: list[str]) -> dict[str, dict]:
        detail_fn = getattr(self.xtdata, "get_instrument_detail", None)
        if not callable(detail_fn):
            return {symbol: {"symbol": symbol} for symbol in symbols}
        result: dict[str, dict] = {}
        for symbol in symbols:
            result[symbol] = self._to_plain(
                self._try_call(
                    detail_fn,
                    ((symbol,), {}),
                    ((), {"stock_code": symbol}),
                )
            )
        return result

    def get_historical_bars(self, symbols: list[str], start: date, end: date) -> dict[str, list[dict]]:
        market_data = self._get_market_data(
            field_list=["open", "high", "low", "close", "volume", "amount"],
            stock_list=symbols,
            period="1d",
            start_time=start.strftime("%Y%m%d"),
            end_time=end.strftime("%Y%m%d"),
            count=-1,
        )
        return {symbol: self._extract_bar_rows(market_data, symbol) for symbol in symbols}

    def get_trading_calendar(self) -> list[date]:
        calendar_fn = getattr(self.xtdata, "get_trading_calendar", None)
        if not callable(calendar_fn):
            raise QmtAdapterError("xtdata.get_trading_calendar is not available")
        raw = self._try_call(
            calendar_fn,
            ((self.calendar_market, "", ""), {}),
            ((self.calendar_market,), {}),
        )
        if raw is None:
            return []
        return [self._parse_calendar_value(item) for item in raw]

    def get_auction_features(self, symbols: list[str]) -> dict[str, AuctionFeatures]:
        full_tick = self.get_full_tick_raw(symbols)
        l2_quote = self.get_l2_quote_raw(symbols)
        result: dict[str, AuctionFeatures] = {}
        for symbol in symbols:
            raw_payload = self._build_auction_payload(
                symbol=symbol,
                full_tick=self._to_plain(full_tick.get(symbol)),
                l2_quote=self._to_plain(l2_quote.get(symbol)),
            )
            result[symbol] = self.normalizer.normalize_auction(raw_payload)
        return result

    def get_open_features(self, symbols: list[str]) -> dict[str, OpenFeatures]:
        full_tick = self.get_full_tick_raw(symbols)
        result: dict[str, OpenFeatures] = {}
        for symbol in symbols:
            raw_payload = self._build_open_payload(
                symbol=symbol,
                full_tick=self._to_plain(full_tick.get(symbol)),
            )
            result[symbol] = self.normalizer.normalize_open(raw_payload)
        return result

    def get_full_tick_raw(self, symbols: list[str]) -> dict[str, Any]:
        get_full_tick = getattr(self.xtdata, "get_full_tick", None)
        if not callable(get_full_tick):
            raise QmtAdapterError("xtdata.get_full_tick is not available")
        raw = self._try_call(get_full_tick, ((symbols,), {}), ((tuple(symbols),), {}))
        if not isinstance(raw, dict):
            raise QmtAdapterError("xtdata.get_full_tick returned an unsupported payload")
        return raw

    def get_l2_quote_raw(self, symbols: list[str]) -> dict[str, Any]:
        get_l2_quote = getattr(self.xtdata, "get_l2_quote", None)
        if not callable(get_l2_quote):
            return {}
        result: dict[str, Any] = {}
        for symbol in symbols:
            payload = self._call_optional(
                get_l2_quote,
                ((symbol,), {}),
                ((symbol, "", "", 1), {}),
                (([symbol],), {}),
            )
            if payload is not None:
                result[symbol] = payload
        return result

    def dump_snapshot(self, symbols: list[str]) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "generated_at": datetime.now().isoformat(),
            "symbols": symbols,
            "full_tick": {},
            "l2_quote": {},
            "instrument_detail": {},
        }
        try:
            snapshot["full_tick"] = {symbol: self._to_plain(payload) for symbol, payload in self.get_full_tick_raw(symbols).items()}
        except Exception as exc:
            snapshot["full_tick_error"] = str(exc)
        try:
            snapshot["l2_quote"] = {symbol: self._to_plain(payload) for symbol, payload in self.get_l2_quote_raw(symbols).items()}
        except Exception as exc:
            snapshot["l2_quote_error"] = str(exc)
        try:
            snapshot["instrument_detail"] = self.get_security_master(symbols)
        except Exception as exc:
            snapshot["instrument_detail_error"] = str(exc)
        return snapshot

    def dump_snapshot_to_file(self, symbols: list[str], output_path: str | Path) -> None:
        payload = self.dump_snapshot(symbols)
        Path(output_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def health_check(self) -> dict[str, Any]:
        capabilities = {
            "has_connect": callable(getattr(self.xtdata, "connect", None)),
            "has_get_full_tick": callable(getattr(self.xtdata, "get_full_tick", None)),
            "has_get_l2_quote": callable(getattr(self.xtdata, "get_l2_quote", None)),
            "has_subscribe_quote": callable(getattr(self.xtdata, "subscribe_quote", None)),
            "has_subscribe_whole_quote": callable(getattr(self.xtdata, "subscribe_whole_quote", None)),
            "has_get_market_data": callable(getattr(self.xtdata, "get_market_data", None)),
            "has_get_trading_calendar": callable(getattr(self.xtdata, "get_trading_calendar", None)),
        }
        return {
            "adapter": "QmtXtDataAdapter",
            "connected": True,
            "calendar_market": self.calendar_market,
            "capabilities": capabilities,
        }

    def _build_auction_payload(self, symbol: str, full_tick: dict[str, Any], l2_quote: dict[str, Any]) -> dict[str, Any]:
        merged = {**full_tick, **l2_quote}
        source_dt = self._extract_source_dt(merged)
        price = self._first_float(
            merged,
            "IndicativeMatchPrice",
            "indicativeMatchPrice",
            "lastPrice",
            "last_price",
            "BidPrice1",
            "bidPrice1",
            "price",
        )
        if price is None or price <= 0:
            raise QmtAdapterError(
                f"QMT auction payload for {symbol} is missing a usable price field. "
                "Run `qmt-dump` on your local machine and inspect the raw field names."
            )

        matched_qty = self._first_int(
            merged,
            "MatchedQty",
            "matchedQty",
            "matched_qty",
            "volume",
            "Volume",
            "totalVolumeTrade",
            "totalVolume",
        )
        if matched_qty is None:
            raise QmtAdapterError(
                f"QMT auction payload for {symbol} is missing a usable matched quantity field. "
                "Run `qmt-dump` and update the mapping profile if needed."
            )

        matched_amount = self._first_float(
            merged,
            "MatchedAmount",
            "matchedAmount",
            "matched_amount",
            "amount",
            "Amount",
            "turnover",
        )
        prev_close = self._first_float(merged, "PrevClose", "prevClose", "preClose", "pre_close", "lastClose")
        unmatched_qty = self._first_int(
            merged,
            "UnmatchedQty",
            "unmatchedQty",
            "unmatched_qty",
            "auctionUnmatchedQty",
            "AuctionUnmatchedQty",
        )
        data_stale = unmatched_qty is None
        if unmatched_qty is None:
            unmatched_qty = 0

        raw_payload = {
            "symbol": symbol,
            "MatchedQty": matched_qty,
            "MatchedAmount": matched_amount if matched_amount is not None else matched_qty * price,
            "UnmatchedQty": unmatched_qty,
            "UnmatchedSide": self._first_str(
                merged,
                "UnmatchedSide",
                "unmatchedSide",
                "unmatched_side",
                "auctionUnmatchedSide",
                "AuctionUnmatchedSide",
                default="N",
            ),
            "IndicativeMatchPrice": price,
            "sourceTs": source_dt.isoformat(),
            "dataStale": data_stale,
        }

        open_gap = self._first_float(merged, "OpenGapPct", "openGapPct", "open_gap_pct")
        if open_gap is not None:
            raw_payload["OpenGapPct"] = open_gap
        elif prev_close is not None and prev_close > 0:
            raw_payload["PrevClose"] = prev_close

        cancel_ratio = self._first_float(
            merged,
            "CancelPre920Ratio",
            "cancelPre920Ratio",
            "cancel_pre920_ratio",
            "R_cancel_pre920",
        )
        if cancel_ratio is not None:
            raw_payload["CancelPre920Ratio"] = cancel_ratio

        bid_vol_1 = self._first_float(merged, "bidVol1", "BidVol1", "bidVolume1", "BidVolume1")
        if bid_vol_1 is not None and matched_qty > 0:
            raw_payload["SealRatio"] = bid_vol_1 / matched_qty

        tail_flow = merged.get("tailFlowDistortion", merged.get("TailFlowDistortion"))
        if tail_flow is not None:
            raw_payload["tailFlowDistortion"] = bool(tail_flow)

        return raw_payload

    def _build_open_payload(self, symbol: str, full_tick: dict[str, Any]) -> dict[str, Any]:
        source_dt = self._extract_source_dt(full_tick)
        current_price = self._first_float(
            full_tick,
            "lastPrice",
            "LastPrice",
            "price",
            "Price",
            "close",
            "Close",
        )
        if current_price is None or current_price <= 0:
            raise QmtAdapterError(
                f"QMT open payload for {symbol} is missing a usable price field. "
                "Run `qmt-dump` and inspect the raw field names."
            )

        open_price = self._first_float(full_tick, "open", "Open", "openPrice", "OpenPrice")
        open_gap = self._first_float(full_tick, "openGapPct", "OpenGapPct", "open_gap_pct")
        prev_close = self._first_float(full_tick, "preClose", "PrevClose", "prevClose", "pre_close")
        if open_price is None:
            if open_gap is not None:
                open_price = current_price / (1.0 + open_gap / 100.0)
            elif prev_close is not None and prev_close > 0:
                open_price = prev_close
            else:
                open_price = current_price

        if open_gap is None:
            if prev_close is not None and prev_close > 0:
                open_gap = (open_price / prev_close - 1.0) * 100.0
            else:
                open_gap = 0.0

        volume = self._first_float(full_tick, "volume", "Volume", "totalVolumeTrade", "totalVolume")
        amount = self._first_float(full_tick, "amount", "Amount", "turnover")
        if amount is not None and volume is not None and volume > 0:
            vwap = amount / volume
        else:
            vwap = current_price

        minutes_since_open = self._minutes_since_open(source_dt)
        volume_ratio = 1.0
        if volume is not None and minutes_since_open > 0:
            volume_ratio = max(0.1, min(5.0, volume / max(minutes_since_open, 1)))

        price_vs_open_pct = (current_price / open_price - 1.0) * 100.0 if open_price > 0 else 0.0
        price_vs_vwap_pct = (current_price / vwap - 1.0) * 100.0 if vwap > 0 else 0.0

        return {
            "symbol": symbol,
            "minutesSinceOpen": minutes_since_open,
            "priceVsOpenPct": price_vs_open_pct,
            "priceVsVwapPct": price_vs_vwap_pct,
            "volumeRatio": volume_ratio,
            "openGapPct": open_gap,
            "sourceTs": source_dt.isoformat(),
            "dataStale": False,
        }

    def _get_market_data(self, **kwargs) -> Any:
        market_data_fn = getattr(self.xtdata, "get_market_data", None)
        if not callable(market_data_fn):
            raise QmtAdapterError("xtdata.get_market_data is not available")
        return self._try_call(
            market_data_fn,
            ((), kwargs),
        )

    def _extract_bar_rows(self, raw: Any, symbol: str) -> list[dict]:
        rows: list[dict] = []
        if not isinstance(raw, dict):
            return rows
        fields = ["open", "high", "low", "close", "volume", "amount"]
        series_map: dict[str, list[Any]] = {}
        max_len = 0
        for field in fields:
            field_payload = raw.get(field)
            values = self._extract_series_values(field_payload, symbol)
            series_map[field] = values
            max_len = max(max_len, len(values))
        for idx in range(max_len):
            row = {}
            for field, values in series_map.items():
                if idx < len(values):
                    row[field] = values[idx]
            rows.append(row)
        return rows

    def _extract_series_values(self, field_payload: Any, symbol: str) -> list[Any]:
        if field_payload is None:
            return []
        if hasattr(field_payload, "columns") and hasattr(field_payload, "tolist"):
            if symbol in getattr(field_payload, "columns", []):
                return [self._to_plain(item) for item in field_payload[symbol].tolist()]
            try:
                return [self._to_plain(item) for item in field_payload.iloc[:, 0].tolist()]
            except Exception:
                return []
        if isinstance(field_payload, dict):
            if symbol in field_payload:
                values = field_payload[symbol]
                if isinstance(values, list):
                    return [self._to_plain(item) for item in values]
                return [self._to_plain(values)]
            return []
        if isinstance(field_payload, list):
            return [self._to_plain(item) for item in field_payload]
        return [self._to_plain(field_payload)]

    def _extract_source_dt(self, payload: dict[str, Any]) -> datetime:
        raw = self._first_value(payload, "sourceTs", "SourceTs", "source_ts", "time", "Time", "timestamp")
        if raw is None:
            return datetime.now()
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, (int, float)):
            ts = float(raw)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts)
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                if raw.isdigit():
                    ts = float(raw)
                    if ts > 1e12:
                        ts /= 1000.0
                    return datetime.fromtimestamp(ts)
        return datetime.now()

    def _minutes_since_open(self, dt: datetime) -> int:
        morning_open = datetime.combine(dt.date(), time(9, 30))
        afternoon_open = datetime.combine(dt.date(), time(13, 0))
        if dt <= morning_open:
            return 0
        if dt < datetime.combine(dt.date(), time(11, 30)):
            return int((dt - morning_open).total_seconds() // 60)
        if dt < afternoon_open:
            return 120
        return 120 + int((dt - afternoon_open).total_seconds() // 60)

    def _parse_calendar_value(self, value: Any) -> date:
        if isinstance(value, date):
            return value
        text = str(value)
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return datetime.fromisoformat(text).date()

    def _first_value(self, payload: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in payload:
                return payload[key]
        return None

    def _first_float(self, payload: dict[str, Any], *keys: str) -> float | None:
        value = self._first_value(payload, *keys)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _first_int(self, payload: dict[str, Any], *keys: str) -> int | None:
        value = self._first_value(payload, *keys)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _first_str(self, payload: dict[str, Any], *keys: str, default: str = "") -> str:
        value = self._first_value(payload, *keys)
        if value is None:
            return default
        return str(value)

    def _call_optional(self, func: Any, *attempts: tuple[tuple[Any, ...], dict[str, Any]]) -> Any:
        try:
            return self._try_call(func, *attempts)
        except QmtAdapterError:
            return None

    def _try_call(self, func: Any, *attempts: tuple[tuple[Any, ...], dict[str, Any]]) -> Any:
        last_error: Exception | None = None
        for args, kwargs in attempts:
            try:
                return func(*args, **kwargs)
            except TypeError as exc:
                last_error = exc
                continue
        raise QmtAdapterError(f"unable to call xtdata API `{getattr(func, '__name__', repr(func))}`: {last_error}")

    def _to_plain(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "item") and callable(getattr(value, "item")):
            try:
                return value.item()
            except Exception:
                pass
        if isinstance(value, dict):
            return {str(key): self._to_plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._to_plain(item) for item in value]
        if hasattr(value, "dtype") and getattr(value.dtype, "names", None):
            names = list(value.dtype.names)
            if hasattr(value, "shape") and getattr(value, "shape", None):
                try:
                    row = value[0]
                except Exception:
                    row = value
            else:
                row = value
            return {name: self._to_plain(row[name]) for name in names}
        if hasattr(value, "tolist"):
            try:
                return self._to_plain(value.tolist())
            except Exception:
                pass
        if hasattr(value, "__dict__"):
            return {key: self._to_plain(item) for key, item in vars(value).items()}
        return str(value)
