from __future__ import annotations

from datetime import date, datetime, time
import json
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.base import MarketDataAdapter
from ashare_decision_support.models.schemas import AuctionFeatures, OpenFeatures


class AkshareAdapterError(RuntimeError):
    """Raised when the AkShare adapter cannot complete a requested action."""


class AkshareAdapter(MarketDataAdapter):
    """Analysis-oriented adapter backed by AkShare spot and minute data."""

    def __init__(self, ak_module: Any | None = None) -> None:
        if ak_module is None:
            try:
                import akshare as ak_module  # type: ignore
            except ImportError as exc:
                raise AkshareAdapterError(
                    "akshare is not available. Install it with `pip install -e .[akshare]` "
                    "or `pip install akshare` on your local machine."
                ) from exc
        self.ak = ak_module

    def subscribe_quotes(self, symbols: list[str]) -> None:
        return None

    def subscribe_l2_snapshot(self, symbols: list[str]) -> None:
        return None

    def subscribe_ticks(self, symbols: list[str]) -> None:
        return None

    def subscribe_orders(self, symbols: list[str]) -> None:
        return None

    def get_security_master(self, symbols: list[str]) -> dict[str, dict]:
        rows_by_code = self._spot_rows_by_code(symbols)
        return {
            symbol: rows_by_code.get(self._code(symbol), {"symbol": symbol, "code": self._code(symbol)})
            for symbol in symbols
        }

    def get_historical_bars(self, symbols: list[str], start: date, end: date) -> dict[str, list[dict]]:
        hist_fn = getattr(self.ak, "stock_zh_a_hist", None)
        if not callable(hist_fn):
            raise AkshareAdapterError("ak.stock_zh_a_hist is not available")

        result: dict[str, list[dict]] = {}
        for symbol in symbols:
            code = self._code(symbol)
            df = hist_fn(
                symbol=code,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="",
            )
            rows = self._rows(df)
            result[symbol] = rows
        return result

    def get_trading_calendar(self) -> list[date]:
        calendar_fn = getattr(self.ak, "tool_trade_date_hist_sina", None)
        if callable(calendar_fn):
            raw = calendar_fn()
            rows = self._rows(raw)
            result = []
            for row in rows:
                value = row.get("trade_date") or row.get("交易日") or row.get("date")
                if value:
                    result.append(self._parse_date(value))
            return result
        return []

    def get_auction_features(self, symbols: list[str]) -> dict[str, AuctionFeatures]:
        rows_by_code = self._spot_rows_by_code(symbols)
        now = datetime.now()
        features: dict[str, AuctionFeatures] = {}
        for symbol in symbols:
            row = rows_by_code.get(self._code(symbol))
            if row is None:
                raise AkshareAdapterError(f"symbol {symbol} not found in ak.stock_zh_a_spot_em() output")
            latest = self._float(row, "最新价")
            open_price = self._float(row, "今开")
            prev_close = self._float(row, "昨收")
            amount = self._float(row, "成交额")
            volume_hand = self._float(row, "成交量")
            matched_qty = int(volume_hand * 100) if volume_hand is not None else 0
            open_gap_pct = 0.0
            if open_price is not None and prev_close not in (None, 0):
                open_gap_pct = (open_price / prev_close - 1.0) * 100.0

            features[symbol] = AuctionFeatures(
                symbol=symbol,
                matched_qty=matched_qty,
                matched_amount=amount or 0.0,
                unmatched_qty=0,
                unmatched_side="N",
                indicative_match_price=latest or open_price or prev_close or 0.01,
                open_gap_pct=open_gap_pct,
                cancel_pre920_ratio=None,
                seal_ratio=None,
                tail_flow_distortion=False,
                data_stale=True,
                source_ts=now,
            )
        return features

    def get_open_features(self, symbols: list[str]) -> dict[str, OpenFeatures]:
        rows_by_code = self._spot_rows_by_code(symbols)
        features: dict[str, OpenFeatures] = {}
        end_dt = datetime.now()
        start_dt = datetime.combine(end_dt.date(), time(9, 30))

        hist_min_fn = getattr(self.ak, "stock_zh_a_hist_min_em", None)
        if not callable(hist_min_fn):
            raise AkshareAdapterError("ak.stock_zh_a_hist_min_em is not available")

        for symbol in symbols:
            code = self._code(symbol)
            row = rows_by_code.get(code)
            if row is None:
                raise AkshareAdapterError(f"symbol {symbol} not found in ak.stock_zh_a_spot_em() output")

            minute_df = hist_min_fn(
                symbol=code,
                start_date=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                period="1",
                adjust="",
            )
            minute_rows = self._rows(minute_df)
            minute_rows = [item for item in minute_rows if item]

            latest_price = self._float(row, "最新价")
            open_price = self._float(row, "今开")
            prev_close = self._float(row, "昨收")
            volume_ratio = self._float(row, "量比") or 1.0

            last_bar = minute_rows[-1] if minute_rows else {}
            bar_close = self._float(last_bar, "收盘")
            current_price = bar_close or latest_price or open_price or prev_close
            if current_price is None:
                raise AkshareAdapterError(f"cannot determine current price for symbol {symbol}")

            vwap = self._float(last_bar, "均价")
            if vwap is None:
                amount = self._float(last_bar, "成交额")
                volume_hand = self._float(last_bar, "成交量")
                if amount is not None and volume_hand not in (None, 0):
                    vwap = amount / (volume_hand * 100)
                else:
                    vwap = current_price

            bar_time_value = last_bar.get("时间")
            bar_dt = self._parse_datetime(bar_time_value) if bar_time_value else end_dt
            open_gap_pct = 0.0
            if open_price is not None and prev_close not in (None, 0):
                open_gap_pct = (open_price / prev_close - 1.0) * 100.0
            elif prev_close not in (None, 0):
                open_gap_pct = (current_price / prev_close - 1.0) * 100.0

            features[symbol] = OpenFeatures(
                symbol=symbol,
                minutes_since_open=self._minutes_since_open(bar_dt),
                price_vs_open_pct=((current_price / open_price - 1.0) * 100.0) if open_price not in (None, 0) else 0.0,
                price_vs_vwap_pct=((current_price / vwap - 1.0) * 100.0) if vwap not in (None, 0) else 0.0,
                volume_ratio=volume_ratio,
                open_gap_pct=open_gap_pct,
                data_stale=False,
                source_ts=bar_dt,
            )
        return features

    def health_check(self) -> dict[str, Any]:
        return {
            "adapter": "AkshareAdapter",
            "capabilities": {
                "has_stock_zh_a_spot_em": callable(getattr(self.ak, "stock_zh_a_spot_em", None)),
                "has_stock_zh_a_hist": callable(getattr(self.ak, "stock_zh_a_hist", None)),
                "has_stock_zh_a_hist_min_em": callable(getattr(self.ak, "stock_zh_a_hist_min_em", None)),
                "has_stock_zt_pool_dtgc_em": callable(getattr(self.ak, "stock_zt_pool_dtgc_em", None)),
                "has_stock_board_concept_name_em": callable(getattr(self.ak, "stock_board_concept_name_em", None)),
            },
        }

    def dump_snapshot(self, symbols: list[str]) -> dict[str, Any]:
        rows_by_code = self._spot_rows_by_code(symbols)
        snapshot = {
            "generated_at": datetime.now().isoformat(),
            "symbols": symbols,
            "spot": {},
            "minute": {},
        }
        for symbol in symbols:
            code = self._code(symbol)
            row = rows_by_code.get(code)
            if row is not None:
                snapshot["spot"][symbol] = row
            try:
                minute_rows = self._rows(
                    self.ak.stock_zh_a_hist_min_em(
                        symbol=code,
                        start_date=datetime.combine(datetime.now().date(), time(9, 30)).strftime("%Y-%m-%d %H:%M:%S"),
                        end_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        period="1",
                        adjust="",
                    )
                )
                snapshot["minute"][symbol] = minute_rows[-5:]
            except Exception as exc:
                snapshot["minute"][symbol] = {"error": str(exc)}
        return snapshot

    def dump_snapshot_to_file(self, symbols: list[str], output_path: str | Path) -> None:
        payload = self.dump_snapshot(symbols)
        Path(output_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def _spot_rows_by_code(self, symbols: list[str]) -> dict[str, dict]:
        spot_fn = getattr(self.ak, "stock_zh_a_spot_em", None)
        if not callable(spot_fn):
            raise AkshareAdapterError("ak.stock_zh_a_spot_em is not available")
        rows = self._rows(spot_fn())
        code_set = {self._code(symbol) for symbol in symbols}
        result = {}
        for row in rows:
            code = str(row.get("代码") or row.get("symbol") or "").zfill(6)
            if code in code_set:
                result[code] = row
        return result

    def _rows(self, table: Any) -> list[dict]:
        if table is None:
            return []
        if isinstance(table, list):
            return [item if isinstance(item, dict) else {"value": item} for item in table]
        to_dict = getattr(table, "to_dict", None)
        if callable(to_dict):
            try:
                return to_dict(orient="records")
            except TypeError:
                try:
                    data = to_dict()
                    if isinstance(data, list):
                        return data
                except Exception:
                    pass
        if isinstance(table, dict):
            return [table]
        raise AkshareAdapterError(f"unsupported AkShare table payload type: {type(table)!r}")

    def _code(self, symbol: str) -> str:
        base = symbol.split(".")[0]
        return base.zfill(6)

    def _float(self, row: dict, key: str) -> float | None:
        value = row.get(key)
        if value in (None, "", "-"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _parse_date(self, value: Any) -> date:
        if isinstance(value, date):
            return value
        text = str(value)
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return datetime.fromisoformat(text).date()

    def _parse_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        text = str(value)
        if len(text) == 19:
            return datetime.fromisoformat(text)
        if len(text) == 16:
            return datetime.fromisoformat(f"{text}:00")
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
