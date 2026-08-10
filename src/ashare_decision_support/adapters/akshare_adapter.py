from __future__ import annotations

from datetime import date
from typing import Any

from ashare_decision_support.adapters.base import MarketDataAdapter
from ashare_decision_support.schedule.calendar import parse_trade_date


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
                    result.append(parse_trade_date(value))
            return result
        return []

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
                except Exception:  # noqa: S110 — 逐个试签名，试不通就换下一种
                    pass
        if isinstance(table, dict):
            return [table]
        raise AkshareAdapterError(f"unsupported AkShare table payload type: {type(table)!r}")

    def _code(self, symbol: str) -> str:
        base = symbol.split(".", maxsplit=1)[0]
        return base.zfill(6)
