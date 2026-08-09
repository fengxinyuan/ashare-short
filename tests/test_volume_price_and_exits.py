"""量价读数与卖出判据的测试。

这两个模块补的是「放量上涨 / 缩量下跌」此前没有判据的那一半，以及五类持仓卖点
之外的量价类与环境类离场理由。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ashare_decision_support.models.enums import EmotionCycle
from ashare_decision_support.signals.volume_price import (
    DIVERGENCE_TOP,
    DOUBLE_VOLUME,
    QUADRANT_DOWN_EXPAND,
    QUADRANT_UP_CONTRACT,
    QUADRANT_UP_EXPAND,
    TRIPLE_VOLUME,
    measure_volume_price,
)
from ashare_decision_support.strategy.exits import EXIT_EXIT, EXIT_TRIM, assess_exits
from ashare_decision_support.strategy.regime import classify_regime


def bars(
    count: int = 120,
    start: float = 20.0,
    drift: float = 0.1,
    volume: float = 100_000.0,
    volume_scale=None,
    unit: str = "share",
) -> list[dict]:
    """构造日线。`amount` 始终按 `volume(股) × close` 给，让单位反推能成立。

    `unit="hand"` 时 volume 按手报（除以 100），amount 不变——用来验证量纲反推。
    """
    rows: list[dict] = []
    price = start
    day = date(2026, 1, 5)
    for index in range(count):
        price = max(price + drift, 1.0)
        size = volume if volume_scale is None else volume * volume_scale(index)
        reported = size / 100.0 if unit == "hand" else size
        rows.append(
            {
                "timestamp": datetime.combine(day + timedelta(days=index), datetime.min.time()),
                "open": round(price - 0.1, 3),
                "high": round(price + 0.3, 3),
                "low": round(price - 0.4, 3),
                "close": round(price, 3),
                "volume": reported,
                "amount": size * price,
            }
        )
    return rows


# ---- 量纲 -----------------------------------------------------------------


def test_volume_unit_is_inferred_not_assumed() -> None:
    """量纲搞错不会报错，只会让换手率差 100 倍。两种单位必须给出同一个换手率。"""
    shares = 1_000_000_000.0
    as_shares = measure_volume_price(bars(unit="share"), float_shares=shares)
    as_hands = measure_volume_price(bars(unit="hand"), float_shares=shares)

    assert as_shares.turnover_rate is not None
    assert as_shares.turnover_rate == as_hands.turnover_rate


def test_unrecoverable_volume_unit_declines_instead_of_guessing() -> None:
    """推不出单位时**不给读数**，而不是拿原始数字硬算。"""
    rows = bars()
    for row in rows:
        row["amount"] = 0  # 反推失效
    result = measure_volume_price(rows)

    assert result.vol_ratio_20d is None
    assert result.turnover_rate is None
    assert any("无法从" in note for note in result.notes)


def test_turnover_rate_needs_float_shares_and_says_so() -> None:
    without = measure_volume_price(bars())
    with_shares = measure_volume_price(bars(), float_shares=1_000_000_000.0)

    assert without.turnover_rate is None
    assert with_shares.turnover_rate is not None
    # 口径必须写进 notes，不能只留在文档里——读简报的人看到的是数字。
    assert any("流通股本" in note for note in with_shares.notes)


# ---- 四象限 ---------------------------------------------------------------


def test_quadrant_separates_price_direction_from_volume_level() -> None:
    up_expand = measure_volume_price(
        bars(volume_scale=lambda i: 3.0 if i == 119 else 1.0)
    )
    assert up_expand.quadrant == QUADRANT_UP_EXPAND

    up_contract = measure_volume_price(
        bars(volume_scale=lambda i: 0.4 if i == 119 else 1.0)
    )
    assert up_contract.quadrant == QUADRANT_UP_CONTRACT

    down_expand = measure_volume_price(
        # 跌幅要够大才落进「下跌」象限：`_quadrant` 有 ±0.2% 的中性带，日跌
        # 0.1% 会被读成横盘。start 也要高，`bars` 有 max(price, 1.0) 的地板，
        # 跌穿之后价格恒定、ret_1d 变 0。两者都是构造数据的坑，不是被测逻辑。
        bars(start=200.0, drift=-1.0, volume_scale=lambda i: 3.0 if i == 119 else 1.0)
    )
    assert down_expand.quadrant == QUADRANT_DOWN_EXPAND


# ---- 量能趋势 -------------------------------------------------------------


def test_sustained_expansion_is_distinguished_from_a_single_spike() -> None:
    """「连续三天放量」和「今天突然放一根」在单日量比上长得一模一样。

    最小二乘斜率答不了这个问题：[1,1,1,1,3] 的斜率和 [1.4,1.8,2.2,2.6,3.0] 同号。
    这条断言就是为了钉住那个区分——第一版实现正是栽在这里。
    """
    spike = measure_volume_price(
        bars(volume_scale=lambda i: 3.0 if i == 119 else 1.0)
    )
    sustained = measure_volume_price(
        bars(volume_scale=lambda i: 1.0 + 0.4 * max(0, i - 114))
    )

    # 两者当日量比都明显 >1.2，单看它无法区分。
    assert spike.vol_ratio_20d and spike.vol_ratio_20d > 1.2
    assert sustained.vol_ratio_20d and sustained.vol_ratio_20d > 1.2
    # 斜率两者同号——所以它不能用来做这个判断。
    assert (spike.vol_trend_5d or 0) > 0 and (sustained.vol_trend_5d or 0) > 0
    # expanding 必须分开它们。
    assert sustained.expanding is True
    assert spike.expanding is False


# ---- 日环比量能 -----------------------------------------------------------


def test_daily_ratio_and_20d_ratio_disagree_on_what_counts_as_doubling() -> None:
    """**倍量是日环比，不是对均量的比值。** 这条钉住的就是这个区分。

    连续温和放量到第 5 天：对 20 日均量已经放到 2.5 倍，看着像倍量——但环比只有
    1.15，根本不是。反过来单日脉冲两个口径都是 3.0。拿 `vol_ratio_20d` 当倍量用，
    会把「稳步放量的中继」误报成「筹码大交换的倍量阴」，而这两者在 §3.3 那条
    形态里是相反的信号。
    """
    sustained = measure_volume_price(bars(volume_scale=lambda i: 1.0 + 0.4 * max(0, i - 114)))
    spike = measure_volume_price(bars(volume_scale=lambda i: 3.0 if i == 119 else 1.0))

    assert sustained.vol_ratio_20d == 2.5
    assert sustained.vol_ratio_1d == 1.1538
    assert sustained.vol_ratio_1d < DOUBLE_VOLUME

    assert spike.vol_ratio_20d == 3.0
    assert spike.vol_ratio_1d == 3.0
    assert spike.vol_ratio_1d >= TRIPLE_VOLUME


def test_doubling_is_named_in_the_readout() -> None:
    """简报读的是 describe()，倍量这件事必须在那里出现，不能只活在字段里。"""
    doubled = measure_volume_price(bars(volume_scale=lambda i: 2.2 if i == 119 else 1.0))
    tripled = measure_volume_price(bars(volume_scale=lambda i: 3.4 if i == 119 else 1.0))
    flat = measure_volume_price(bars())

    assert any("倍量：" in line for line in doubled.describe())
    assert any("三倍量：" in line for line in tripled.describe())
    assert not any("倍量" in line for line in flat.describe())


def test_daily_ratio_needs_a_previous_bar() -> None:
    result = measure_volume_price(bars(count=1))
    assert result.vol_ratio_1d is None


# ---- 方向性成交量 ---------------------------------------------------------


def test_up_down_volume_ratio_is_none_when_one_side_is_empty() -> None:
    """20 天没有一根阴线是该被看见的事实，压成一个大数字会让它混进普通读数。"""
    result = measure_volume_price(bars(drift=0.1))
    assert result.up_down_vol_ratio is None


def test_up_down_volume_ratio_detects_a_sold_off_pullback() -> None:
    """今天缩量、但整段是放着量砸下来的——单日量比看不到这件事。"""
    rows = bars(count=140)
    # 后 20 根里下跌日放量、上涨日缩量。
    for index in range(120, 140):
        falling = index % 2 == 0
        rows[index]["close"] = rows[index - 1]["close"] * (0.97 if falling else 1.01)
        size = 300_000.0 if falling else 60_000.0
        rows[index]["volume"] = size
        rows[index]["amount"] = size * rows[index]["close"]

    result = measure_volume_price(rows)
    assert result.up_down_vol_ratio is not None
    assert result.up_down_vol_ratio < 1.0


# ---- 卖出判据 -------------------------------------------------------------


class _VP:
    """只带卖出判据关心的那几个字段的替身。"""

    def __init__(self, **kwargs):
        self.climax_stall = kwargs.get("climax_stall", False)
        self.breakdown_on_volume = kwargs.get("breakdown_on_volume", False)
        self.divergence = kwargs.get("divergence")
        self.vol_ratio_20d = kwargs.get("vol_ratio_20d")


def test_no_readings_means_no_exit_signal() -> None:
    """「没测到」不该变成一条卖出理由。"""
    assert assess_exits().signals == []
    assert assess_exits(volume_price=_VP()).signals == []


def test_volume_breakdown_is_an_exit_not_a_trim() -> None:
    """放量跌破 MA20 是「缩量回踩不破 MA20」的镜像，最该无条件走。"""
    result = assess_exits(volume_price=_VP(breakdown_on_volume=True))
    assert result.strongest == EXIT_EXIT
    assert any("MA20" in item.reason for item in result.signals)


def test_climax_stall_is_a_trim() -> None:
    result = assess_exits(volume_price=_VP(climax_stall=True, vol_ratio_20d=3.4))
    assert result.strongest == EXIT_TRIM
    assert any("天量滞涨" in item.reason for item in result.signals)


def test_top_divergence_does_not_apply_without_a_gain() -> None:
    """顶背离是**兑现**判据。对还在亏损的仓位说「减仓落袋」没有意义。"""
    with_gain = assess_exits(
        volume_price=_VP(divergence=DIVERGENCE_TOP), has_unrealised_gain=True
    )
    without_gain = assess_exits(
        volume_price=_VP(divergence=DIVERGENCE_TOP), has_unrealised_gain=False
    )

    assert with_gain.strongest == EXIT_TRIM
    assert without_gain.signals == []


def test_regime_downgrade_speaks_to_existing_positions() -> None:
    """「今天行情很差」必须能直接对持仓说话，而不是只收紧选股。"""
    ebb = classify_regime(blowup_rate=0.55, max_streak=3, limit_up_count=30, limit_down_count=2)
    assert ebb.cycle is EmotionCycle.EBB

    result = assess_exits(regime=ebb)
    assert result.strongest == EXIT_TRIM
    signal = next(item for item in result.signals if item.trigger == "regime_derisk")
    # 标签是结论，读数才能被复核——判定依据必须一起带出来。
    assert any("炸板率" in text for text in signal.detail)
    assert any("降仓提示，不是清仓信号" in text for text in signal.detail)


def test_freezing_regime_escalates_to_exit() -> None:
    frozen = classify_regime(
        blowup_rate=0.9, max_streak=1, limit_up_count=3, limit_down_count=40
    )
    assert frozen.cycle is EmotionCycle.FREEZING
    assert assess_exits(regime=frozen).strongest == EXIT_EXIT


def test_healthy_regime_produces_no_environment_exit() -> None:
    healthy = classify_regime(
        blowup_rate=0.15, max_streak=5, limit_up_count=60, limit_down_count=1
    )
    assert assess_exits(regime=healthy).signals == []
