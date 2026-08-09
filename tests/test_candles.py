"""K 线几何读数的测试。

这一层是反包类形态（`docs/KNOWLEDGE_VOLUME_PRICE.md` §3.2 / §3.3）的地基，测试要
守住的是**判据比的是价位不是形状**——一根又长又壮的阳线如果没站上前一根阴线的开盘
价，就不是反包。
"""

from __future__ import annotations

from ashare_decision_support.signals.candles import (
    ENGULF_BODY,
    ENGULF_RANGE,
    body_position,
    candle,
    close_position,
    engulf,
)


def bar(open_: float, high: float, low: float, close: float) -> dict:
    return {"open": open_, "high": high, "low": low, "close": close}


# ---- 收线 -----------------------------------------------------------------


def test_broken_bar_declines_instead_of_guessing() -> None:
    """high < low 是数据错误，整根弃权——颠倒的 K 线算出的反包是随机的。"""
    assert candle(bar(10.0, 9.0, 11.0, 10.5)) is None
    assert candle({"open": 10.0, "high": 11.0, "low": 9.0}) is None
    assert candle({"open": 10.0, "high": 11.0, "low": 9.0, "close": "-"}) is None


def test_limit_bar_is_legal() -> None:
    """一字板 high == low 是合法行情，不是坏数据。"""
    assert candle(bar(11.0, 11.0, 11.0, 11.0)) is not None


# ---- 日内位置 -------------------------------------------------------------


def test_close_position_spans_the_day_range() -> None:
    assert close_position(bar(10.0, 12.0, 10.0, 12.0)) == 1.0
    assert close_position(bar(10.0, 12.0, 10.0, 10.0)) == 0.0
    assert close_position(bar(10.0, 12.0, 10.0, 11.0)) == 0.5


def test_flat_bar_has_no_intraday_position() -> None:
    """一字板返回 None 而不是 0.5。

    给 0.5 会让一字涨停在「天量滞涨」里被当成中性收盘——那正是这条判据最不该
    误报的一天。
    """
    assert close_position(bar(11.0, 11.0, 11.0, 11.0)) is None


# ---- 相对实体位置 ---------------------------------------------------------


def test_body_position_locates_close_within_reference_body() -> None:
    """D0 实体 10 → 12，收在 11 就是实体正中。"""
    reference = bar(10.0, 12.5, 9.8, 12.0)
    assert body_position(bar(11.5, 11.6, 10.9, 11.0), reference) == 0.5


def test_body_position_is_not_clamped() -> None:
    """收在参考实体之外要给出 >1 / <0，那是真实且有用的信息。

    和 `volume_price._range_position` 的截断不同：那是个分位数，按定义就在区间内。
    """
    reference = bar(10.0, 12.5, 9.8, 12.0)
    assert body_position(bar(13.0, 13.2, 12.8, 13.0), reference) == 1.5
    assert body_position(bar(9.5, 9.6, 9.4, 9.5), reference) == -0.25


def test_body_position_undefined_against_a_doji() -> None:
    """参考实体高度为零时无意义，返回 None 而不是除零。"""
    assert body_position(bar(11.0, 11.2, 10.8, 11.0), bar(10.0, 10.5, 9.5, 10.0)) is None


# ---- 反包 -----------------------------------------------------------------


def test_engulf_compares_price_levels_not_candle_size() -> None:
    """**这是本模块的核心判据。**

    第二根阳线实体（1.2）远大于前一根阴线实体（0.5），形状上「更壮」，但收盘价
    9.9 没站上前阴的开盘价 10.5——那天买入的人还套着，反包不成立。
    """
    bars = [bar(10.5, 10.6, 9.9, 10.0), bar(8.7, 10.0, 8.6, 9.9)]
    assert engulf(bars) is None


def test_engulf_body_clears_the_prior_day_buyers() -> None:
    """收盘 ≥ 前阴开盘：那天买入的人全部回本。"""
    bars = [bar(10.5, 10.6, 9.9, 10.0), bar(10.0, 10.7, 9.95, 10.5)]
    assert engulf(bars) == ENGULF_BODY


def test_strong_engulf_clears_even_the_high_buyers() -> None:
    """收盘 > 前阴最高：连当天最高位接的人也解套。"""
    bars = [bar(10.5, 10.6, 9.9, 10.0), bar(10.0, 10.9, 9.95, 10.8)]
    assert engulf(bars) == ENGULF_RANGE


def test_engulfed_bar_must_be_bearish() -> None:
    """被吞的必须是阴线。吞一根阳线没有「解套」的含义。"""
    bars = [bar(10.0, 10.6, 9.9, 10.5), bar(10.5, 11.2, 10.4, 11.0)]
    assert engulf(bars) is None


def test_doji_is_not_engulfable() -> None:
    """十字星实体为零，没有堆积出可辨认的套牢盘——刻意从严，判不成立。"""
    bars = [bar(10.0, 10.6, 9.4, 10.0), bar(10.0, 11.2, 9.9, 11.0)]
    assert engulf(bars) is None


def test_engulfing_bar_must_be_bullish() -> None:
    """收尾那根必须是阳线，否则「站上去了」只是当天高开回落。"""
    bars = [bar(10.5, 10.6, 9.9, 10.0), bar(11.0, 11.2, 10.4, 10.6)]
    assert engulf(bars) is None


def test_cover_two_answers_the_whitepaper_top_rule() -> None:
    """白皮书铁律：高价区一根长阴，后两根大阳也不能吞没，则天价成立。

    两根阳线合力仍收在 12.0 之下 → 不成立（天价成立，该清仓）；再多涨一点站上
    12.0 → 成立。判据只看最后一根的收盘价，中间那根怎么走不影响结论。
    """
    long_bear = bar(12.0, 12.1, 10.4, 10.5)
    fails = [long_bear, bar(10.5, 11.2, 10.4, 11.0), bar(11.0, 11.9, 10.9, 11.8)]
    assert engulf(fails, cover=2) is None

    clears = [long_bear, bar(10.5, 11.2, 10.4, 11.0), bar(11.0, 12.3, 10.9, 12.1)]
    assert engulf(clears, cover=2) == ENGULF_BODY


def test_engulf_declines_without_enough_bars() -> None:
    """确认型判据：没数据确认和确认了不成立，对买入侧是同一个结论。"""
    assert engulf([bar(10.0, 10.5, 9.5, 10.2)]) is None
    assert engulf([]) is None
    assert engulf(None) is None
    assert engulf([bar(10.5, 10.6, 9.9, 10.0), bar(10.0, 10.7, 9.95, 10.5)], cover=0) is None
