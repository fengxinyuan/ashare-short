"""限价单口径的单测。

这一层比适配性筛选更容易写出「好看但是假」的实现，所以测试的重点全在**正确性
陷阱**上：ATR 不能用未来数据、跳空低开要按更优价成交、未成交不能记 0 收益、网格
最优必须经过样本外与邻域两道检验。

其中 `test_rolling_atr_never_sees_the_future` 是最关键的一条——它坏掉的时候，其余
测试全都照样通过，而回测收益会凭空高出一截。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ashare_decision_support.screening.limit_order import (
    DEFAULT_ENTRY_GRID,
    DEFAULT_TARGET_GRID,
    LimitRule,
    _rolling_atr,
    scan_limit_rules,
    simulate,
)
from ashare_decision_support.screening.suitability import ScreenConfig, _normalise


def build_bars(
    count: int = 400,
    *,
    dip_every: int = 4,
    dip_depth: float = 0.03,
    rebound: float = 0.025,
    start_price: float = 20.0,
    amount: float | None = 4e8,
) -> list[dict]:
    """一条「每 N 天挖一个坑、次日填回来」的日线。

    坑日：盘中最低跌 `dip_depth`，收盘收在低位；次日反弹 `rebound`。这是低吸规则
    **应该**能抓到的形态——如果模拟器在这样的序列上都赚不到钱，那是实现错了，
    不是市场无效。
    """
    start = datetime(2025, 1, 2)
    bars: list[dict] = []
    price = start_price
    prev_close = price
    for idx in range(count):
        is_dip = idx % dip_every == 0
        if is_dip:
            low = price * (1 - dip_depth)
            high = price * 1.002
            close = low * 1.004
        else:
            low = price * 0.997
            high = price * (1 + rebound)
            close = high * 0.996
        open_ = (high + low) / 2
        bars.append(
            {
                "timestamp": start + timedelta(days=idx),
                "open": round(open_, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "prev_close": round(prev_close, 4),
                "volume": 4_000_000.0,
                "amount": amount,
            }
        )
        prev_close = close
    return bars


def oscillator_bars(count: int = 400, *, span: float = 0.004) -> list[dict]:
    """完全周期的振荡：每天最低最高都落在同一对价位上。

    这**不是**「没有机会」的序列——恰恰相反，它是可想象的最好赚的序列，只要挂单价
    正好卡在那个低点上就能天天吃满。用它来测网格的孤峰检查：只有一个参数点能对准
    那对价位，邻域全是负的，正确的判定是 `unstable` 而不是 `pass`。
    """
    start = datetime(2025, 1, 2)
    bars: list[dict] = []
    price, prev_close = 20.0, 20.0
    for idx in range(count):
        low, high = price * (1 - span / 2), price * (1 + span / 2)
        mid = low + (high - low) * 0.5
        bars.append(
            {
                "timestamp": start + timedelta(days=idx),
                "open": round(mid, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(mid, 4),
                "prev_close": round(prev_close, 4),
                "volume": 4_000_000.0,
                "amount": 4e8,
            }
        )
        prev_close = mid
    return bars


def dead_bars(count: int = 400) -> list[dict]:
    """日内振幅小于往返成本的日线：再完美的执行也赚不回手续费。

    往返成本 0.202%，这里全天振幅只有 0.05%——上界摆在那里，任何参数都不可能正。
    """
    return oscillator_bars(count, span=0.0005)


# ---------------------------------------------------------------------------
# 未来函数
# ---------------------------------------------------------------------------


def test_rolling_atr_never_sees_the_future() -> None:
    """`atr[i]` 只能由 `rows[:i]` 决定。

    验证手法是**改掉未来再重算**：把 i 之后的所有 K 线换成完全不同的形状，`atr[i]`
    必须一个字节都不变。这条守住了，挂单价就不可能含未来信息。
    """
    rows = _normalise(build_bars(count=120))
    baseline = _rolling_atr(rows, 20)

    tampered = [dict(row) for row in rows]
    for row in tampered[60:]:
        row["high"] = row["high"] * 5
        row["low"] = row["low"] / 5
    after = _rolling_atr(tampered, 20)

    assert baseline[:61] == after[:61]
    assert baseline[70] != after[70]  # 反证：改了过去确实会变，说明测试本身有效


def test_atr_is_none_before_warmup() -> None:
    rows = _normalise(build_bars(count=120))
    atrs = _rolling_atr(rows, 20)

    assert atrs[0] is None
    assert atrs[5] is None
    assert atrs[30] is not None


# ---------------------------------------------------------------------------
# 成交语义
# ---------------------------------------------------------------------------


def test_deeper_limit_means_lower_fill_rate() -> None:
    """挂得越深成交越少。这条不成立说明成交判定写反了。"""
    rows = _normalise(build_bars())
    cfg = ScreenConfig()

    shallow = simulate("600519.SH", rows, LimitRule(0.0, 1.0), cfg)
    deep = simulate("600519.SH", rows, LimitRule(1.5, 1.0), cfg)

    assert shallow.fill_rate > deep.fill_rate
    assert shallow.attempts == deep.attempts  # 挂单次数一样，只是成交与否不同


def test_gap_down_fills_at_the_better_open_price() -> None:
    """跳空低开时限价买单成交在开盘价，不是挂单价。"""
    rows = _normalise(build_bars(count=200))
    for row in rows[100:]:
        row["open"] = row["low"]  # 一开盘就是全天最低

    cfg = ScreenConfig()
    result = simulate("600519.SH", rows, LimitRule(0.5, 1.0), cfg)

    assert result.fills > 0
    # 成交价被压低，收益必然优于按挂单价成交的版本
    baseline = simulate("600519.SH", _normalise(build_bars(count=200)), LimitRule(0.5, 1.0), cfg)
    assert result.median > baseline.median


def test_unfilled_attempts_are_not_counted_as_zero() -> None:
    """未成交只减少 `fills`，不能进收益样本。"""
    rows = _normalise(build_bars())
    result = simulate("600519.SH", rows, LimitRule(1.5, 1.0), ScreenConfig())

    assert result.attempts > result.fills
    assert result.per_attempt is not None
    # per_attempt 被成交率打了折，必然小于成交样本自身的中位数
    assert abs(result.per_attempt) < abs(result.median)


def test_suspended_days_produce_no_attempt() -> None:
    rows = _normalise(build_bars(count=200))
    baseline = simulate("600519.SH", rows, LimitRule(0.5, 1.0), ScreenConfig())

    for row in rows[100:110]:
        row["volume"] = 0
    holed = simulate("600519.SH", rows, LimitRule(0.5, 1.0), ScreenConfig())

    assert holed.attempts < baseline.attempts


def test_cost_is_deducted_from_every_trade() -> None:
    rows = _normalise(build_bars())
    from ashare_decision_support.screening.suitability import CostModel

    free = simulate("600519.SH", rows, LimitRule(0.5, 1.0), ScreenConfig(cost=CostModel(0.0)))
    dear = simulate("600519.SH", rows, LimitRule(0.5, 1.0), ScreenConfig(cost=CostModel(0.01)))

    assert dear.median < free.median


def test_target_hit_is_reported_separately_from_close_exit() -> None:
    """止盈率必须单独报——收盘平仓的那部分才是真实的成本来源。"""
    rows = _normalise(build_bars())

    easy = simulate("600519.SH", rows, LimitRule(0.5, 0.5), ScreenConfig())
    hard = simulate("600519.SH", rows, LimitRule(0.5, 2.0), ScreenConfig())

    assert easy.hit_rate > hard.hit_rate
    assert 0.0 <= easy.hit_rate <= 1.0


# ---------------------------------------------------------------------------
# 网格与过拟合护栏
# ---------------------------------------------------------------------------


def test_scan_covers_the_whole_grid() -> None:
    scan = scan_limit_rules("600519.SH", build_bars())

    assert len(scan.grid) == len(DEFAULT_ENTRY_GRID) * len(DEFAULT_TARGET_GRID)


def test_scan_finds_the_rule_on_a_series_built_for_it() -> None:
    """构造出来就该被抓到的形态，必须真的被抓到——否则模拟器是坏的。"""
    scan = scan_limit_rules("600519.SH", build_bars())

    assert scan.best is not None
    assert scan.best.per_attempt > 0
    assert scan.verdict in {"pass", "unstable"}


def test_range_below_cost_yields_no_profitable_rule() -> None:
    """振幅覆盖不了成本时，网格里不该有任何正期望的参数。"""
    scan = scan_limit_rules("600519.SH", dead_bars())

    assert scan.verdict == "reject"
    assert any("覆盖不了成本" in note for note in scan.notes)


def test_regime_change_is_caught_by_holdout() -> None:
    """样本内有规律、样本外规律消失 → `unstable`，不能报 `pass`。"""
    bars = build_bars(count=280) + dead_bars(count=120)
    scan = scan_limit_rules("600519.SH", bars)

    assert scan.verdict == "unstable"
    assert scan.decay is None or scan.decay < 0.5


def test_holdout_uses_the_in_sample_best_rule_not_a_refit() -> None:
    """样本外必须用样本内选出的那组参数重跑，不能在样本外重新挑一遍。"""
    scan = scan_limit_rules("600519.SH", build_bars())

    assert scan.holdout is not None
    assert scan.holdout.rule == scan.best.rule


def test_neighbourhood_is_measured_around_the_best_point() -> None:
    scan = scan_limit_rules("600519.SH", build_bars())

    assert scan.neighbourhood is not None


def test_isolated_grid_peak_is_downgraded_to_unstable() -> None:
    """孤峰判 `unstable`：一个参数点亮眼、邻域全负，形状像噪声而不像规律。

    周期振荡序列上只有一个挂单深度能正好卡在那对固定价位上，是这个检查最干净的
    反例——它的样本内收益是真的（+0.198% × 100% 成交率），照样不该报 `pass`。
    """
    scan = scan_limit_rules("600519.SH", oscillator_bars())

    assert scan.best is not None
    assert scan.best.per_attempt > 0  # 样本内确实赚钱
    assert scan.best.fill_rate == 1.0
    assert scan.neighbourhood is not None and scan.neighbourhood <= 0
    assert scan.verdict == "unstable"
    assert any("邻域" in note for note in scan.notes)


def test_sparse_fills_are_excluded_from_best_selection() -> None:
    """成交次数太少的参数点不能当选，哪怕它的收益最高。"""
    scan = scan_limit_rules(
        "600519.SH",
        build_bars(),
        entry_grid=(0.25, 8.0),  # 8 个 ATR 基本永不成交
        target_grid=DEFAULT_TARGET_GRID,
    )

    assert scan.best is not None
    assert scan.best.rule.entry_atr == 0.25
    assert scan.best.fills >= 30


def test_short_history_is_reported_not_crashed() -> None:
    scan = scan_limit_rules("600519.SH", build_bars(count=50))

    assert scan.best is None
    assert scan.verdict == "reject"
    assert any("不足以扫参数" in note for note in scan.notes)


# ---------------------------------------------------------------------------
# 接线
# ---------------------------------------------------------------------------


def test_run_screen_attaches_limit_scans_for_tradable_names() -> None:
    from datetime import date

    from ashare_decision_support.screening.runner import run_screen

    class _Stub:
        def get_historical_bars(self, symbols, start, end):
            return {
                "600519.SH": build_bars(),
                "000001.SZ": build_bars(amount=1e6),  # 流动性不达标，死在 L0
            }

    report = run_screen(
        _Stub(), ["600519.SH", "000001.SZ"], end=date(2026, 8, 2), limit_scan=True
    )

    # L0 就被砍掉的标的不进限价扫描——它买不进卖不出，扫参数没有意义
    assert [scan.symbol for scan in report.limit_scans] == ["600519.SH"]
    assert any("邻域" in note or "虚高" in note for note in report.notes)
    assert "限价单扫描" in "\n".join(report.describe())


def test_limit_scan_is_off_by_default() -> None:
    from datetime import date

    from ashare_decision_support.screening.runner import run_screen

    class _Stub:
        def get_historical_bars(self, symbols, start, end):
            return {"600519.SH": build_bars()}

    report = run_screen(_Stub(), ["600519.SH"], end=date(2026, 8, 2))

    assert report.limit_scans == []
