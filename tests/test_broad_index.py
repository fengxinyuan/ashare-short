"""宽基指数（市值加权口径）。

这一层补的是等权读数的**方向性盲区**，所以测试守的是三条：

1. 背离必须两条同时成立——宽度高 **且** 指数在跌。少一条都不是背离。
2. 跨交易日不比。`hq.sinajs.cn` 给的是实时行情，盘中取到的宽基是此刻的，
   而等权那一路是昨收，混比就是这套系统在别处一直防的跨时点比较。
3. 只出话不出分。这一层不许碰 score 与仓位系数。
"""

from __future__ import annotations

from ashare_decision_support.adapters.broad_index import (
    BroadIndexAdapter,
    BroadIndexRead,
    IndexQuote,
    read_divergence,
)
from tests.fakes import FakeResponse


class StubHttp:
    def __init__(self, body: str) -> None:
        self.body = body
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        return FakeResponse(self.body, encoding="gbk")


def line(code: str, name: str, prev: float, last: float, day: str = "2026-08-03") -> str:
    """新浪指数行：名称,今开,昨收,当前,最高,最低,...,日期,时间,"""
    fields = [name, str(last), str(prev), str(last), str(last), str(last)]
    fields += ["0"] * 2 + ["1000", "100000"] + ["0"] * 20
    fields += [day, "15:00:00", "00"]
    return f'var hq_str_{code}="{",".join(fields)}";'


def quotes(*rows: tuple[str, float]) -> BroadIndexRead:
    return BroadIndexRead(
        quotes=[
            IndexQuote(code=f"sh{index:06d}", name=name, close=100.0 * (1 + ret), prev_close=100.0)
            for index, (name, ret) in enumerate(rows)
        ],
        trade_date="2026-08-03",
    )


# 取数与解析


def test_parses_index_rows_and_computes_returns() -> None:
    body = "\n".join(
        [
            line("sh000001", "上证指数", 3832.2624, 3809.6629),
            line("sh000688", "科创50", 1635.9, 1552.89),
        ]
    )
    read = BroadIndexAdapter(
        indices=(("sh000001", "上证指数"), ("sh000688", "科创50")), opener=StubHttp(body)
    ).fetch()

    assert read.ok
    assert read.counted == 2
    assert read.trade_date == "2026-08-03"
    picked = {quote.name: quote for quote in read.quotes}
    assert abs(picked["上证指数"].ret + 0.0059) < 1e-3
    assert abs(picked["科创50"].ret + 0.0508) < 1e-3
    assert read.worst().name == "科创50"


def test_a_halted_index_is_dropped_instead_of_published_as_zero() -> None:
    """停牌/未开盘的指数返回 0。把 0.00 当点位发出去比报缺失更糟。"""
    body = "\n".join(
        [line("sh000001", "上证指数", 3832.0, 3809.0), line("sh000688", "科创50", 1635.0, 0.0)]
    )
    read = BroadIndexAdapter(
        indices=(("sh000001", "上证指数"), ("sh000688", "科创50")), opener=StubHttp(body)
    ).fetch()

    assert read.counted == 1
    assert any("缺 1 个" in note for note in read.notes)


def test_mixed_trade_dates_void_the_whole_read() -> None:
    """不同指数停在不同交易日 → 至少有一路是旧的，中位数就成了跨日混算。"""
    body = "\n".join(
        [
            line("sh000001", "上证指数", 3832.0, 3809.0, day="2026-08-03"),
            line("sh000688", "科创50", 1635.0, 1552.0, day="2026-07-31"),
        ]
    )
    read = BroadIndexAdapter(
        indices=(("sh000001", "上证指数"), ("sh000688", "科创50")), opener=StubHttp(body)
    ).fetch()

    assert not read.ok
    assert any("交易日不一致" in note for note in read.notes)


# 背离判据


def test_high_breadth_with_falling_indices_is_flagged() -> None:
    """2026-08-03 的真实形状：七成成分股在涨，每一个宽基都在跌。"""
    read = quotes(
        ("上证指数", -0.0059), ("沪深300", -0.0098), ("科创50", -0.0508), ("中证1000", 0.0006)
    )

    reasons, warnings = read_divergence(read, breadth=0.7613, expected_date="2026-08-03")

    assert reasons
    assert any("等权与市值加权背离" in text for text in warnings)
    # 科创50 单独点名——它会被其余指数的中位数平掉，而它正是当天的主线。
    assert any("科创50" in text and "-5" in text for text in warnings)


def test_low_breadth_with_falling_indices_is_not_a_divergence() -> None:
    """宽度本身就低时，指数跟着跌是**一致**，不是背离。

    少了这一条，判据会退化成「只要指数跌就告警」，那和直接看指数没有区别。
    """
    read = quotes(("上证指数", -0.012), ("沪深300", -0.015), ("中证1000", -0.009))

    _reasons, warnings = read_divergence(read, breadth=0.28, expected_date="2026-08-03")

    assert not any("背离" in text for text in warnings)


def test_high_breadth_with_rising_indices_is_not_a_divergence() -> None:
    read = quotes(("上证指数", 0.008), ("沪深300", 0.011), ("中证1000", 0.014))

    _reasons, warnings = read_divergence(read, breadth=0.82, expected_date="2026-08-03")

    assert not warnings


def test_a_stale_index_read_refuses_to_judge_divergence() -> None:
    """盘中取数会让宽基是实时的、等权是昨收的。两边不同日就不比。"""
    read = quotes(("上证指数", -0.02), ("沪深300", -0.03), ("科创50", -0.05))

    reasons, warnings = read_divergence(read, breadth=0.80, expected_date="2026-08-04")

    # 事实照报，判断不给。
    assert reasons
    assert any("两边不同日" in text for text in warnings)
    assert not any("等权与市值加权背离" in text for text in warnings)


def test_no_read_produces_nothing_rather_than_a_neutral_claim() -> None:
    assert read_divergence(None, breadth=0.8) == ([], [])
    assert read_divergence(BroadIndexRead(), breadth=0.8) == ([], [])


def test_unknown_equal_weight_date_refuses_to_judge() -> None:
    """日期未知走「不知道」，不走「不用查」。

    把未知当成可比，等于在调用方忘了传日期时**静默地**恢复成有 bug 的版本——
    那正是这条闸门要防的事。
    """
    read = quotes(("上证指数", -0.02), ("沪深300", -0.03), ("科创50", -0.06))

    reasons, warnings = read_divergence(read, breadth=0.80, expected_date=None)

    assert reasons
    assert any("数据日期未知" in text for text in warnings)
    assert not any("等权与市值加权背离" in text for text in warnings)


# 判定的单一真源


def test_verdict_and_warning_never_disagree() -> None:
    """措辞与台账标签必须出自同一个判据。

    两边各写一遍的话，日后调 `BREADTH_STRONG` 只会改到其中一处，于是简报说
    「背离」而台账记「一致」，事后统计再也对不上当天实际报出去的结论。
    """
    from ashare_decision_support.adapters.broad_index import (
        VERDICT_ALIGNED,
        VERDICT_DIVERGED,
        divergence_verdict,
    )

    diverged = quotes(("上证指数", -0.006), ("沪深300", -0.010), ("科创50", -0.051))
    aligned = quotes(("上证指数", 0.008), ("沪深300", 0.011), ("中证1000", 0.014))

    for read, breadth, expected in (
        (diverged, 0.76, VERDICT_DIVERGED),
        (aligned, 0.76, VERDICT_ALIGNED),
        # 宽度低时指数跟着跌是一致，不是背离。
        (diverged, 0.28, VERDICT_ALIGNED),
    ):
        verdict = divergence_verdict(read, breadth, "2026-08-03")
        _reasons, warnings = read_divergence(read, breadth, "2026-08-03")
        fired = any("等权与市值加权背离" in text for text in warnings)
        assert verdict == expected
        assert fired == (verdict == VERDICT_DIVERGED)


def test_verdict_separates_incomparable_from_unknown() -> None:
    """跨日不可比 ≠ 没有读数。合并会让「跑了但对不上」和「压根没跑」长得一样。"""
    from ashare_decision_support.adapters.broad_index import (
        VERDICT_INCOMPARABLE,
        VERDICT_UNKNOWN,
        divergence_verdict,
    )

    read = quotes(("上证指数", -0.02), ("沪深300", -0.03))

    assert divergence_verdict(read, 0.8, "2026-08-04") == VERDICT_INCOMPARABLE
    assert divergence_verdict(read, 0.8, None) == VERDICT_INCOMPARABLE
    assert divergence_verdict(None, 0.8, "2026-08-03") == VERDICT_UNKNOWN
    assert divergence_verdict(BroadIndexRead(), 0.8, "2026-08-03") == VERDICT_UNKNOWN
    # 有读数、日期也对得上，但宽度缺失 → 判不了，不是「一致」。
    assert divergence_verdict(read, None, "2026-08-03") == VERDICT_UNKNOWN
