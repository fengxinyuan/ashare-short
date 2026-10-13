"""跷跷板：两道闸门、按日期配对，以及「不预设关系」这条纪律。

这一层最容易出的错不是算错相关系数——那是三行公式。是**把噪声当成关系**：
90 个板块两两 4005 对，即便全是随机序列也会筛出一批看起来显著的。所以下面的
用例里，钉闸门的比钉算式的多。
"""

from __future__ import annotations

import math
import random

import pytest

from ashare_decision_support.sectors.panel import pearson
from ashare_decision_support.sectors.seesaw import (
    MIN_HALF_POINTS,
    NEGATIVE_THRESHOLD,
    SeesawRead,
    _returns,
    critical_correlation,
    explain_divergence,
    read_seesaw,
)


def _series(name: str, closes: list[float], start: int = 1) -> tuple[str, list[dict]]:
    """一条板块指数序列。日期用 2026 年 1 月起的连续整数戳，与落盘格式一致。"""
    bars = [
        {
            "date": f"2026{(start + index) // 100 + 1:02d}{(start + index) % 100 + 1:02d}",
            "close": close,
            "amount": 1e8,
        }
        for index, close in enumerate(closes)
    ]
    return name, bars


def _walk(steps: list[float], base: float = 100.0) -> list[float]:
    closes = [base]
    for step in steps:
        closes.append(closes[-1] * (1.0 + step))
    return closes


def _panel(pairs: dict[str, list[float]]) -> dict[str, tuple[str, list[dict]]]:
    return {
        f"BK{index:04d}": _series(name, closes)
        for index, (name, closes) in enumerate(pairs.items())
    }


def _opposite(count: int, amplitude: float = 0.02) -> tuple[list[float], list[float]]:
    """两条严格反向的日收益序列。用正弦而不是常数，避免方差为 0。"""
    up = [amplitude * math.sin(index / 3.0) for index in range(count)]
    return up, [-value for value in up]


def _noise(count: int, seed: int) -> list[float]:
    """确定性的伪随机收益。

    **必须是真的互不相关，不能只是相位不同的同一条正弦**——第一版用
    `sin(i*1.7 + seed)` 造陪跑序列，结果任意两条的相关系数是 ±1，陪跑板块自己
    霸榜把待测的那一对挤出了前 12。固定 seed 的 `Random` 才是「互不相关」。
    """
    rng = random.Random(seed)  # noqa: S311 — 造测试数据，不是密码学用途
    return [rng.gauss(0.0, 0.01) for _ in range(count)]


def _filler(count: int, bars: int) -> dict[str, list[float]]:
    """凑够 `MIN_SECTORS` 的陪跑板块，各自互不相关。

    **长度必须与待测的那两条严格相同。** `_walk` 会比收益序列多出基期那一根，
    所以这里传 `bars - 1`——第一版传了 `bars`，陪跑序列因此多一根、最新交易日
    比待测那两条晚一天，于是 `panel.align` 按多数派把**待测的那一对整个剔除了**，
    而榜是空的、测试报的是「没有跷跷板」，看不出是数据没进来。
    """
    return {f"陪跑{index}": _walk(_noise(bars - 1, index * 11 + 3)) for index in range(count)}


# 闸门 1：两个半窗必须同号


@pytest.mark.parametrize("same_direction_first", [True, False])
def test_a_pair_that_only_diverged_in_one_half_is_not_a_seesaw(same_direction_first) -> None:
    """任一半窗同向，都不足以构成稳定的反向关系。"""
    bars = 61
    half = (bars - 1) // 2
    up, down = _opposite(bars - 1)
    wobbly = up[:half] + down[half:] if same_direction_first else down[:half] + up[half:]

    panel = {"甲": _walk(up), "乙": _walk(wobbly), **_filler(20, bars)}
    read = read_seesaw(_panel(panel))

    assert read.trusted
    assert read.correlation_of("甲", "乙") is None


@pytest.mark.parametrize("flat_first", [True, False])
def test_a_pair_with_a_flat_half_is_not_reported(flat_first) -> None:
    bars = 61
    half = (bars - 1) // 2
    up, down = _opposite(bars - 1)
    flat = [0.0] * half
    partial = flat + down[half:] if flat_first else down[:half] + flat
    panel = {"甲": _walk(up), "半窗停更": _walk(partial), **_filler(20, bars)}

    read = read_seesaw(_panel(panel))

    assert read.trusted
    assert all("半窗停更" not in {pair.left, pair.right} for pair in read.pairs)


def test_a_pair_negative_in_both_halves_is_reported_with_both_coefficients() -> None:
    """报出去的每一条都必须带着两个半窗的系数——那是它可信度的全部依据。"""
    bars = 61
    up, down = _opposite(bars - 1)
    panel = {"甲": _walk(up), "乙": _walk(down), **_filler(20, bars)}
    read = read_seesaw(_panel(panel))

    pair = read.correlation_of("甲", "乙")
    assert pair is not None
    assert pair.correlation < -0.9
    # 直接断言两个字段，不经过一个「复核用」的属性——那种属性把构造时的过滤
    # 重写了一遍，两份判据可以漂开，而漂开的时候没有任何人会发现。
    assert pair.first_half < 0.0 and pair.second_half < 0.0
    # 描述里三个系数都在，简报读者不必回头查代码就能判断这条可不可信。
    assert "前半段" in pair.describe() and "后半段" in pair.describe()


# 闸门 2：多重比较必须写在脸上


def test_the_notes_say_how_many_pairs_were_tested_and_at_what_threshold() -> None:
    """检验了多少对、本轮阈值是多少，都是读懂这张榜的必要前提。

    不写对数，6 对读起来像 6 个发现；不写阈值，同一份榜在不同天的严格程度看不
    出来——而它是随对数变的。
    """
    bars = 61
    read = read_seesaw(_panel(_filler(22, bars)))

    assert read.pairs_tested == 22 * 21 // 2
    assert read.threshold == round(critical_correlation(22 * 21 // 2, bars - 1), 4)
    assert any(str(read.pairs_tested) in note for note in read.notes)
    assert any("Bonferroni" in note for note in read.notes)
    assert read.to_dict()["threshold"] == read.threshold


def test_the_threshold_tightens_as_more_pairs_are_tested() -> None:
    """**固定阈值在四千对上是无效的**，这是这一层与「算个相关矩阵」的全部区别。

    -0.35 那个常量只是地板：单对看 p≈0.006 挺显著，乘上 3240 对，光噪声就该出
    约 9 个假阳性——而这张榜通常只有几行。
    """
    assert critical_correlation(1, 60) == NEGATIVE_THRESHOLD  # 对数少时不放宽
    assert critical_correlation(3240, 60) < critical_correlation(231, 60) < NEGATIVE_THRESHOLD
    # 点数太少时 Fisher z 变换没有定义，退回地板而不是报错。
    assert critical_correlation(3240, 3) == NEGATIVE_THRESHOLD


def test_pure_noise_does_not_produce_a_seesaw() -> None:
    """这条是上面那条的理由，也是它的回归测试。

    第一版按固定 -0.35 筛，**231 对纯高斯噪声里筛出了一对 r=-0.357、两个半窗
    同为负的「跷跷板」**——两道闸门当时都放行了。写死这条用例，是因为下次有人
    想把阈值调回常量时，它会当场变红。
    """
    read = read_seesaw(_panel(_filler(22, bars=61)))

    assert read.trusted
    assert read.pairs == []


def test_a_one_versus_many_structure_is_called_out_as_one_finding() -> None:
    """N 对里 N 对都含同一个板块，那是一件事被列了 N 行，不是 N 条证据。

    2026-08-10 实测：12 对全部含「银行」——「银行 vs 整个电子链」一件事。当成
    多个独立信号读，会把同一个价格序列数很多遍。
    """
    bars = 61
    up, down = _opposite(bars - 1)
    # 一个板块与三个板块同时反向：三对，但只有一件事。
    panel = {
        "枢纽": _walk(up),
        "甲": _walk(down),
        "乙": _walk([value * 0.98 for value in down]),
        "丙": _walk([value * 1.02 for value in down]),
        **_filler(20, bars),
    }
    read = read_seesaw(_panel(panel))

    assert len(read.pairs) >= 3
    assert any("一对多结构" in line and "枢纽" in line for line in read.observations)


# 逐日配对


def test_returns_are_paired_by_trade_date_not_by_index() -> None:
    """**`panel.align` 只保证最后一根同日，不保证中间对得齐。**

    一个停牌三天的板块，清洗掉那三根之后第 60 根往前跨的是 63 个交易日。按下标
    zip 会把甲的周三配到乙的周一，而算出来的数看不出任何异常——它只是一个偏向
    0 的、错的系数。这条用例把中间几天挖掉，断言剩下的共同交易日仍然配得准。
    """
    bars = 71
    up, down = _opposite(bars - 1)
    panel = {"甲": _walk(up), "乙": _walk(down), **_filler(20, bars)}
    payload = _panel(panel)

    # 从「乙」中间挖掉 5 根（模拟停牌），最后一根保持不动。
    code = next(key for key, (name, _bars) in payload.items() if name == "乙")
    name, rows = payload[code]
    payload[code] = (name, rows[:30] + rows[35:])

    read = read_seesaw(payload)
    pair = read.correlation_of("甲", "乙")

    assert pair is not None
    # 按日期配对 ⇒ 仍然是强负相关。
    assert pair.correlation < -0.8
    # 共同交易日少了：挖掉 5 根会同时废掉它自己和它后面那一根的收益。
    assert MIN_HALF_POINTS * 2 <= pair.points < bars - 1

    # 同一份数据按**下标**配会错位，把系数整个拉向 0——而那个数看不出任何异常，
    # 它只是一个错的相关系数。两者差得越远，这条判据越值得留着。
    left = list(_returns(dict(payload.values()).get("甲") or [], 60).values())
    right = list(_returns(dict(payload.values()).get("乙") or [], 60).values())
    size = min(len(left), len(right))
    by_index = pearson(left[-size:], right[-size:])
    assert by_index is not None
    assert by_index > pair.correlation + 0.3


# 不预设、不预测


def test_a_pair_off_the_board_is_called_unknown_not_unrelated() -> None:
    """「不在榜上」= 没通过闸门，**不等于**「相关系数为 0」。

    银行 vs 半导体正是这一档（2026-08-10 实测未通过）。把它说成「无关」是在替
    数据下一个数据没给的结论。
    """
    read = SeesawRead(trusted=True, window=60)
    line = explain_divergence(read, "银行", "半导体", 0.012, -0.028)

    assert line is not None
    assert "没有稳定关系" in line
    assert "未通过跷跷板闸门" in line


def test_explaining_a_divergence_never_says_anything_about_tomorrow() -> None:
    """`explain_divergence` 这个名字就是边界本身。"""
    bars = 61
    up, down = _opposite(bars - 1)
    read = read_seesaw(_panel({"甲": _walk(up), "乙": _walk(down), **_filler(20, bars)}))

    line = explain_divergence(read, "甲", "乙", 0.012, -0.028)
    assert line is not None
    assert "与历史关系一致" in line
    assert "不含对明天的判断" in line
    # 榜上的三个系数在这句话里齐了：窗口长度、整窗、两个半窗。
    assert "前半段" in line and "后半段" in line


def test_no_divergence_means_nothing_to_explain() -> None:
    """两个都涨或都跌时不说话。硬凑一句「同向也符合历史」是把 beta 重报一遍。"""
    read = SeesawRead(trusted=True)
    assert explain_divergence(read, "甲", "乙", 0.01, 0.02) is None
    assert explain_divergence(read, "甲", "乙", -0.01, -0.02) is None
    assert explain_divergence(read, "甲", "乙", None, -0.02) is None


# 「没有」和「算不出来」是两件事


def test_an_empty_board_is_a_conclusion_not_a_failure() -> None:
    """没有一对通过闸门，本身就是一条结论：这段行情里板块是同涨同跌的 beta 关系。"""
    bars = 61
    read = read_seesaw(_panel(_filler(22, bars)))

    assert read.trusted
    assert read.pairs == []
    assert any("没有一对通过跷跷板闸门" in line for line in read.observations)
    assert any("对冲" in line for line in read.observations)


def test_too_few_sectors_is_a_data_problem_not_a_conclusion() -> None:
    """样本不够时 `trusted` 为假，措辞也不同——否则读的人会把「算不出」读成「没有」。"""
    bars = 61
    read = read_seesaw(_panel(_filler(5, bars)))

    assert not read.trusted
    assert read.pairs == []
    assert any("不可信" in note for note in read.notes)
    assert not any("没有一对通过" in line for line in read.observations)


def test_series_too_short_yields_no_read_at_all() -> None:
    """61 根是硬要求（60 个日收益）。短序列直接不给结论，不降级到更短的窗口。"""
    read = read_seesaw(_panel(_filler(30, bars=40)))

    assert not read.trusted
    assert read.sectors == 0
    assert any("没有可用的板块指数序列" in line for line in read.observations)


def test_a_flat_sector_is_skipped_instead_of_reading_as_uncorrelated() -> None:
    """整段不动 ⇒ 方差为 0 ⇒ 算不出相关，返回 None 而不是 0。

    返回 0 会让「这个板块这段时间没有报价变化」读起来像「实测无关」，而这两件事
    该导向不同的动作。同 `numeric` 那一层对「拿不到」与「等于零」的区分。
    """
    bars = 61
    up, _down = _opposite(bars - 1)
    panel = {"甲": _walk(up), "停更": [100.0] * bars, **_filler(20, bars)}
    read = read_seesaw(_panel(panel))

    assert read.trusted
    assert all("停更" not in {item.left, item.right} for item in read.pairs)
