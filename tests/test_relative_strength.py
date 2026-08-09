"""相对强度：个股减基准。

这一层存在的理由是 2026-08-04 牧原股份那一天——当日 −2.34%，深证成指 +2.18%，
两个数分开看都不吓人，差值 −4.5 个点才是当天最该看见的那一行。
"""

from __future__ import annotations

from datetime import date

from ashare_decision_support.agents import AgentContext, BenchmarkDailyAgent
from ashare_decision_support.signals.relative_strength import (
    LABEL_STRONG_UNDER,
    LABEL_SYNC,
    LABEL_UNDER,
    benchmark_for,
    measure_relative_strength,
)


def bars(closes: list[float], start_day: int = 1) -> list[dict]:
    return [{"date": f"2026-08-{start_day + i:02d}", "close": c} for i, c in enumerate(closes)]


# ---- 基准归属 ---------------------------------------------------------------


def test_benchmark_follows_the_board_not_the_exchange() -> None:
    """创业板/科创板的贝塔和主板差得远，按板块归属而不是交易所。"""
    assert benchmark_for("002714.SZ") == ("sz399001", "深证成指")
    assert benchmark_for("000001") == ("sz399001", "深证成指")
    assert benchmark_for("300750.SZ") == ("sz399006", "创业板指")
    assert benchmark_for("688981.SH") == ("sh000688", "科创50")
    assert benchmark_for("600519.SH") == ("sh000001", "上证指数")
    assert benchmark_for("603477") == ("sh000001", "上证指数")


def test_unknown_code_ranges_get_no_benchmark_rather_than_a_wrong_one() -> None:
    """北交所没有合适的宽基口径。硬套上证指数比不算更有害。"""
    assert benchmark_for("830799.BJ") is None
    assert benchmark_for("43") is None


# ---- 超额计算 ---------------------------------------------------------------


def test_the_muyuan_day_reads_as_significantly_underperforming() -> None:
    """个股跌 2.34% 而指数涨 2.18%，超额 −4.5 个点。"""
    stock = bars([38.93, 38.02])
    index = bars([13447.0, 13740.5])
    read = measure_relative_strength("002714.SZ", stock, index)

    assert read.benchmark_name == "深证成指"
    assert read.excess[1] < -0.044
    assert read.label == LABEL_STRONG_UNDER
    body = " ".join(read.describe())
    assert "深证成指" in body
    assert "-4.5" in body or "-4.4" in body


def test_moving_with_the_index_reads_as_sync_not_weak() -> None:
    """个股跌 1%、指数也跌 1%，那是大盘的事，不是它自己的事。"""
    read = measure_relative_strength("002714.SZ", bars([10.0, 9.9]), bars([3000.0, 2970.0]))

    assert read.label == LABEL_SYNC
    assert abs(read.excess[1]) < 0.005


def test_multi_window_excess_separates_today_from_the_month() -> None:
    """1 日答「今天」，20 日答「这一个月是不是一直在被卖」。"""
    stock = bars([round(100.0 - 0.5 * i, 2) for i in range(21)])
    index = bars([100.0] * 21)
    read = measure_relative_strength("600519.SH", stock, index)

    assert read.excess[1] < 0
    assert read.excess[20] < read.excess[5] < 0
    # 标签只看当日，不被 20 日的累计弱势带跑：单日只跌 0.55%，是「跑输」不是「显著」。
    assert read.label == LABEL_UNDER


def test_windows_that_lack_history_are_dropped_not_faked() -> None:
    read = measure_relative_strength("600519.SH", bars([10.0, 10.1]), bars([100.0, 100.5]))

    assert 1 in read.excess
    assert 5 not in read.excess
    assert 20 not in read.excess


# ---- 对齐与弃权 -------------------------------------------------------------


def test_mismatched_dates_align_on_the_intersection_and_say_so() -> None:
    """静默错位算出来的超额是最难发现的那种错，所以对齐要留一句话。"""
    stock = bars([10.0, 10.1, 10.2])
    index = bars([100.0, 100.5], start_day=2)
    read = measure_relative_strength("002714.SZ", stock, index)

    assert read.sessions_used == 2
    assert any("对齐" in note for note in read.notes)
    assert 1 in read.excess


def test_no_benchmark_abstains_instead_of_degrading_to_absolute_return() -> None:
    read = measure_relative_strength("830799.BJ", bars([10.0, 9.0]), bars([100.0, 99.0]))

    assert read.excess == {}
    assert read.label is None
    assert read.notes and "不出相对强度" in read.notes[0]


def test_a_single_common_session_abstains() -> None:
    """一天算不出收益率，弃权而不是给 0。"""
    read = measure_relative_strength("002714.SZ", bars([10.0]), bars([100.0]))

    assert read.excess == {}
    assert read.label is None


def test_non_numeric_and_zero_closes_are_dropped() -> None:
    stock = [{"date": "2026-08-01", "close": 10.0}, {"date": "2026-08-02", "close": "x"},
             {"date": "2026-08-03", "close": 0.0}, {"date": "2026-08-04", "close": 9.5}]
    read = measure_relative_strength("002714.SZ", stock, bars([100.0, 100.0, 100.0, 99.0]))

    assert read.sessions_used == 2
    assert read.excess[1] < 0


def test_benchmark_agent_fetches_each_required_index_once() -> None:
    class Adapter:
        def __init__(self) -> None:
            self.requested: list[str] = []

        def get_historical_bars(self, symbols, start, end):
            self.requested = list(symbols)
            return {symbol: bars([100.0, 101.0]) for symbol in symbols}

    adapter = Adapter()
    agent = BenchmarkDailyAgent(["600519.SH", "601398.SH", "300750.SZ", "830799.BJ"])
    data, notes = agent.run(AgentContext(trade_date=date(2026, 8, 8), adapter=adapter))

    assert adapter.requested == ["sh000001", "sz399006"]
    assert sorted(data["bars"]) == ["sh000001", "sz399006"]
    assert notes == []


def test_benchmark_agent_reports_missing_index_bars() -> None:
    class Adapter:
        def get_historical_bars(self, symbols, start, end):
            return {symbol: [] for symbol in symbols}

    data, notes = BenchmarkDailyAgent(["600519.SH"]).run(
        AgentContext(trade_date=date(2026, 8, 8), adapter=Adapter())
    )

    assert data["bars"] == {}
    assert notes == ["相对强度缺少宽基日线：sh000001"]
