"""交易日历的适配器契约、缺失数据恢复与刷新范围。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from ashare_decision_support.schedule.calendar import (
    TradingCalendar,
    next_session,
    session_forming,
)

# 2026-10-01 是周四（工作日却休市 → 应进 holidays）
# 2026-08-01 是周六（周末却开市 → 应进 workdays，调休）
NATIONAL_DAY = "2026-10-01"
MAKEUP_SATURDAY = "2026-08-01"
WEEKDAYS_IN_2026 = 261


def _trading_days_2026() -> list[date]:
    """全年工作日，减掉国庆当天，加上一个调休周六。"""
    days = []
    current = date(2026, 1, 1)
    while current.year == 2026:
        key = str(current)
        if (current.weekday() < 5 and key != NATIONAL_DAY) or key == MAKEUP_SATURDAY:
            days.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    return days


class RealContractAdapter:
    """按 `adapters/base.py` 的真实签名实现：**不接受任何参数**。

    akshare 那一路也是这样——`tool_trade_date_hist_sina()` 一次返回 1990 年以来
    的全部交易日，没有区间参数。所以这里也带上隔年的日期。
    """

    def __init__(self) -> None:
        self.calls = 0

    def get_trading_calendar(self) -> list[date]:
        self.calls += 1
        return [date(2025, 3, 3), date(2025, 3, 4), *_trading_days_2026()]


def test_refresh_calls_the_adapter_with_the_signature_it_actually_has() -> None:
    """这条就是那个 bug 的回归测试：改之前 refresh_from 传两个日期进去，
    适配器抛 TypeError，被 `except Exception` 咽掉，返回 0。"""
    adapter = RealContractAdapter()
    calendar = TradingCalendar()

    count = calendar.refresh_from(adapter, 2026)

    assert adapter.calls == 1
    assert count > 0, "刷新返回 0 说明调用又被吞了"


def test_a_weekday_that_is_closed_becomes_a_holiday() -> None:
    calendar = TradingCalendar()
    calendar.refresh_from(RealContractAdapter(), 2026)

    assert NATIONAL_DAY in calendar.holidays
    assert calendar.is_trading_day(date(2026, 10, 1)) is False
    assert "跳过" in calendar.explain(date(2026, 10, 1))


def test_a_weekend_that_is_open_becomes_a_makeup_workday() -> None:
    """没有 workdays 这一层，调休日会被「周末直接排除」那条规则判掉——
    代价是一整天没有任何监控。"""
    calendar = TradingCalendar()
    calendar.refresh_from(RealContractAdapter(), 2026)

    assert MAKEUP_SATURDAY in calendar.workdays
    assert calendar.is_trading_day(date(2026, 8, 1)) is True
    assert "调休" in calendar.explain(date(2026, 8, 1))


def test_the_count_is_this_years_trading_days_not_the_sources_whole_history() -> None:
    """akshare 一次给回三十多年的日期。报 `len(源给的全部)` 会让
    「刷新了 2026 年」打印出八千多天。"""
    count = TradingCalendar().refresh_from(RealContractAdapter(), 2026)

    # 261 个工作日 − 国庆 1 天 + 调休周六 1 天
    assert count == WEEKDAYS_IN_2026


def test_refresh_survives_a_source_that_has_no_calendar() -> None:
    """TickFlow 免费档 `get_trading_calendar()` 返回空列表。保持现状、返回 0，
    而不是把整张表清空——清空等于把已有的节假日信息丢掉。"""

    class Empty:
        def get_trading_calendar(self) -> list[date]:
            return []

    calendar = TradingCalendar()
    calendar.save(holidays=[NATIONAL_DAY], workdays=[])

    assert calendar.refresh_from(Empty(), 2026) == 0
    assert NATIONAL_DAY in calendar.holidays


def test_refresh_survives_an_adapter_without_the_method_or_one_that_raises() -> None:
    class NoMethod:
        pass

    class Broken:
        def get_trading_calendar(self) -> list[date]:
            raise RuntimeError("data source down")

    assert TradingCalendar().refresh_from(NoMethod(), 2026) == 0
    assert TradingCalendar().refresh_from(Broken(), 2026) == 0


def test_an_unconfigured_calendar_says_so_instead_of_claiming_certainty() -> None:
    """空表时不阻断，但结论里要写明「未经假期校验」——宁可说不确定，
    也不假装确定。"""
    calendar = TradingCalendar()

    assert calendar.configured is False
    assert calendar.is_trading_day(date(2026, 8, 3)) is True
    assert "未经假期校验" in calendar.explain(date(2026, 8, 3))


@pytest.mark.parametrize(
    "days",
    [
        [date(2025, 8, 1)],
        ["bad", "2026-08-03"],
        "2026-08-03",
        {"date": "2026-08-03"},
    ],
)
def test_unusable_or_wrong_year_calendar_does_not_close_every_weekday(days) -> None:
    class Source:
        def get_trading_calendar(self):
            return days

    calendar = TradingCalendar()
    calendar.save([NATIONAL_DAY], [MAKEUP_SATURDAY])
    before = calendar.path.read_bytes()

    assert calendar.refresh_from(Source(), 2026) == 0
    assert calendar.path.read_bytes() == before
    assert calendar.is_trading_day(date(2026, 8, 3)) is True


def test_partial_history_only_refreshes_covered_dates_and_keeps_other_years() -> None:
    class Source:
        def get_trading_calendar(self):
            return [date(2025, 12, 31), date(2026, 1, 5), date(2026, 1, 7)]

    calendar = TradingCalendar()
    calendar.save(["2025-10-01", NATIONAL_DAY], [MAKEUP_SATURDAY])

    assert calendar.refresh_from(Source(), 2026) == 2

    reloaded = TradingCalendar()
    assert reloaded.is_trading_day(date(2026, 1, 6)) is False
    assert reloaded.is_trading_day(date(2026, 1, 8)) is True
    assert {"2025-10-01", NATIONAL_DAY} <= reloaded.holidays
    assert MAKEUP_SATURDAY in reloaded.workdays


def test_calendar_refresh_normalizes_date_formats_before_classifying_holidays() -> None:
    class Source:
        def get_trading_calendar(self):
            return ["20260803", "2026-08-04T15:00:00+08:00", "2026-08-04T17:00:00Z"]

    calendar = TradingCalendar()
    assert calendar.refresh_from(Source(), 2026) == 3

    assert all(calendar.is_trading_day(date(2026, 8, day)) for day in (3, 4, 5))
    assert calendar.is_trading_day(date(2026, 7, 31)) is True
    assert calendar.is_trading_day(date(2026, 8, 6)) is True


def test_next_session_skips_weekends_and_listed_holidays() -> None:
    """周日跑的盘前计划是给周一的；国庆当天跑的是给节后第一个交易日的。"""
    calendar = TradingCalendar()
    calendar.save([NATIONAL_DAY], [MAKEUP_SATURDAY])

    assert next_session(date(2026, 9, 14), calendar) == date(2026, 9, 14)  # 周一，开市
    assert next_session(date(2026, 9, 13), calendar) == date(2026, 9, 14)  # 周日 → 周一
    assert next_session(date(2026, 10, 1), calendar) == date(2026, 10, 2)  # 节假日表
    assert next_session(date(2026, 8, 1), calendar) == date(2026, 8, 1)  # 调休周六，开市
    # 不传日历读默认表；用例隔离的状态目录里没有表，退回只排除周末。
    assert next_session(date(2026, 9, 12)) == date(2026, 9, 14)


@pytest.mark.parametrize(
    "moment, forming",
    [
        (datetime(2026, 9, 14, 8, 20), False),  # 盘前：上一交易日的整段
        (datetime(2026, 9, 14, 9, 15), True),  # 集合竞价开始报价
        (datetime(2026, 9, 14, 9, 33), True),  # 2026-09-14 实测跑批的时刻
        (datetime(2026, 9, 14, 12, 0), True),  # 午休：成交额只是上半场
        (datetime(2026, 9, 14, 15, 0), False),  # 收盘：今天的整段
        (datetime(2026, 9, 13, 10, 0), False),  # 周日：没有交易日在形成
        (datetime(2026, 10, 1, 10, 0), False),  # 节假日表里的休市日
    ],
)
def test_session_forming_is_the_intersection_of_calendar_and_clock(moment, forming) -> None:
    calendar = TradingCalendar()
    calendar.save([NATIONAL_DAY], [MAKEUP_SATURDAY])

    assert session_forming(moment, calendar) is forming
