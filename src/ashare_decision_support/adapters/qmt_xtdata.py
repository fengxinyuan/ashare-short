from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ashare_decision_support.adapters.base import MarketDataAdapter
from ashare_decision_support.schedule.calendar import parse_trade_date


class QmtAdapterError(RuntimeError):
    """Raised when the xtdata adapter cannot complete a requested action."""


class QmtXtDataAdapter(MarketDataAdapter):
    """Best-effort xtdata adapter for local QMT and MiniQMT environments."""

    def __init__(
        self,
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
        self.calendar_market = calendar_market

        if auto_connect:
            self.connect()

    def connect(self) -> None:
        connect_fn = getattr(self.xtdata, "connect", None)
        if callable(connect_fn):
            result = connect_fn()
            if result is False:
                raise QmtAdapterError("xtdata.connect() returned False")

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
        return [parse_trade_date(item) for item in raw]

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
        if hasattr(value, "item") and callable(value.item):
            try:
                return value.item()
            except Exception:  # noqa: S110 — 探测失败即「不是标量」，这就是答案
                pass
        if isinstance(value, dict):
            return {str(key): self._to_plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._to_plain(item) for item in value]
        if hasattr(value, "dtype") and getattr(value.dtype, "names", None):
            return self._from_record(value)
        if hasattr(value, "tolist"):
            try:
                return self._to_plain(value.tolist())
            except Exception:  # noqa: S110 — 同上，落到下面的通用分支
                pass
        if hasattr(value, "__dict__"):
            return {key: self._to_plain(item) for key, item in vars(value).items()}
        return str(value)

    def _from_record(self, value: Any) -> dict[str, Any]:
        """numpy 结构化数组（带 `dtype.names` 的那种）→ 普通 dict。

        切出来是为了让 `_to_plain` 保持一条**平的类型分发阶梯**：那张阶梯上每一行
        都是「认出一种形状，交出一个结果」，读的时候可以一行一行扫过去。八种形状
        里只有这一种在认出之后还要再判一次「拿到的是一整列还是其中一行」——那段
        判断和形状识别无关，留在阶梯中间会让人在扫到第七行之前先停下来读六行取行
        逻辑。`pyproject` 里为 `max-returns` 辩护的正是这条阶梯的形状，所以这次
        **一个 return 都没有从阶梯上拿走**，只把嵌套的那一层挪出去。
        """
        names = list(value.dtype.names)
        row = value
        if hasattr(value, "shape") and getattr(value, "shape", None):
            try:
                row = value[0]
            except Exception:
                row = value
        return {name: self._to_plain(row[name]) for name in names}
