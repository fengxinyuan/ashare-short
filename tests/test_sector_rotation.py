"""板块轮动（价格序列口径）。

这一层最容易变成「看起来在测量、其实在假设」的东西，所以测试守的主要是三条
纪律，而不是具体数值：

1. **切换方向由数据说，不预设。** 同一份代码在两种相反的行情上必须给出相反的
   结论，否则它测的是自己的常量而不是市场。
2. **小样本不给结论。** 横截面秩相关在 20 个板块上随便换两个位置就能翻号。
3. **区间必须对齐。** 停更三天的板块，它的「5 日涨幅」跨的是别人 8 天的区间。
"""

from __future__ import annotations

from ashare_decision_support.sectors.rotation import (
    LABEL_MOMENTUM,
    LABEL_SWITCH,
    LABEL_UNCLEAR,
    MIN_SECTORS,
    _spearman,
    read_rotation,
)

DAYS = 40


def bars(steps: list[float], start: str = "2026-06-01") -> list[dict]:
    """按日收益序列造日线。日期只要求单调且可比，用序号即可。"""
    close = 100.0
    rows: list[dict] = []
    year, month, day = (int(part) for part in start.split("-"))
    for index, step in enumerate(steps):
        close *= 1.0 + step
        rows.append(
            {
                "date": f"{year}{month:02d}{day + index // 28:02d}{index:02d}"[:12],
                "open": close,
                "high": close,
                "low": close,
                "close": round(close, 4),
                "volume": 1000.0,
                "amount": 1000.0 * close,
            }
        )
    return rows


def series_from(profiles: dict[str, list[float]]) -> dict[str, tuple[str, list[dict]]]:
    return {
        f"88{index:04d}": (name, bars(steps))
        for index, (name, steps) in enumerate(profiles.items(), start=1101)
    }


def switching_market(n: int = 40) -> dict[str, list[float]]:
    """前 20 日强的后 5 日弱，反之亦然——高低切换的教科书形态。"""
    profiles: dict[str, list[float]] = {}
    for i in range(n):
        # 一半在前段猛涨、末段回落；另一半正相反。
        early = 0.010 if i < n // 2 else -0.008
        late = -0.012 if i < n // 2 else 0.011
        profiles[f"板块{i:02d}"] = [early] * (DAYS - 5) + [late] * 5
    return profiles


def trending_market(n: int = 40) -> dict[str, list[float]]:
    """前段强的后段继续强——强者恒强。"""
    profiles: dict[str, list[float]] = {}
    for i in range(n):
        rate = 0.010 if i < n // 2 else -0.008
        profiles[f"板块{i:02d}"] = [rate] * DAYS
    return profiles


# 高低切换


def test_switching_and_momentum_produce_opposite_labels() -> None:
    """**同一份代码在两种相反行情上必须给出相反结论。**

    只测一种行情的话，一个恒定返回「高低切换」的实现也能通过——而那正是这一层
    最该避免的东西：把一句每天都有人说的判断硬编码成结论。
    """
    switch = read_rotation(series_from(switching_market()))
    trend = read_rotation(series_from(trending_market()))

    assert switch.trusted and trend.trusted
    assert switch.switch_label == LABEL_SWITCH
    assert trend.switch_label == LABEL_MOMENTUM
    assert switch.switch_correlation is not None
    assert trend.switch_correlation is not None
    assert switch.switch_correlation < 0 < trend.switch_correlation


def test_month_can_trend_while_the_week_switches() -> None:
    """长窗口与等长窗口必须能给出**相反**的方向，因为它们问的不是同一件事。

    造的是 2026-08-03 半导体那个真实形状：整月弱（前 15 日大跌），但中间那一周
    反弹过（`t-10→t-5` 排到前列），最后一周又崩掉。此时——

    - 长窗口（前 15 日 vs 近 5 日）：跌完接着跌 → **弱者恒弱**，相关为正；
    - 等长窗口（相邻两个 5 日）：上周最强的这周垫底 → **高低切换**，相关为负。

    只有一个相关系数的话，这一天只能二选一地报出去，另一半信息直接丢掉——
    而丢掉的恰好是超短最关心的那半。
    """
    n = 40
    profiles: dict[str, list[float]] = {}
    for i in range(n):
        weak_overall = i < n // 2
        base = [0.0] * 20
        # 20..29：决定整月方向；30..34：中间那一周的反向脉冲；35..39：最近一周。
        mid = [-0.010 if weak_overall else 0.010] * 10
        bounce = [0.008 if weak_overall else -0.008] * 5
        recent = [-0.012 if weak_overall else 0.012] * 5
        profiles[f"板块{i:02d}"] = base + mid + bounce + recent

    read = read_rotation(series_from(profiles))

    assert read.trusted
    assert read.switch_correlation is not None
    assert read.adjacent_correlation is not None
    # 一正一负，且各自越过了 ±0.2 的判定带。
    assert read.switch_label == LABEL_MOMENTUM
    assert read.adjacent_label == LABEL_SWITCH
    assert read.adjacent_correlation < 0 < read.switch_correlation
    # 分歧必须写进简报，否则读的人看不到「按哪个窗口做决定」。
    assert any("两个窗口给出的方向不同" in line for line in read.observations)


def test_adjacent_correlation_matches_the_rank_change_windows() -> None:
    """等长窗口的相关系数必须与 `rank_change` 同口径——它就是给那张榜做总结的。

    切换行情里名次应当整体对调：排名上升最快的那批，前一个 5 日必然排在后半段。
    这条一旦不成立，说明两者用的不是同一对窗口，简报里那句「与排名变化同口径」
    就是假的。
    """
    read = read_rotation(series_from(switching_market()))

    assert read.adjacent_label == LABEL_SWITCH
    rising = read.rising(5)
    assert rising
    assert all(item.rank_prev > read.sectors // 2 for item in rising)
    assert all(item.rank_now <= read.sectors // 2 for item in rising)


def test_a_directionless_tape_refuses_to_name_a_rotation() -> None:
    """相关系数落在中间带时不给方向。给一个标签会让「测不出关系」
    读起来像「没有轮动」，而那是两件事。

    用 `(i * 17) % 40` 这个乘法置换而不是随机数：它把后段名次相对前段名次
    彻底打散（实测秩相关 -0.02），同时**完全确定**——测试不该有种子。
    """
    n = 40
    profiles = {
        f"板块{i:02d}": [(-0.010 + 0.0005 * i)] * (DAYS - 5)
        + [(-0.010 + 0.0005 * ((i * 17) % n))] * 5
        for i in range(n)
    }
    read = read_rotation(series_from(profiles))

    assert read.trusted
    assert read.switch_correlation is not None
    assert abs(read.switch_correlation) < 0.2
    assert read.switch_label == LABEL_UNCLEAR
    assert any("说不出轮动方向" in line for line in read.observations)


def test_the_prior_window_does_not_overlap_the_recent_one() -> None:
    """**这是一个不会报错、只会算错的坑，所以单独钉一条。**

    20 日窗口包含最近 5 日。拿它当「前期涨幅」，本周暴涨的板块会自动显得
    「前期已经涨高了」，相关系数被机械地推向正号——也就是**系统性地把高低切换
    误判成强者恒强**。

    这份数据前后两段完全无关（乘法置换，秩相关 -0.02）。用重叠窗口算会得到
    +0.27 并报「强者恒强」；用不重叠窗口才会正确地报「无方向」。
    """
    n = 40
    profiles = {
        f"板块{i:02d}": [(-0.010 + 0.0005 * i)] * (DAYS - 5)
        + [(-0.010 + 0.0005 * ((i * 17) % n))] * 5
        for i in range(n)
    }
    read = read_rotation(series_from(profiles))
    sample = read.trends[0]

    # 前期区间独立存在，且确实不等于 20 日读数。
    assert sample.ret_prior is not None
    assert sample.ret_20d is not None
    assert sample.ret_prior != sample.ret_20d

    overlapping = _spearman(
        [item.excess_20d for item in read.trends],
        [item.excess_5d for item in read.trends],
    )
    assert overlapping is not None and overlapping > 0.2  # 重叠窗口的假信号
    assert read.switch_correlation is not None
    assert abs(read.switch_correlation) < 0.2  # 不重叠窗口的真读数
    assert read.switch_label == LABEL_UNCLEAR


def test_a_thin_cross_section_gives_no_verdict_at_all() -> None:
    """秩相关在 20 个板块上随便换两个位置就能翻号。"""
    read = read_rotation(series_from(switching_market(n=MIN_SECTORS - 5)))

    assert read.trusted is False
    assert read.switch_correlation is None
    assert any("不足以支撑轮动判断" in line for line in read.observations)


# 相对强弱排名


def test_rank_change_surfaces_where_money_is_moving() -> None:
    """排在第 3 名说明它已经强了；从第 40 名升到第 12 名说明资金正在往这里挪。
    后者才是「轮动到哪了」。"""
    profiles = switching_market()
    # 塞一个「一直垫底、最后 5 天最猛」的板块，它应该是排名上升最快的那个。
    profiles["后来居上"] = [-0.015] * (DAYS - 5) + [0.030] * 5
    read = read_rotation(series_from(profiles))

    rising = read.rising(3)
    assert rising
    assert rising[0].name == "后来居上"
    assert rising[0].rank_change is not None and rising[0].rank_change > 0
    assert rising[0].rank_prev is not None and rising[0].rank_now is not None
    assert rising[0].rank_now < rising[0].rank_prev


def test_excess_return_is_measured_against_the_cross_section() -> None:
    """绝对涨幅答不了「跑赢没有」——普涨日里 +2% 是落后。"""
    profiles = {f"板块{i:02d}": [0.010] * DAYS for i in range(40)}
    profiles["落后者"] = [0.002] * DAYS
    read = read_rotation(series_from(profiles))

    laggard = next(item for item in read.trends if item.name == "落后者")
    assert laggard.ret_5d is not None and laggard.ret_5d > 0
    assert laggard.excess_5d is not None and laggard.excess_5d < 0


# 对齐与降级


def test_a_stale_sector_is_excluded_rather_than_compared() -> None:
    """停更的板块，它的「5 日涨幅」跨的是别人 8 天的区间。"""
    series = series_from(switching_market())
    stale_code = min(series)
    name, rows = series[stale_code]
    series[stale_code] = (name, rows[:-3])

    read = read_rotation(series)

    assert stale_code not in {item.code for item in read.trends}
    assert any("最新交易日不是" in note for note in read.notes)


def test_a_short_series_is_dropped_before_it_can_skew_the_ranking() -> None:
    series = series_from(switching_market())
    short_code = sorted(series)[1]
    name, rows = series[short_code]
    series[short_code] = (name, rows[-6:])

    read = read_rotation(series)

    assert short_code not in {item.code for item in read.trends}


def test_no_series_at_all_is_a_stated_absence() -> None:
    read = read_rotation({})

    assert read.ok is False
    assert read.trends == []
    assert any("没有可用的板块指数序列" in line for line in read.observations)


# 主线持续度


def test_persistence_separates_a_clear_leadership_from_churn() -> None:
    """恒为 0 的指标区分不出「主线明确」和「完全无序」——两种盘面它都报 0，
    所以用相邻两日重合度的均值，而不是全窗口交集。"""
    stable = {f"板块{i:02d}": [0.02 if i < 5 else 0.001] * DAYS for i in range(40)}
    steady = read_rotation(series_from(stable))

    assert steady.persistence is not None
    assert steady.persistence > 0.9
    assert steady.recurring_leaders
    assert any("主线明确" in line for line in steady.observations)


def test_the_two_windows_are_reconciled_in_the_wording() -> None:
    """切换标签与排名变化用的是**不同的对比窗口**，可以同时成立而看起来矛盾。

    实测 2026-08-03：半导体前期 -22.8% 之后再跌 -12.8%（弱者恒弱，支持 +0.45 的
    相关系数），但它在更早那个 5 日窗口反弹到第 6 名，于是排名读数是 6→89。
    不说明窗口的话，读的人只能在两个都对的读数里二选一地相信一个。
    """
    read = read_rotation(series_from(trending_market()))

    assert read.switch_label == LABEL_MOMENTUM
    text = "".join(read.observations)
    assert "对比窗口不同" in text
    assert "不一致是常态" in text


def test_observations_never_predict_tomorrow() -> None:
    """本模块的全部输出都是对已发生的事的陈述句。"""
    read = read_rotation(series_from(switching_market()))

    text = "".join(read.observations)
    for forbidden in ("明天", "预计", "将会", "有望", "建议"):
        assert forbidden not in text
