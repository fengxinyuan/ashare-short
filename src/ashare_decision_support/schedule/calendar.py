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

from datetime import date, datetime
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
        """行情源给得出日历就用它，给不出就保持现状并返回 0。

        **契约是零参数的**（`adapters/base.py:get_trading_calendar`）：四个适配器
        全部返回全量交易日，不接受区间。这里原来按 `(start, end)` 调，任何适配器
        都会抛 `TypeError`，而下面那个 `except Exception` 把它咽成「这个源没有
        日历」——一个永远返回 0 的刷新，看起来和「免费档确实没有日历」一模一样。
        年份的筛选在下面那个循环里做，本来就不需要数据源配合。
        """
        getter = getattr(adapter, "get_trading_calendar", None)
        if not callable(getter):
            return 0
        try:
            days = getter()
        except Exception:
            return 0
        trading = {str(item) for item in days or []}
        if not trading:
            return 0

        holidays: list[str] = []
        workdays: list[str] = []
        in_year = 0
        current = date(year, 1, 1)
        while current.year == year:
            key = str(current)
            if key in trading:
                in_year += 1
            if current.weekday() < 5 and key not in trading:
                holidays.append(key)
            elif current.weekday() >= 5 and key in trading:
                workdays.append(key)
            current = date.fromordinal(current.toordinal() + 1)
        self.save(holidays, workdays)
        # 返回的是**这一年**的交易日数，不是数据源给了多少天。akshare 一次给回
        # 1990 年以来的全部日期，报 `len(trading)` 会让「刷新了 2026 年」打印出
        # 八千多天。
        return in_year


def parse_trade_date(value: Any) -> date:
    """把行情源给的交易日值解析成 `date`。

    两种写法都要认：`"20260803"`（八位数字，QMT 与多数国内源的口径）和
    ISO 的 `"2026-08-03"`。两个适配器原来各写了一份逐字相同的实现。
    """
    if isinstance(value, date):
        return value
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return datetime.fromisoformat(text).date()
