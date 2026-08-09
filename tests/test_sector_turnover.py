"""板块成交额迁移。

这一层最容易出的错不是算错，是**读反**：成交额占比上升被当成「资金流入」，
而放量下跌同样会让占比上升。所以测试守的第一条就是这个区分。
"""

from __future__ import annotations

from ashare_decision_support.sectors.turnover import (
    MIN_SECTORS,
    QUADRANT_DISTRIBUTION,
    QUADRANT_FLAT,
    QUADRANT_INFLOW,
    QUADRANT_NEGLECT,
    SHARE_MOVE,
    read_turnover,
)

NEED = 20


def bars(
    amounts: list[float], closes: list[float], start_day: int = 1
) -> list[dict]:
    return [
        {
            "date": f"202606{start_day + index:02d}",
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": amount / max(close, 1e-9),
            "amount": amount,
        }
        for index, (amount, close) in enumerate(zip(amounts, closes, strict=True))
    ]


def series_from(
    profiles: dict[str, tuple[list[float], list[float]]],
) -> dict[str, tuple[str, list[dict]]]:
    return {
        f"88{index:04d}": (name, bars(amounts, closes))
        for index, (name, (amounts, closes)) in enumerate(profiles.items(), start=1101)
    }


def flat(value: float, n: int = NEED) -> list[float]:
    return [value] * n


def ramp(prior: float, recent: float, n: int = NEED, recent_n: int = 5) -> list[float]:
    return [prior] * (n - recent_n) + [recent] * recent_n


def baseline(n: int = MIN_SECTORS) -> dict[str, tuple[list[float], list[float]]]:
    """一批不动的陪衬板块，用来把样本量顶过 MIN_SECTORS 并稳住占比分母。"""
    return {f"陪衬{i:02d}": (flat(1e8), flat(100.0)) for i in range(n)}


# ---- 读反的那一格 ---------------------------------------------------------


def test_rising_share_on_falling_price_is_not_called_inflow() -> None:
    """**占比上升 + 价格下跌 = 有人在跑，不是资金进场。**

    这是整层唯一会被读反的一格。只按占比排序的实现会把它和真正的进场混在同一
    张榜上，而两者对超短的含义正好相反。
    """
    profiles = baseline()
    # 成交额翻三倍，价格一路跌——夺路而逃的形态。
    profiles["出货板块"] = (ramp(1e8, 3e8), [100.0] * 15 + [98, 96, 94, 92, 90])
    # 成交额翻三倍，价格一路涨——真的有人在买。
    profiles["进场板块"] = (ramp(1e8, 3e8), [100.0] * 15 + [102, 104, 106, 108, 110])

    read = read_turnover(series_from(profiles))

    assert read.trusted
    picked = {item.name: item for item in read.entries}
    assert picked["出货板块"].quadrant == QUADRANT_DISTRIBUTION
    assert picked["进场板块"].quadrant == QUADRANT_INFLOW
    # 两者占比变化同号，仅靠占比无法区分——这正是必须配价格方向的理由。
    assert picked["出货板块"].share_change > 0
    assert picked["进场板块"].share_change > 0
    assert any("有人在跑" in line for line in read.observations)


def test_shrinking_share_with_falling_price_reads_as_neglected() -> None:
    profiles = baseline()
    profiles["没人看"] = (ramp(3e8, 1e8), [100.0] * 15 + [99, 98, 97, 96, 95])

    read = read_turnover(series_from(profiles))

    picked = {item.name: item for item in read.entries}
    assert picked["没人看"].quadrant == QUADRANT_NEGLECT


# ---- 占比抹掉的那个量 -----------------------------------------------------


def test_total_turnover_is_reported_separately_from_share() -> None:
    """占比刻意把全市场水位除掉，所以水位必须单独报回来。

    存量博弈下的高低切换和增量入场下的高低切换，对仓位的含义相反；只看占比的话
    这两种盘面长得一模一样。
    """
    profiles = {f"陪衬{i:02d}": (ramp(2e8, 1e8), flat(100.0)) for i in range(MIN_SECTORS)}

    read = read_turnover(series_from(profiles))

    assert read.trusted
    assert read.total_change is not None
    assert read.total_change < -0.4
    assert any("总量在收缩" in line for line in read.observations)
    # 每个板块都同步腰斩，占比因此**一点没动**——占比单独看会说「什么也没发生」。
    assert all(abs(item.share_change) < 1e-6 for item in read.entries)


def test_absolute_amount_surfaces_what_share_hides() -> None:
    """总量收缩时，成交额腰斩的板块占比可能几乎不动，在占比榜上一行都不出现。

    实测 2026-08-03 的半导体：日均少 887 亿（全场最大），占比只动 -4.8%，
    落在中性带里被归为「基本没动」——而它是当天全场失血最多的板块。

    这里把那个形状造出来：陪衬集体腰斩，目标板块按同样的比例失血，于是**占比
    几乎不变**（中性带内），绝对额却是全场最大的一笔。
    """
    profiles = {f"陪衬{i:02d}": (ramp(10e8, 5e8), flat(100.0)) for i in range(MIN_SECTORS)}
    profiles["失血板块"] = (ramp(100e8, 48e8), [100.0] * 15 + [95, 90, 88, 86, 84])

    read = read_turnover(series_from(profiles))

    picked = {item.name: item for item in read.entries}
    target = picked["失血板块"]
    # 占比落在中性带里，四象限说不出方向——占比这一路对它完全失明。
    assert abs(target.share_change) < SHARE_MOVE
    assert target.quadrant == QUADRANT_FLAT
    # 而绝对额榜必须把它排在第一，且量级远超任何陪衬。
    lost = read.by_amount_delta(3, rising=False)
    assert lost[0].name == "失血板块"
    assert abs(target.amount_delta) > 5 * abs(picked["陪衬00"].amount_delta)
    assert any("占比榜看不见这一类" in line for line in read.observations)


# ---- 样本与对齐 -----------------------------------------------------------


def test_small_sample_refuses_to_conclude() -> None:
    profiles = {f"板块{i:02d}": (flat(1e8), flat(100.0)) for i in range(MIN_SECTORS - 5)}

    read = read_turnover(series_from(profiles))

    assert not read.ok
    assert any("不可比" in note for note in read.notes)


def test_a_stale_sector_is_dropped_before_shares_are_computed() -> None:
    """停更的板块会让它缺席那几天的占比分母偏小，**抬高其余每一个板块**。

    所以对齐必须发生在归一之前，错的不只是它自己那一行。
    """
    profiles = baseline(MIN_SECTORS + 1)
    series = series_from(profiles)
    code, (name, rows) = next(iter(series.items()))
    series[code] = (name, [dict(row, date=f"202605{index + 1:02d}") for index, row in enumerate(rows)])

    read = read_turnover(series)

    assert read.trusted
    assert name not in {item.name for item in read.entries}
    assert any("占比分母偏小" in note for note in read.notes)
