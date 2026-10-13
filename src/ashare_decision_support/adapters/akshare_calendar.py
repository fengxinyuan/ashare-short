"""AkShare 交易日历接入，供 calendar-refresh 更新本地节假日表。"""

from __future__ import annotations

from datetime import date
from typing import Any

from ashare_decision_support.schedule.calendar import parse_trade_date


class AkshareCalendarAdapter:
    def __init__(self, ak_module: Any | None = None) -> None:
        if ak_module is None:
            try:
                import akshare as ak_module
            except ImportError as exc:
                raise RuntimeError(
                    "akshare is not available. Install it with `pip install -e .[akshare]`."
                ) from exc
        self.ak = ak_module

    def get_trading_calendar(self) -> list[date]:
        """读取完整日历；坏日期交给刷新层处理，避免漏掉交易日后误推休市。"""
        getter = getattr(self.ak, "tool_trade_date_hist_sina", None)
        if not callable(getter):
            return []
        table = getter()
        if table is None:
            return []
        to_dict = getattr(table, "to_dict", None)
        rows = to_dict(orient="records") if callable(to_dict) else table
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise TypeError(f"unsupported AkShare calendar payload type: {type(rows)!r}")
        return [
            parse_trade_date(row.get("trade_date") or row.get("交易日") or row.get("date"))
            for row in rows
        ]
