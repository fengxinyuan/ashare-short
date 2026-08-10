"""涨停池的封板质量与扎堆方向。

`LimitStock` 一直解析着 `first_seal`（首封时间）、`seal_fund`（封单金额）、
`sector`（东财行业）三个字段，2026-08-07 实测 74 只涨停三项全有值——但在
2026-08-09 之前**没有任何调用方**，简报只印得出「涨停 74 只」。

于是涨停池答不了超短线真正要问的两件事：这些板封得结不结实（封单薄的次日容易
被砸开），以及它们扎堆在哪（这是「主线在哪」最直接的读数，比连板高度更早给出
方向）。下面的用例钉的就是这三个读数不再被丢掉。
"""

from __future__ import annotations

from datetime import date

from ashare_decision_support.adapters.limit_pool import (
    EARLY_SEAL_BEFORE,
    LimitPoolSnapshot,
    LimitStock,
)


def _stock(name: str, **kw) -> LimitStock:
    base = {"symbol": f"{abs(hash(name)) % 1000000:06d}.SZ", "name": name}
    return LimitStock(**(base | kw))


def _snapshot(*limit_up: LimitStock) -> LimitPoolSnapshot:
    return LimitPoolSnapshot(trade_date=date(2026, 8, 7), limit_up=list(limit_up))


# ---- 首封时间 -------------------------------------------------------------


def test_early_seal_ratio_counts_only_stamped_stocks() -> None:
    """没有首封时间的票不进分母——否则拿不到时间会被读成「封得晚」。"""
    snap = _snapshot(
        _stock("早1", first_seal="09:25"),
        _stock("早2", first_seal="09:31"),
        _stock("晚1", first_seal="14:30"),
        _stock("无时间"),
    )

    # 与 `blowup_rate` 同样收到小数点后 4 位：这个比例会整份序列化进台账，
    # 不收的话 0.6666666666666666 和 0.6667 在下一次 diff 里是两个值。
    assert snap.early_seal_ratio == 0.6667


def test_early_seal_ratio_is_none_when_no_stock_carries_a_stamp() -> None:
    """全都拿不到首封时间时返回 None，不是 0.0。

    「没有一只早盘封板」和「拿不到首封时间」是相反的两件事，同 `blowup_rate`
    分母为 0 的处置。
    """
    assert _snapshot(_stock("甲"), _stock("乙")).early_seal_ratio is None


def test_the_early_late_boundary_is_the_shared_constant() -> None:
    """分界线只有一条，措辞和判定都走它——写死两遍必然漂。"""
    snap = _snapshot(_stock("刚好晚", first_seal=EARLY_SEAL_BEFORE))

    assert snap.early_seal_ratio == 0.0  # 等于分界线算晚，不算早


# ---- 封单金额 -------------------------------------------------------------


def test_median_seal_fund_is_not_dragged_by_one_giant() -> None:
    """封单金额是长尾分布，中位数才说得出「全场封板普遍薄不薄」。

    2026-08-07 实测最大 6.51 亿、中位只有 0.72 亿。用均值的话那一只独苗会把
    读数抬起来，把「普遍很薄」这个事实盖掉——而薄不薄才是次日会不会被砸开的
    那个量。
    """
    snap = _snapshot(
        _stock("小1", seal_fund=5e7),
        _stock("小2", seal_fund=7e7),
        _stock("独苗", seal_fund=65e8),
    )

    assert snap.median_seal_fund == 7e7
    mean = (5e7 + 7e7 + 65e8) / 3
    assert snap.median_seal_fund < mean / 10


def test_median_seal_fund_is_none_without_data() -> None:
    assert _snapshot(_stock("甲"), _stock("乙")).median_seal_fund is None


# ---- 扎堆方向 -------------------------------------------------------------


def test_sector_distribution_ranks_where_the_limit_ups_cluster() -> None:
    """「主线在哪」最直接的读数。2026-08-07 实测：元件 11 只、医疗服务 8 只。"""
    snap = _snapshot(
        *[_stock(f"元件{i}", sector="元件") for i in range(3)],
        *[_stock(f"医疗{i}", sector="医疗服务") for i in range(2)],
        _stock("半导体1", sector="半导体"),
        _stock("没有行业"),
    )

    assert snap.sector_distribution(top=2) == [("元件", 3), ("医疗服务", 2)]


def test_sector_distribution_breaks_ties_by_name_so_it_stays_diffable() -> None:
    """并列时按行业名排序：台账要能逐日 diff，顺序不能随字典插入序漂。

    用 A/B 而不是「甲/乙」举例：排序走的是码点序，而「甲」(U+7532) 的码点大于
    「乙」(U+4E59)，拿天干直觉读这条用例会读反。
    """
    snap = _snapshot(
        _stock("B1", sector="B行业"),
        _stock("A1", sector="A行业"),
    )

    assert snap.sector_distribution() == [("A行业", 1), ("B行业", 1)]


# ---- 进简报 ---------------------------------------------------------------


def test_describe_carries_all_four_readings() -> None:
    """四个读数都要落到简报的行里，否则又回到「算了没人读」。"""
    snap = _snapshot(
        _stock("早", first_seal="09:25", seal_fund=8e7, turnover=3.0, sector="元件"),
        _stock("晚", first_seal="14:30", seal_fund=6e7, turnover=9.0, sector="元件"),
    )

    text = "\n".join(snap.describe())
    assert "早盘封板占比 50%" in text
    assert "封单中位" in text
    assert "换手中位" in text
    assert "元件 2 只" in text


def test_describe_skips_a_reading_it_has_no_data_for() -> None:
    """三个读数各自缺数据时单独跳过，不互相拖累。"""
    snap = _snapshot(_stock("无字段"), _stock("也无"))
    text = "\n".join(snap.describe())

    assert "涨停 2 只" in text
    assert "早盘封板占比" not in text
    assert "封板强度" not in text
    assert "涨停扎堆的行业" not in text


# ---- 换手率：封单的另一面 --------------------------------------------------


def test_median_turnover_reads_the_other_half_of_seal_strength() -> None:
    """封单答「有多少钱压在买一」，换手答「有多少筹码今天换了手」。

    2026-08-07 实测封单中位 0.72 亿、换手中位约 8%，而当天换手最低的宝鼎科技只有
    1.21%（封单 2.20 亿、4 板）——**封单薄但换手也低是惜售，封单薄且换手高才是
    随时会开板**。只看一个得不出结论，所以两个写在同一行。
    """
    snap = _snapshot(
        _stock("惜售", seal_fund=6e7, turnover=1.2),
        _stock("激烈", seal_fund=6e7, turnover=24.0),
        _stock("居中", seal_fund=6e7, turnover=8.0),
    )

    assert snap.median_turnover == 8.0
    line = next(line for line in snap.describe() if line.startswith("封板强度"))
    assert "封单中位" in line and "换手中位" in line


def test_seal_strength_line_survives_a_missing_half() -> None:
    """只有一半数据时报一半，不整行消失——两个字段各自独立缺失。"""
    only_turnover = _snapshot(_stock("甲", turnover=5.0))
    line = next(line for line in only_turnover.describe() if line.startswith("封板强度"))

    assert "换手中位" in line
    assert "封单中位" not in line


def test_median_turnover_is_none_without_data() -> None:
    assert _snapshot(_stock("甲"), _stock("乙")).median_turnover is None
