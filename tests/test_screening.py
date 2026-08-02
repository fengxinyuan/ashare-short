"""适配性筛选的单测。

重点不在「跑通」，在**几个必须成立的性质**：oracle 口径必须 ≥ 可执行口径、一字板
必须被剔除、只在一段赚钱的票必须被砍、样本外衰减必须被识别出来。这几条任何一条
坏掉，模块的结论就是假的，而单元测试是唯一能在没有实盘数据时守住它们的东西。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ashare_decision_support.screening.runner import ScreenReport, run_screen
from ashare_decision_support.screening.suitability import (
    CostModel,
    ScreenConfig,
    limit_pct,
    screen_many,
    screen_symbol,
)


def build_bars(
    count: int = 300,
    *,
    daily_edge: float = 0.03,
    start_price: float = 20.0,
    volume: float = 4_000_000.0,
    amount: float | None = 4e8,
    drift: float = 0.0,
) -> list[dict]:
    """一条节律稳定的日线：低开、盘中冲高、尾盘收在区间下沿。

    这是低吸最理想的形态——**收盘买得到、次日盘中卖得掉、但开盘卖不到**。三档
    口径因此会拉开明显差距，正好用来验证它们没被实现串。

    价格中枢刻意不漂移（`drift=0`），这样测出来的收益只可能来自日内节律，不会
    被趋势混进去——否则「适合超短线」和「一直在涨」两件事分不开。

    开收盘用**区间内的相对位置**而不是乘一个固定系数，这样 `daily_edge` 取到
    很小的值时 OHLC 仍然自洽，不会被 `_normalise` 当成脏数据丢掉。
    """
    start = datetime(2025, 1, 2)
    bars: list[dict] = []
    price = start_price
    prev_close = price
    for idx in range(count):
        low = price * (1 - daily_edge / 2)
        high = price * (1 + daily_edge / 2)
        span = high - low
        open_ = low + span * 0.03
        close = low + span * 0.06
        bars.append(
            {
                "timestamp": start + timedelta(days=idx),
                "open": round(open_, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "prev_close": round(prev_close, 4),
                "volume": volume,
                "amount": amount,
            }
        )
        prev_close = close
        price = price * (1 + drift)
    return bars


# ---------------------------------------------------------------------------
# 口径关系
# ---------------------------------------------------------------------------


def test_oracle_dominates_both_executable_modes() -> None:
    """上帝视角必须是上限：它买在全天最低、卖在次日最高，逐笔都不可能被超过。

    只断言 oracle 压住另外两档。`neutral` 和 `strict` 之间**没有固定顺序**——次日
    开盘价在它自己的均价之上还是之下，是这只票的性质，不是实现的约束。
    """
    card = screen_symbol("600519.SH", build_bars())

    assert card.capture.oracle_median >= card.capture.neutral_median
    assert card.capture.oracle_median >= card.capture.strict_median


def test_timing_premium_is_reported() -> None:
    """择时溢价必须显式算出来——它是这个模块存在的理由。"""
    card = screen_symbol("600519.SH", build_bars())

    assert card.capture.timing_premium is not None
    assert card.capture.timing_premium > 0
    assert 0.0 < card.capture.executable_ratio < 1.0


def test_cost_is_deducted() -> None:
    """成本必须真的扣掉：滑点调大，净收益必须同步下降。"""
    cheap = screen_symbol("600519.SH", build_bars(), ScreenConfig(cost=CostModel(0.0)))
    dear = screen_symbol("600519.SH", build_bars(), ScreenConfig(cost=CostModel(0.01)))

    assert dear.capture.neutral_median < cheap.capture.neutral_median


# ---------------------------------------------------------------------------
# L0 可交易性
# ---------------------------------------------------------------------------


def test_thin_liquidity_is_rejected_first() -> None:
    bars = build_bars(amount=1e6)
    card = screen_symbol("600519.SH", bars)

    assert card.verdict == "reject"
    assert card.rejected_at == "tradability"
    assert any("成交额" in reason for reason in card.reasons)


def test_penny_stock_is_rejected() -> None:
    card = screen_symbol("600519.SH", build_bars(start_price=1.5))

    assert card.rejected_at == "tradability"
    assert any("最低价" in reason for reason in card.reasons)


def test_short_history_is_data_reject_not_quality_reject() -> None:
    """样本不够和「算出来不合格」必须分开——前者不该污染漏斗统计。"""
    card = screen_symbol("600519.SH", build_bars(count=40))

    assert card.rejected_at == "data"


# ---------------------------------------------------------------------------
# 一字板与停牌
# ---------------------------------------------------------------------------


def test_one_word_boards_are_excluded_from_samples() -> None:
    """一字涨停日不能当买入日。回测虚高的头号来源就是这里。"""
    bars = build_bars()
    for idx in (50, 51, 52):
        price = round(bars[idx]["prev_close"] * 1.10, 4)
        bars[idx].update(open=price, high=price, low=price, close=price)

    card = screen_symbol("600519.SH", bars, ScreenConfig(max_one_word_rate=0.5))

    assert card.capture.unfillable >= 3


def test_suspended_days_are_excluded() -> None:
    bars = build_bars()
    bars[80]["volume"] = 0
    bars[80]["amount"] = 0

    card = screen_symbol("600519.SH", bars, ScreenConfig(max_suspension_rate=0.5))

    assert card.capture.unfillable >= 2  # 停牌日既当不了买入日也当不了卖出日


def test_limit_up_close_is_excluded_by_default() -> None:
    """涨停收盘买不到。关掉开关样本数必须回来，这验证了开关真的在起作用。"""
    bars = build_bars()
    for idx in (100, 120, 140):
        bars[idx]["close"] = round(bars[idx]["prev_close"] * 1.10, 4)
        bars[idx]["high"] = bars[idx]["close"]

    strict = screen_symbol("600519.SH", bars)
    loose = screen_symbol("600519.SH", bars, ScreenConfig(exclude_limit_up_close=False))

    assert loose.capture.samples > strict.capture.samples


def test_limit_pct_by_board() -> None:
    assert limit_pct("600519.SH") == 0.10
    assert limit_pct("300750.SZ") == 0.20
    assert limit_pct("688981.SH") == 0.20
    assert limit_pct("830799.BJ") == 0.30


# ---------------------------------------------------------------------------
# L2 跨期一致性
# ---------------------------------------------------------------------------


def test_returns_concentrated_in_one_segment_are_rejected() -> None:
    """只在一段赚钱的票必须被砍掉——全样本中位数会掩盖这件事。"""
    good = build_bars(count=100, daily_edge=0.05)
    flat = build_bars(count=200, daily_edge=0.0005)
    card = screen_symbol("600519.SH", good + flat)

    assert card.verdict == "reject"
    assert card.rejected_at in {"capture", "consistency"}


def test_segment_medians_are_reported() -> None:
    card = screen_symbol("600519.SH", build_bars())

    assert len(card.consistency.segments) == 4
    assert card.consistency.segment_pass == 4


def test_consistency_uses_median_not_mean() -> None:
    """插一天暴涨不该把结论翻过来。均值会，中位数不会。"""
    bars = build_bars(daily_edge=0.001)
    bars[60]["high"] = bars[60]["low"] * 2.0

    card = screen_symbol("600519.SH", bars)

    assert card.verdict == "reject"


# ---------------------------------------------------------------------------
# L4 样本外
# ---------------------------------------------------------------------------


def test_holdout_decay_downgrades_to_unstable() -> None:
    """样本内漂亮、样本外垮掉 → `unstable`，不能进 `pass`。"""
    in_sample = build_bars(count=210, daily_edge=0.035)
    out_sample = build_bars(count=90, daily_edge=0.006, start_price=20.0)
    card = screen_symbol("600519.SH", in_sample + out_sample)

    assert card.verdict == "unstable"
    assert card.decay is not None and card.decay < 0.5


def test_stable_symbol_passes_all_layers() -> None:
    card = screen_symbol("600519.SH", build_bars())

    assert card.verdict == "pass", card.reasons
    assert card.rejected_at is None
    assert card.rank_key is not None


def test_rank_key_comes_from_holdout_not_full_sample() -> None:
    """排序也会过拟合。排序键必须取样本外。"""
    card = screen_symbol("600519.SH", build_bars())

    assert card.rank_key == card.holdout_consistency.median


# ---------------------------------------------------------------------------
# 批量与报告
# ---------------------------------------------------------------------------


def test_screen_many_sorts_by_holdout_and_keeps_rejects() -> None:
    cards = screen_many(
        {
            "600519.SH": build_bars(daily_edge=0.03),
            "000001.SZ": build_bars(daily_edge=0.05),
            "600000.SH": build_bars(amount=1e6),
        }
    )

    assert len(cards) == 3  # 淘汰的也要留着，漏斗统计要用
    assert cards[0].symbol == "000001.SZ"
    assert cards[-1].verdict == "reject"


class _StubAdapter:
    def __init__(self, bars_by_symbol: dict[str, list[dict]]) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.calls = 0

    def get_historical_bars(self, symbols, start, end):
        self.calls += 1
        return {symbol: self.bars_by_symbol.get(symbol, []) for symbol in symbols}


def test_run_screen_builds_funnel_and_excludes_missing_symbols() -> None:
    adapter = _StubAdapter(
        {
            "600519.SH": build_bars(),
            "000001.SZ": build_bars(amount=1e6),
        }
    )
    report = run_screen(adapter, ["600519.SH", "000001.SZ", "999999.SH"], end=date(2026, 8, 2))

    assert isinstance(report, ScreenReport)
    assert report.universe == 3
    assert report.fetched == 2  # 无数据的那只不进漏斗
    assert report.funnel["tradability"] == 1
    assert report.funnel["pass"] == 1
    assert any("未取到日线" in note for note in report.notes)


def test_run_screen_survives_adapter_failure() -> None:
    class _Broken:
        def get_historical_bars(self, symbols, start, end):
            raise RuntimeError("upstream down")

    report = run_screen(_Broken(), ["600519.SH"], end=date(2026, 8, 2))

    assert report.fetched == 0
    assert report.passed == []


def test_describe_flags_empty_result() -> None:
    adapter = _StubAdapter({"600519.SH": build_bars(amount=1e6)})
    report = run_screen(adapter, ["600519.SH"], end=date(2026, 8, 2))
    text = "\n".join(report.describe())

    assert "无标的通过" in text
    assert "理论上限" in text  # 口径提醒必须每次都印


def test_dirty_bars_are_dropped_not_crashed() -> None:
    bars = build_bars()
    bars[10].update(high=1.0, low=99.0)  # high < low
    bars[11]["close"] = None

    card = screen_symbol("600519.SH", bars)

    assert card.tradability.bars == len(bars) - 2
