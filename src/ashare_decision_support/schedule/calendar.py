"""交易日判定。

`get_trading_calendar()` 在免费档返回空列表，所以判定分三层，从便宜到贵：

1. **周末**——直接排除，不用查任何东西。
2. **节假日表**——`holidays.json`，可手工维护也可从行情源刷新。缺表时不阻断，
   但会在结论里说明「未经节假日校验」，而不是假装确定。
3. **数据源实证**——真要确认，就看行情源当天有没有出数据。这一层最准也最慢，
   只在调用方明确要求时才走。

宁可说「不确定」也不假装确定：假日误判成交易日，最坏结果是跑一轮空数据并推一条
无意义的简报；交易日误判成假日，最坏结果是一整天没有任何监控。两者不对称，所以
默认偏向「跑」。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import read_json, write_json

HOLIDAYS_FILE = "holidays.json"


class TradingCalendar:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else state_path(HOLIDAYS_FILE)
        payload = read_json(self.path, default={}) or {}
        self.holidays: set[str] = set(payload.get("holidays") or [])
        # 调休上班日：周末但开市。没有这个集合，调休日会被第 1 层直接判掉。
        self.workdays: set[str] = set(payload.get("workdays") or [])

    @property
    def configured(self) -> bool:
        return bool(self.holidays or self.workdays)

    def is_trading_day(self, day: date) -> bool:
        key = str(day)
        if key in self.holidays:
            return False
        if key in self.workdays:
            return True
        return day.weekday() < 5

    def explain(self, day: date) -> str:
        key = str(day)
        if key in self.holidays:
            return f"{day} 在节假日表中，跳过"
        if key in self.workdays:
            return f"{day} 为调休交易日"
        if day.weekday() >= 5:
            return f"{day} 是周末，跳过"
        if not self.configured:
            return f"{day} 是工作日（未配置节假日表，未经假期校验）"
        return f"{day} 是交易日"

    def save(self, holidays: list[str], workdays: list[str] | None = None) -> None:
        self.holidays = set(holidays)
        self.workdays = set(workdays or [])
        write_json(
            self.path,
            {"holidays": sorted(self.holidays), "workdays": sorted(self.workdays)},
        )

    def refresh_from(self, adapter: Any, year: int) -> int:
        """行情源给得出日历就用它，给不出就保持现状并返回 0。"""
        getter = getattr(adapter, "get_trading_calendar", None)
        if not callable(getter):
            return 0
        try:
            days = getter(date(year, 1, 1), date(year, 12, 31))
        except Exception:
            return 0
        trading = {str(item) for item in days or []}
        if not trading:
            return 0

        holidays: list[str] = []
        workdays: list[str] = []
        current = date(year, 1, 1)
        while current.year == year:
            key = str(current)
            if current.weekday() < 5 and key not in trading:
                holidays.append(key)
            elif current.weekday() >= 5 and key in trading:
                workdays.append(key)
            current = date.fromordinal(current.toordinal() + 1)
        self.save(holidays, workdays)
        return len(trading)
