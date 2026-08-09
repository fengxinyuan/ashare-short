"""`numeric.py` 的收口判据。

这个模块此前**一条测试都没有**，而它是全仓调用最密、也最安全攸关的一个：它的
docstring 讲的正是「仓库里九份 `_float` 各漏掉不同的一项」。漏掉的那一项不会
报错——一个 NaN 参与比较永远得 False，于是「跌破止损」静默变成「没跌破」。

`finite` 后来加了一条 `type(value) is float` 的快路（为性能，不改判据）。这一组
测试同时钉住两件事：判据没变，以及**numpy 标量不许走快路**——`numpy.float64`
是 `float` 的子类，用 `isinstance` 判就会把一个 numpy 标量原样交给下游。
"""

from __future__ import annotations

import math

import pytest

from ashare_decision_support.numeric import as_int, clamp, finite, first_finite, positive


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (12.5, 12.5),
        (12, 12.0),
        (-3.25, -3.25),
        (0.0, 0.0),
        ("12.5", 12.5),
        ("  12.5  ", 12.5),          # 两头的空白要剥掉
        ("1e3", 1000.0),
    ],
)
def test_finite_accepts_anything_that_is_a_real_number(value, expected) -> None:
    assert finite(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "", "-", "--", "null", "None", "N/A", "n/a", "nan",   # 缺失占位
        "abc", "12.5.6",
        float("nan"), float("inf"), float("-inf"),            # 非读数
        [], {}, (), object(),
    ],
)
def test_finite_refuses_everything_else(value) -> None:
    assert finite(value) is None


def test_finite_refuses_bool_because_it_means_the_source_is_wrong() -> None:
    """`float(True)` 是 1.0。价格字段里出现 True 是上游给错了，不是「一块钱」。"""
    assert finite(True) is None
    assert finite(False) is None


def test_finite_always_returns_a_native_float() -> None:
    """快路只许放行**精确**是 float 的值，返回类型必须始终是原生 float。"""
    for value in (12, "12.5", 12.5):
        assert type(finite(value)) is float


def test_numpy_scalars_are_converted_not_passed_through() -> None:
    """**这条钉的是 `type(...) is float` 而不是 `isinstance(...)`。**

    `numpy.float64` 是 `float` 的子类。用 `isinstance` 做快路判断会让一个 numpy
    标量原样返回给下游——而 `adapters/qmt_xtdata._to_plain` 整个存在的理由就是
    不让 numpy 标量漏进来。
    """
    np = pytest.importorskip("numpy")

    result = finite(np.float64(3.5))
    assert result == 3.5
    assert type(result) is float, "numpy 标量漏进了下游"

    assert finite(np.float64("nan")) is None
    assert finite(np.float64("inf")) is None
    assert type(finite(np.int64(7))) is float


def test_positive_treats_zero_as_missing() -> None:
    """价格/成交量字段上的 0 含义是「没取到」——真停牌是没有这一行。"""
    assert positive(3.5) == 3.5
    assert positive(0) is None
    assert positive(-1.0) is None
    assert positive("nan") is None


def test_first_finite_takes_the_first_field_that_holds() -> None:
    """同一个读数在不同源里叫不同名字，调用方给优先级，这里只管「哪个先收得住」。"""
    row = {"close": "-", "收盘": "12.5", "last": 99.0}
    # `close` 在，但值是缺失占位，所以要继续往后试而不是就此返回 None。
    assert first_finite(row, "close", "收盘", "last") == 12.5
    # 字段名不存在与字段值收不住，两种都要跳过。
    assert first_finite(row, "missing", "close", "last") == 99.0
    assert first_finite({"a": "-"}, "a", "b") is None
    assert first_finite({}, "a") is None


def test_clamp_holds_the_declared_range() -> None:
    """夹持是评分的量纲约定：越界的分项会在合成时把总分推出 0-100。"""
    assert clamp(5.0, 0.0, 10.0) == 5.0
    assert clamp(-1.0, 0.0, 10.0) == 0.0
    assert clamp(99.0, 0.0, 10.0) == 10.0


def test_clamp_silently_flattens_nan_to_low() -> None:
    """**NaN 进来出去的是 `low`，不是 NaN。**

    `min(nan, high)` 交还 nan，接着 `max(low, nan)` 里 `nan > low` 为 False，
    于是返回 `low`。对打分层 `clamp(nan, 0, 100)` 就是 0 分——最低分，而不是一个
    一眼看得出不对的 NaN。

    这条钉的不是「应该这样」，是「事实就是这样」：`clamp` 的 docstring 曾经写反
    过（说会原样交还 NaN），于是上游看起来无害的收数写法实际在打满负分。NaN 要
    挡就得挡在 `finite` 那一层。
    """
    assert clamp(float("nan"), 0.0, 100.0) == 0.0
    assert clamp(float("nan"), -2.5, 2.5) == -2.5
    assert not math.isnan(clamp(float("nan"), 0.0, 10.0))


def test_as_int_goes_through_finite_first() -> None:
    assert as_int("3.0") == 3
    assert as_int(3.9) == 3
    assert as_int(float("nan")) == 0
    assert as_int(True) == 0          # bool 被 finite 挡掉，不是 1
    assert as_int(None, default=-1) == -1
