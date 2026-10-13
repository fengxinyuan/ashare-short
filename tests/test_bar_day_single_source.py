"""交易日解析的共用口径及缓存、核对和策略消费者回归。

AST 检查防止重复定义时间戳键名表；快照采集时点的字段优先级单独保留。
行为检查覆盖日期别名、epoch、北京时间跨日，以及缓存读写。
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest

from ashare_decision_support.adapters.daily_cache import DailyBarsCache
from ashare_decision_support.journal.verify import _dated_bars
from ashare_decision_support.schedule.timezone import BEIJING_TZ
from ashare_decision_support.signals import series
from ashare_decision_support.signals.series import (
    bar_date,
    bar_day,
    normalize_bars,
    parse_bar_date,
    parse_bar_datetime,
)
from ashare_decision_support.strategy.overnight import _ordered_bars

# 相对 `src/ashare_decision_support/` 的路径。
EXEMPT = {
    "signals/series.py",  # 表的定义处
    "adapters/tickflow_adapter.py",  # 问的是快照时刻，不是交易日；见模块 docstring
}

KEY_FAMILY = {"timestamp", "date"}


def _string_tuples(tree: ast.AST) -> Iterator[tuple[int, set[str]]]:
    """全是字符串字面量的元组，连行号一起给出。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple) or not node.elts:
            continue
        values = [
            item.value
            for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
        if len(values) == len(node.elts):
            yield node.lineno, set(values)


def _get_chains(tree: ast.AST) -> Iterator[tuple[int, set[str]]]:
    """`x.get("a") or x.get("b") …` 这种兜底链里出现的键名。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        keys: set[str] = set()
        for value in node.values:
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "get"
                and value.args
                and isinstance(value.args[0], ast.Constant)
                and isinstance(value.args[0].value, str)
            ):
                keys.add(value.args[0].value)
        if keys:
            yield node.lineno, keys


def test_the_timestamp_key_table_is_defined_exactly_once() -> None:
    package = pathlib.Path(series.__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        if relative in EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, keys in (*_string_tuples(tree), *_get_chains(tree)):
            if keys >= KEY_FAMILY:
                offenders.append(f"{relative}:{lineno}")

    assert offenders == [], (
        "K 线时间戳的键名表要从 signals/series.py 取（bar_date / bar_day），不要各抄一份："
        + "、".join(offenders)
    )


def test_the_exemption_still_exists() -> None:
    """豁免的那一份被删了或改名了，上面那条就该把它从 EXEMPT 里摘掉。"""
    package = pathlib.Path(series.__file__).resolve().parent.parent
    tree = ast.parse((package / "adapters" / "tickflow_adapter.py").read_text(encoding="utf-8"))
    assert any(keys >= KEY_FAMILY for _lineno, keys in _string_tuples(tree))


# 解析：三种写法收敛成同一天


EPOCH_SECONDS = int(datetime(2025, 8, 1, 10, 30, tzinfo=BEIJING_TZ).timestamp())


@pytest.mark.parametrize(
    "value",
    [
        datetime(2025, 8, 1, 15, 0),
        date(2025, 8, 1),
        "2025-08-01",
        "2025-08-01 15:00:00",
        "2025-08-01T15:00:00",
        "20250801",
        20250801,
        "2025/08/01",
        "2025-8-1",
        EPOCH_SECONDS,
        EPOCH_SECONDS * 1000,
    ],
)
def test_every_dialect_lands_on_the_same_trading_day(value) -> None:
    """同一天来自两个源的两根不能变成两个不同的键——那等于两边一天都对不上。"""
    assert parse_bar_date(value) == date(2025, 8, 1)
    assert bar_day({"timestamp": value}) == "2025-08-01"


@pytest.mark.parametrize("value", [None, "", "not-a-date", "2025-13-40", True, object()])
def test_unreadable_dates_are_none_not_today(value) -> None:
    """认不出来必须是 None：盖一个今天的戳上去会和另一条序列里真正的今天撞在一起。"""
    assert parse_bar_date(value) is None
    assert bar_date({"date": value}) is None


def test_a_zero_timestamp_falls_through_to_the_date_field() -> None:
    """`timestamp=0` 而 `date` 有值的 K 线，答案是后者而不是 1970。"""
    assert bar_day({"timestamp": 0, "date": "2025-08-01"}) == "2025-08-01"


def test_a_row_without_any_date_field_is_none() -> None:
    assert bar_date({"close": 10.0}) is None
    assert bar_day({}) is None


# 三个原来各抄一份的消费者，现在读到的是同一天


def test_the_cache_and_the_verifier_agree_with_bar_day(tmp_path) -> None:
    rows = [
        {"date": "20250803", "close": 3.0},
        {"timestamp": datetime(2025, 8, 1, 15, 0), "close": 1.0},
        {"日期": "2025/08/02", "close": 2.0},
    ]
    cache = DailyBarsCache(root=tmp_path)
    cache.write("600519.SH", rows)
    cached = cache.read("600519.SH", date(2025, 8, 1), date(2025, 8, 3))
    assert [bar_date(row) for row in cached] == [
        date(2025, 8, 1),
        date(2025, 8, 2),
        date(2025, 8, 3),
    ]
    assert [row["close"] for row in cached] == [1.0, 2.0, 3.0]
    assert cache.latest_date(rows) == date(2025, 8, 3)
    assert [stamp for _row, stamp in _dated_bars(rows)] == [
        date(2025, 8, 1),
        date(2025, 8, 2),
        date(2025, 8, 3),
    ]
    assert [row["close"] for row in _ordered_bars(rows)] == [1.0, 2.0, 3.0]


def test_the_cache_no_longer_drops_epoch_rows(tmp_path) -> None:
    rows = [{"timestamp": EPOCH_SECONDS * 1000, "close": 1.0}]
    cache = DailyBarsCache(root=tmp_path)
    cache.write("600519.SH", rows)
    day = date(2025, 8, 1)
    assert cache.read("600519.SH", day, day) == rows
    assert cache.latest_date(rows) == day


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 8, 17, 20, tzinfo=UTC),
        "2026-08-17T20:00:00Z",
        "2026-08-18T04:00:00+08:00",
        " 2026-08-18 ",
    ],
)
def test_trading_dates_are_in_beijing_even_across_utc_midnight(value, tmp_path) -> None:
    row = {"timestamp": value}
    assert bar_day(row) == "2026-08-18"
    assert DailyBarsCache(root=tmp_path).latest_date([row]) == date(2026, 8, 18)
    assert _dated_bars([row])[0][1] == date(2026, 8, 18)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 10**400])
def test_invalid_numeric_timestamp_does_not_escape_the_shared_parser(value) -> None:
    assert parse_bar_datetime(value) is None
    assert parse_bar_date(value) is None


@pytest.mark.parametrize("value", ["bad", True, float("nan"), 10**400])
def test_bad_primary_timestamp_can_use_a_valid_date_alias(value, tmp_path) -> None:
    row = {"timestamp": value, "date": "2026-08-18"}
    assert bar_day(row) == "2026-08-18"
    assert DailyBarsCache(root=tmp_path).latest_date([row]) == date(2026, 8, 18)


def test_mixed_aware_and_naive_bars_sort_in_beijing_time() -> None:
    rows = normalize_bars(
        [
            {"timestamp": "2026-08-18T01:30:00Z", "close": 1},
            {"timestamp": "2026-08-18 10:00:00", "close": 2},
        ]
    )
    assert [row["close"] for row in rows] == [1, 2]
    assert rows[0]["timestamp"] == datetime(2026, 8, 18, 9, 30)
