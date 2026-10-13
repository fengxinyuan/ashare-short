"""校验层测试。

每条测试对应一个**实测记录在案**的故障：这些问题的共同点是出错时不抛异常、
简报照出、数字看着正常。测试是它们唯一的哨兵。
"""

from __future__ import annotations

from ashare_decision_support.adapters.validate import (
    DEFAULT_TOLERANCE,
    cross_check,
)

# 跨源比对


def test_a_unit_mismatch_is_reported_separately_from_a_reading_gap() -> None:
    """量纲差 100 倍是 bug（有确定修法），差 2% 是厂商口径差异（没有正确答案）。

    混在一起报会把唯一可修的那类埋进噪声里。
    """
    primary = {
        "600519.SH": {"volume": 93930, "close": 1350.60},  # 手
        "000001.SZ": {"volume": 500000, "close": 12.77},
    }
    fallback = {
        "600519.SH": {"volume": 9393000, "close": 1350.60},  # 股 —— 差 100 倍
        "000001.SZ": {"volume": 500000, "close": 13.05},  # 差 2.2% —— 读数分歧
    }

    result = cross_check(primary, fallback, fields=["volume", "close"])

    assert len(result.unit_bugs) == 1
    assert result.unit_bugs[0].field == "volume"
    assert len(result.reading_gaps) == 1
    assert result.reading_gaps[0].field == "close"

    notes = " / ".join(result.notes())
    assert "量纲" in notes and "bug 不是数据分歧" in notes
    assert "厂商口径差异" in notes


def test_the_ten_thousand_scale_is_recognised_too() -> None:
    """流通市值：TickFlow 要自算 float_shares×现价（元），新浪给 nmc（万元）。

    不归一则 20 亿门槛变成 20 万，可交易性筛选静默失效——通过率会跳到 100%。
    """
    result = cross_check(
        {"600519.SH": {"float_mv": 1_700_000_000_000.0}},
        {"600519.SH": {"float_mv": 170_000_000.0}},
        fields=["float_mv"],
    )

    assert len(result.unit_bugs) == 1
    assert abs(result.unit_bugs[0].scale) == 10000.0


def test_agreement_is_reported_not_just_silence() -> None:
    """「比过了，一致」和「根本没比」在只报异常的设计里长得一模一样。"""
    rows = {"600519.SH": {"close": 1350.60, "volume": 93930}}
    result = cross_check(rows, dict(rows), fields=["close", "volume"])

    assert result.compared == 2
    assert result.disputes == []
    assert any("两路一致" in note for note in result.notes())


def test_normal_vendor_drift_stays_below_the_tolerance() -> None:
    """两家分钟线重叠部分实测相对差中位 ~1e-4、最大 0.31%，不该报成异常。"""
    result = cross_check(
        {"600519.SH": {"close": 1350.60}},
        {"600519.SH": {"close": 1350.60 * 1.003}},  # 0.3%
        fields=["close"],
    )

    assert result.disputes == []
    assert result.agreed == 1
    assert DEFAULT_TOLERANCE > 0.003


def test_only_the_overlap_is_compared() -> None:
    """一路独有的标的与字段没有比对对象，不该被当成分歧。"""
    result = cross_check(
        {"600519.SH": {"close": 1350.6, "pe": 28.0}, "000001.SZ": {"close": 12.77}},
        {"600519.SH": {"close": 1350.6}},
        fields=["close", "pe"],
    )

    assert result.compared == 1  # 只有 600519 的 close 两边都有
    assert result.disputes == []


def test_missing_values_are_not_disputes() -> None:
    result = cross_check(
        {"600519.SH": {"pe": None, "pb": 0}},
        {"600519.SH": {"pe": 28.0, "pb": 9.1}},
        fields=["pe", "pb"],
    )

    assert result.compared == 0
    assert result.notes() == []


def test_a_large_gap_is_not_written_off_as_a_vendor_convention() -> None:
    """大额分歧和舍入差必须分开报。

    2026-08-02 全市场实测：179 处分歧里 21 处相对差 ≥10%、最大 77.7%，全部在
    `float_cap` 上。旧口径把它们和 0.6% 的舍入差一起写成「属厂商口径差异，非错误」——
    而 `float_cap` 正是 `LiquidityScreen` 用来放行或否决候选的字段，差一倍就是
    候选池里多一只或少一只。简报不能主动叫人忽略一个真的错。
    """
    primary = {"000973": {"float_cap": 1.16768e10}, "600519": {"float_cap": 1.0e10}}
    fallback = {"000973": {"float_cap": 8.55356e9}, "600519": {"float_cap": 1.006e10}}

    result = cross_check(primary, fallback, ["float_cap"])

    assert [d.key for d in result.material_gaps] == ["000973"]
    assert [d.key for d in result.reading_gaps] == ["600519"]

    notes = " ".join(result.notes())
    assert "大额分歧" in notes and "必有一路是错的" in notes
    assert "000973" in notes
    # 小额那条仍然可以说是口径差异，但不能把 36% 的那条也盖进去。
    assert "属厂商口径差异，非错误" in notes


def test_a_unit_error_still_outranks_the_severity_split() -> None:
    """量纲错是唯一有确定修法的一类，仍然单独且优先报，不被归进大额分歧。"""
    result = cross_check(
        {"600519": {"volume": 12_345_600.0}}, {"600519": {"volume": 123_456.0}}, ["volume"]
    )

    assert result.unit_bugs and not result.material_gaps
    assert "量纲" in result.notes()[0]
