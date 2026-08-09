"""校验层测试。

每条测试对应一个**实测记录在案**的故障：这些问题的共同点是出错时不抛异常、
简报照出、数字看着正常。测试是它们唯一的哨兵。
"""

from __future__ import annotations

from ashare_decision_support.adapters.validate import (
    DEFAULT_TOLERANCE,
    cross_check,
    infer_volume_unit,
    sanitise_ohlc,
)

# ---- 跨源比对 -------------------------------------------------------------


def test_a_unit_mismatch_is_reported_separately_from_a_reading_gap() -> None:
    """量纲差 100 倍是 bug（有确定修法），差 2% 是厂商口径差异（没有正确答案）。

    混在一起报会把唯一可修的那类埋进噪声里。
    """
    primary = {
        "600519.SH": {"volume": 93930, "close": 1350.60},   # 手
        "000001.SZ": {"volume": 500000, "close": 12.77},
    }
    fallback = {
        "600519.SH": {"volume": 9393000, "close": 1350.60},  # 股 —— 差 100 倍
        "000001.SZ": {"volume": 500000, "close": 13.05},     # 差 2.2% —— 读数分歧
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
        {"600519.SH": {"close": 1350.60 * 1.003}},   # 0.3%
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

    assert result.compared == 1          # 只有 600519 的 close 两边都有
    assert result.disputes == []


def test_missing_values_are_not_disputes() -> None:
    result = cross_check(
        {"600519.SH": {"pe": None, "pb": 0}},
        {"600519.SH": {"pe": 28.0, "pb": 9.1}},
        fields=["pe", "pb"],
    )

    assert result.compared == 0
    assert result.notes() == []


# ---- 浮点卫生 -------------------------------------------------------------


def test_adjustment_float_tails_no_longer_invert_high_and_low() -> None:
    """实测 60 只 / 15000 根 1 分钟线里 1399 根（9.33%）满足 high<low。

    复权对四个字段各自算，尾巴不一样，倒挂就出来了。下游拿 high-low 当振幅
    会得到负数，拿 low==min(lows) 找枢轴点会漏掉真正的低点——两者都不报错。
    """
    result = sanitise_ohlc([
        {"high": 1350.599999999988, "low": 1350.6, "open": 1350.6, "close": 1350.6},
    ])

    bar = result.bars[0]
    assert bar["high"] >= bar["low"]
    assert bar["high"] - bar["low"] >= 0
    # 纯浮点尾巴，取整后自然消解，不该报成数据错误
    assert result.inverted == 0
    assert result.notes == []


def test_a_genuinely_inverted_bar_is_swapped_and_counted() -> None:
    """取整之后仍然倒挂的是真错误，不是浮点尾巴——一根写反的 bar 比缺一根更危险。"""
    result = sanitise_ohlc([{"high": 10.0, "low": 12.0, "close": 11.0}])

    assert result.bars[0]["high"] == 12.0
    assert result.bars[0]["low"] == 10.0
    assert result.inverted == 1
    assert any("上游数据错误" in note for note in result.notes)


def test_sanitising_never_mutates_the_caller_s_bars() -> None:
    original = [{"high": 10.0, "low": 12.0}]
    sanitise_ohlc(original)

    assert original[0]["high"] == 10.0, "入参必须原样不动"


def test_sanitising_tolerates_junk_rows() -> None:
    result = sanitise_ohlc([{}, {"high": None, "low": "x"}, {"high": 1.0, "low": 0.5}])

    assert len(result.bars) == 3
    assert result.inverted == 0


# ---- 量纲反推 -------------------------------------------------------------


def test_volume_in_lots_is_detected_from_amount_over_volume() -> None:
    """茅台：93930 手、现价 1350.60 → 成交额 12,686,185,800 元。

    `amount / volume` 得到 135060，正是 price×100——volume 记的是手。不换算的话
    VWAP 算成 135060 而不是 1350.60，然后每只票都显示为永远在均价下方，
    open-follow 变成无差别 BLOCK。
    """
    multiplier, note = infer_volume_unit(
        volume=93_930, amount=93_930 * 100 * 1350.60, price=1350.60
    )

    assert multiplier == 100.0
    assert "手" in note


def test_volume_already_in_shares_needs_no_conversion() -> None:
    """同一笔成交，volume 记成股时 amount/volume 直接就是均价。"""
    multiplier, note = infer_volume_unit(
        volume=9_393_000, amount=9_393_000 * 1350.60, price=1350.60
    )

    assert multiplier == 1.0
    assert "股" in note


def test_an_unresolvable_unit_refuses_rather_than_guesses() -> None:
    """推不出来就不给 VWAP，绝不猜——猜错就是 100 倍。"""
    multiplier, note = infer_volume_unit(volume=7, amount=271_445_068.0, price=1350.60)

    assert multiplier is None
    assert "拒绝推导" in note


def test_missing_inputs_refuse_quietly() -> None:
    assert infer_volume_unit(None, 100.0, 10.0)[0] is None
    assert infer_volume_unit(100.0, None, 10.0)[0] is None
    assert infer_volume_unit(100.0, 100.0, None)[0] is None
    assert infer_volume_unit(0, 100.0, 10.0)[0] is None


def test_ordinary_intrabar_swing_is_not_mistaken_for_a_unit_error() -> None:
    """参考价是收盘价，隐含均价是全时段加权——一根振幅 10% 的 bar 里差几个
    百分点很正常。要分辨的是 1 倍与 100 倍，不是 1.0 与 1.05。"""
    multiplier, _ = infer_volume_unit(volume=1_000_000, amount=10_800_000.0, price=10.0)

    assert multiplier == 1.0, "10.8 的隐含均价对 10.0 的收盘价来说完全正常"


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
