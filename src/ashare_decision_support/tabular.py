"""表格收口：把数据源交出来的「一张表」变成 `list[dict]`，把单元格变成字符串。

和 `numeric.py` 是一对——那边管**单元格里的数**，这边管**表的形状和单元格里
的字**。两处解决的是同一件事的两半：akshare 这类源有时给 `DataFrame`、有时给
`list[dict]`、拿不到时给 `None`，而每个适配器都得先把这三种情况抹平才能开始
干活。

原来六个文件各写了一份，其中三份逐字相同。收在这里的不只是重复：

- `to_dict(orient="records")` 是 pandas 专有调用。写成能力探测（`getattr` +
  `callable`）而不是 `import pandas`，是因为 pandas 是**可选依赖**——没装
  akshare 的机器上主链路照跑，不该因为一个类型判断把它变成硬依赖。
- `"nan"` / `"none"` / `"nat"` 这三个字符串是 pandas 把缺失值 `str()` 之后的
  样子。**它们会通过所有非空判断**：`bool("nan")` 是 True，`"nan" or "默认值"`
  拿到的是 `"nan"`。于是一条没有标题的公告，标题字段会变成字符串 `"nan"` 并
  一路印进简报。这是必须在解析层挡掉、不能留给下游的一类值。
"""

from __future__ import annotations

from typing import Any

# pandas 把缺失值 `str()` 之后的三种样子。小写比较，因为 `NaT` / `NaN` 的
# 大小写取决于是哪个版本、哪条路径转的。
_NULL_TEXT = frozenset({"nan", "none", "nat"})


def records(table: Any) -> list[dict]:
    """把一张表收成 `list[dict]`，收不成就是空列表。

    认三种形状：`None`（没拿到）、`list`（已经是行了）、任何带 `to_dict` 的
    对象（`DataFrame`）。列表里的非 dict 元素直接丢掉——那是上游把表头当成了
    数据行一类的错误，留着只会让下游 `row.get()` 抛 `AttributeError`。

    **不吞异常**，和被它替换掉的三份保持一致。取不到就当没有数据的那种语义要
    自带 `try`，不能靠这里——这里分不清「上游没给」和「上游给错了」。
    """
    if table is None:
        return []
    if isinstance(table, list):
        return [row for row in table if isinstance(row, dict)]
    to_dict = getattr(table, "to_dict", None)
    if callable(to_dict):
        return list(to_dict(orient="records"))
    return []


def text(row: dict | None, key: str, default: str = "") -> str:
    """取一个字符串字段，缺失与 pandas 的空值占位都收成 `default`。

    见模块注释：`"nan"` 这类占位是**非空字符串**，不挡在这里的话，下游任何
    `value or "未知"` 都会拿到 `"nan"` 而不是 `"未知"`。
    """
    if not row:
        return default
    value = row.get(key)
    if value is None:
        return default
    stripped = str(value).strip()
    return default if stripped.lower() in _NULL_TEXT else stripped
