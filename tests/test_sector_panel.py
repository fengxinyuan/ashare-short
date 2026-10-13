"""板块横截面的共用判据：`MIN_SECTORS` 只有一处定义。

`sectors/panel.py` 的注释记着这个旋钮被从 `rotation` / `turnover` / `seesaw`
三处收上来的经过。但**收一次不等于收干净**：收口之后新加的 `backtest.py` 又
自己写了一份同名同值的常量，用在同样的横截面下限上，四道闸门一道没红。

所以这道闸只能由测试来把——常量重复不是 ruff 认的错，接线审计也只查「有没有
被读」，不查「是不是同一个判据被写了两遍」。
"""

from __future__ import annotations

import ast
import pathlib

from ashare_decision_support.sectors import backtest, rotation, seesaw, turnover
from ashare_decision_support.sectors.panel import (
    COMPACT_KEEP_NOTES,
    COMPACT_KEEP_ROWS,
    MIN_SECTORS,
    compact_reading,
)


def test_compact_reading_drops_the_full_table_and_keeps_leading_examples() -> None:
    """`ask` 与研究包交给外部模型看的是同一份压缩口径。

    此前两处各带一份逐字相同的实现；这里钉住的是口径本身：整张 `sectors` 榜
    不带，结论类列表留前几条，其余列表留前几条，标量原样。
    """
    reading = {
        "as_of": "2026-09-11",
        "sectors": [{"name": f"板块{i}"} for i in range(90)],
        "observations": [f"观察{i}" for i in range(10)],
        "notes": [f"说明{i}" for i in range(10)],
        "leaders": [f"龙头{i}" for i in range(10)],
        "trusted": True,
    }
    compact = compact_reading(reading)

    assert "sectors" not in compact
    assert compact["as_of"] == "2026-09-11"
    assert compact["trusted"] is True
    assert compact["observations"] == [f"观察{i}" for i in range(COMPACT_KEEP_NOTES)]
    assert compact["notes"] == [f"说明{i}" for i in range(COMPACT_KEEP_NOTES)]
    assert compact["leaders"] == [f"龙头{i}" for i in range(COMPACT_KEEP_ROWS)]
    # 不是 dict 的原样交回——读数缺失时上游放的是 None。
    assert compact_reading(None) is None
    assert compact_reading(["not", "a", "dict"]) == ["not", "a", "dict"]


def test_every_sector_module_reads_the_same_floor() -> None:
    """四个下游读到的必须是同一个对象，不是碰巧相等的四个 20。"""
    assert rotation.MIN_SECTORS is MIN_SECTORS
    assert turnover.MIN_SECTORS is MIN_SECTORS
    assert seesaw.MIN_SECTORS is MIN_SECTORS
    assert backtest.MIN_SECTORS is MIN_SECTORS


def test_min_sectors_is_defined_exactly_once() -> None:
    """除 `panel.py` 外，没有模块可以给 `MIN_SECTORS` 赋值。

    不能只比相等：20 会被 CPython 驻留，重新写一遍字面量照样 `is` 成立，
    而这正是 `backtest.py` 那份能躲过所有检查的原因。所以查的是**源码里的
    赋值语句**——`sectors/panel.py` 是唯一的定义处，其余一律 import。
    """
    from ashare_decision_support.sectors import panel

    package = pathlib.Path(panel.__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "panel.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "MIN_SECTORS":
                    offenders.append(f"{path.relative_to(package)}:{node.lineno}")

    assert offenders == [], "板块数下限要从 sectors/panel.py 取，不要各写一份：" + "、".join(
        offenders
    )


def test_rounding_helper_is_defined_exactly_once() -> None:
    """落盘前的小数位收口在 `sectors/` 里只有 `panel.py` 一处。

    `baserate.py` 曾自带一份逐字相同的 `_rounded`——`MIN_SECTORS` 那次收口之后
    加的模块，和 `backtest.py` 是同一种漏法。查法同上：查源码里的函数定义。
    只扫 `sectors/` 子包：别的层有自己的量纲约定，不归这条闸门管。
    """
    from ashare_decision_support.sectors import panel

    package = pathlib.Path(panel.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "panel.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in {"rounded", "_rounded"}:
                offenders.append(f"{path.relative_to(package)}:{node.lineno}")

    assert offenders == [], "小数位收口要从 sectors/panel.py 取，不要各写一份：" + "、".join(
        offenders
    )
