"""交易日判定：默认工作日开市，节假日表和特殊开市日覆盖默认值。

缺表时不阻断工作日任务，并说明未经假期校验。刷新仅更新行情源实际覆盖的日期，
不能把尚未提供的未来交易日推断成休市；范围外的已知日历保持不变。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.schedule.clock import within_session_hours
from ashare_decision_support.schedule.timezone import to_beijing_naive
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import read_json_object, write_json

HOLIDAYS_FILE = "holidays.json"

# 往后找下一个交易日最多跨这么多个自然日。再长就不是长假而是日历表坏了，那时
# 原样交回，不无限循环。
MAX_CLOSED_STRETCH = 30


class TradingCalendar:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else state_path(HOLIDAYS_FILE)
        payload = read_json_object(self.path)
        self.holidays = _date_keys(payload.get("holidays"))
        # 调休上班日：周末但开市。没有这个集合，调休日会被第 1 层直接判掉。
        self.workdays = _date_keys(payload.get("workdays"))

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
        holiday_set = set(holidays)
        workday_set = set(workdays or [])
        write_json(
            self.path,
            {"holidays": sorted(holiday_set), "workdays": sorted(workday_set)},
        )
        self.holidays, self.workdays = holiday_set, workday_set

    def refresh_from(self, adapter: Any, year: int) -> int:
        """从零参数日历接口刷新指定年份，返回该年有效交易日数。

        数据无效或不含目标年份时保留旧表。首末交易日之外没有覆盖证据，
        只在源覆盖范围与目标年份的交集内推导节假日。
        """
        year_start, year_end = date(year, 1, 1), date(year, 12, 31)
        getter = getattr(adapter, "get_trading_calendar", None)
        if not callable(getter):
            return 0
        try:
            days = getter()
            if not isinstance(days, (list, tuple, set)):
                return 0
            trading = {parse_trade_date(item) for item in days}
        except Exception:
            return 0
        in_year = {day for day in trading if day.year == year}
        if not in_year:
            return 0

        start, end = max(year_start, min(trading)), min(year_end, max(trading))
        holidays = [key for key in self.holidays if not str(start) <= key <= str(end)]
        workdays = [key for key in self.workdays if not str(start) <= key <= str(end)]
        for ordinal in range(start.toordinal(), end.toordinal() + 1):
            current = date.fromordinal(ordinal)
            key = str(current)
            if current.weekday() < 5 and current not in trading:
                holidays.append(key)
            elif current.weekday() >= 5 and current in trading:
                workdays.append(key)
        self.save(holidays, workdays)
        return len(in_year)


def default_calendar() -> TradingCalendar | None:
    """节假日表。状态目录不可用（读不到、建不了）就是 None，判定退回只排除周末。"""
    try:
        return TradingCalendar()
    except Exception:
        return None


def is_session(day: date, calendar: TradingCalendar | None) -> bool:
    """`day` 开不开市；没有日历表（或日历表坏了）就只排除周末。"""
    if calendar is not None:
        try:
            return bool(calendar.is_trading_day(day))
        except Exception:
            return day.weekday() < 5
    return day.weekday() < 5


def next_session(day: date, calendar: TradingCalendar | None = None) -> date:
    """`day` 当天开市就是它本身，否则其后最近的一个交易日。

    周日跑的盘前计划是给周一的：预测的目标交易日要按日历转，不能把运行日原样
    写成交易日——那样盘中确认会去找一个不存在的周日截面。不传日历就读默认
    节假日表；表缺失时只排除周末。
    """
    if calendar is None:
        calendar = default_calendar()
    for offset in range(MAX_CLOSED_STRETCH + 1):
        current = day + timedelta(days=offset)
        if is_session(current, calendar):
            return current
    return day


def session_forming(moment: datetime, calendar: TradingCalendar | None = None) -> bool:
    """此刻拿到的当日行情是不是**半截交易日**：今天开市、集合竞价已开始、尚未收盘。

    盘前跑批拿到的是上一交易日的整段，盘后拿到的是今天的整段，都不算；09:15 到
    15:00 之间取到的成交额、涨跌幅、涨停池都还在形成中。2026-09-14 09:33 实测：
    `morning` 在开盘后跑，涨停池读到 15 家被判成「冰点」，全市场成交额 0.17 万亿
    让可交易性筛选多剔了六百只——两个读数都没错，错的是把它们当成了整天的数。
    """
    return is_session(moment.date(), calendar) and within_session_hours(moment)


def parse_trade_date(value: Any) -> date:
    """行情源的八位日期、ISO 日期/时刻统一转成北京时间的 date。"""
    if isinstance(value, datetime):
        return to_beijing_naive(value).date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return to_beijing_naive(datetime.fromisoformat(text)).date()


def _date_keys(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    dates: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            dates.add(str(date.fromisoformat(item)))
        except ValueError:
            continue
    return dates
