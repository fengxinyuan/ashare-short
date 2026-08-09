"""交易日历：`refresh_from` 此前调错了适配器的签名，而错误被吞掉了。

`adapters/base.py` 的契约是 **零参数**的 `get_trading_calendar()`，四个适配器
无一例外。`refresh_from` 却按 `(start, end)` 调它——任何适配器都会抛 `TypeError`，
而函数里那个 `except Exception: return 0` 把它咽成「这个源没有日历」。

于是刷新永远返回 0、节假日表永远是空的，而这和「免费档确实不提供日历」
长得一模一样。这组测试就是为了让这两件事再也长得不一样。
"""

from __future__ import annotations

from datetime import date

from ashare_decision_support.schedule.calendar import TradingCalendar

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
