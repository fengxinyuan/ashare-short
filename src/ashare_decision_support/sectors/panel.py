"""板块历史序列的数值读取、日期面板对齐与统计工具。

价格轮动和成交额迁移共用对齐口径，避免缺失日期导致收益窗口或占比分母错位。
align 返回覆盖事实，展示文案由各调用方组织。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.numeric import positive

# 低于这个板块数就不给结论。横截面统计在小样本上毫无意义——20 个板块算出的秩
# 相关，随便两个板块换位就能翻号；占比更甚，样本太小时一个板块的异动就能改写
# 所有其他板块的占比。

# `rotation` / `turnover` / `seesaw` 三处原来各写了一份 `MIN_SECTORS = 20`，而且
# 后两份的注释各自写着「同 `rotation.py:MIN_SECTORS` 的理由与取值」——**注释里
# 互相指认，恰好说明它是一个旋钮而不是三个**。三份并存时，把其中一份调到 30
# 不会让另外两处报错，只会让那两句「同上」的注释从此是假的。收在这里，和
# `align()` 同一条理由：共用的是判据。

# **2026-08-28 收了第四份。** `sectors/backtest.py` 是这次收口之后才加的，它又
# 自己写了一遍 `MIN_SECTORS = 20`，用在三处横截面下限上（信号数、面板行数、
# 市场收益样本数）——和上面三处是同一个判据。这说明「收一次」不等于收干净：
# 常量收进来了，**但没有任何闸门拦住下一个文件再写一份**，而它照样全绿。
# 查法在 `tests/test_sector_panel.py`：判据只有一处定义，靠测试守，不靠注释。
MIN_SECTORS = 20

# 横截面读数交给外部模型看时，名单类列表各留几条。结论与观察是短句，多留几条
# 就是多几句话；`sectors` 是整张榜，一份轮动读数里 90 行结构相同的板块会把
# 真正的结论淹掉，整块不带。
COMPACT_KEEP_NOTES = 3
COMPACT_KEEP_ROWS = 5


def compact_reading(value: Any) -> Any:
    """压缩横截面读数：省略完整 sectors 榜，其余列表按类别保留前几项。"""
    if not isinstance(value, dict):
        return value
    compact: dict[str, Any] = {}
    for key, item in value.items():
        if key == "sectors":
            continue
        if isinstance(item, list):
            keep = COMPACT_KEEP_NOTES if key in {"observations", "notes"} else COMPACT_KEEP_ROWS
            compact[key] = item[:keep]
        else:
            compact[key] = item
    return compact


def bar_value(bar: dict, key: str) -> float | None:
    """取日线里的一格数，要求**有限且为正**，否则 None。

    正数是这一层的硬要求，不是保守：收盘价与成交额取 0 的含义都是「这天没取到」
    ——板块指数不会真的跌到 0，停牌的板块是没有这一行。放一个 0 进去，`_pct`
    会拿它当基期算出 -100%，而占比会拿它当分母。

    走 `numeric.positive` 而不是自己写 isinstance 判断：序列是从 JSON 读回来的，
    上游哪天把数字写成字符串（`"12.5"`），isinstance 那份会把**整个板块**判为
    序列不够长而静默剔除——它不会报错，只会让榜上少一行。
    """
    return positive(bar.get(key))


@dataclass
class Alignment:
    """`align()` 的产物：留下了哪些板块，以及为了留下它们丢掉了什么。

    丢掉的数量单独带出来，是为了让调用方能把它写进 note。**一次静默的剔除和一次
    带说明的剔除，在读简报的人那里是两件事**：前者只会让人觉得「今天板块怎么这么
    少」，后者能直接看出是数据停更还是真的没有序列。
    """

    rows: list[tuple[str, str, list[dict]]] = field(default_factory=list)
    latest: str = ""
    total: int = 0
    """入参里的板块总数（含序列不够长的）。"""
    long_enough: int = 0
    """序列够长的板块数。`long_enough - len(rows)` 就是因为最新交易日对不齐而丢掉的。"""

    @property
    def dropped(self) -> int:
        """够长但最新交易日不是多数派、因而被剔除的板块数。"""
        return self.long_enough - len(self.rows)

    def __bool__(self) -> bool:
        return bool(self.rows)


def align(
    series: dict[str, tuple[str, list[dict]]] | None,
    min_bars: int,
    keys: tuple[str, ...] = ("close",),
) -> Alignment:
    """留下序列够长、且最新交易日与多数派一致的板块。

    两步各挡一类问题：

    1. **逐格清洗**——`keys` 里任意一格取不到值的那一根日线整根丢掉。丢整根而不是
       补值，是因为占比与涨幅都要求同一根 bar 上的几个字段互相对得上；
    2. **按多数派对齐**——以出现次数最多的最新交易日为准（并列时取字典序较大的，
       也就是较晚的那天），其余板块整体剔除。

    取多数派而不是取全局最大日期：真正停更的是少数，而少数派里只要有一个板块的
    日期串串到了未来（上游偶发的脏数据），取最大就会把**全部正常板块**判成过期。
    """
    kept: list[tuple[str, str, list[dict]]] = []
    for code, payload in (series or {}).items():
        name, bars = payload
        clean = [bar for bar in bars if all(bar_value(bar, key) is not None for key in keys)]
        if len(clean) >= min_bars:
            kept.append((code, name, clean))
    if not kept:
        return Alignment(total=len(series or {}))

    stamps: dict[str, int] = {}
    for _code, _name, bars in kept:
        stamp = str(bars[-1]["date"])
        stamps[stamp] = stamps.get(stamp, 0) + 1
    latest = max(stamps, key=lambda key: (stamps[key], key))
    return Alignment(
        rows=[item for item in kept if str(item[2][-1]["date"]) == latest],
        latest=latest,
        total=len(series or {}),
        long_enough=len(kept),
    )


def closes(bars: list[dict], key: str = "close") -> list[float]:
    """把一段已对齐的日线摊成一列数。

    只在 `align()` 之后调用：那时每一根都已经过 `bar_value`，所以这里直接取值，
    长度与 `bars` 严格相等——`_pct` 靠下标回看第 N 根，序列一旦被静默压缩，
    「近 5 日涨幅」跨的就不是 5 天。
    """
    return [float(bar[key]) for bar in bars]


def pct(values: list[float], window: int) -> float | None:
    """近 `window` 根的涨跌幅。样本不够或基期非正时返回 None。

    和 `signals/series.py:change` 是同一个算式，没有并过去：那边收的是**个股**
    日线，入参先过 `series()` 跳过取不到值的根，长度因此可能小于 bars；这边的
    入参来自 `closes()`，长度与 bars 严格相等。两边的「近 5 根」不是同一个 5 根，
    合并会让其中一边的窗口含义悄悄改变。
    """
    if len(values) < window + 1:
        return None
    previous = values[-(window + 1)]
    if previous <= 0:
        return None
    return values[-1] / previous - 1.0


def pearson(left: list[float], right: list[float]) -> float | None:
    """两列等长读数的皮尔逊相关。任一侧方差为 0 时返回 None，**不返回 0**。

    方差为 0 的含义是「这一列整段没有变化」，那是数据问题；返回 0 会让它读起来
    像「实测无关」，而那两件事该导向不同的动作。同 `numeric` 那一层对「拿不到」
    与「等于零」的区分。

    `rotation.py` 与 `seesaw.py` 原来各写了一份这段协方差——前者叫 `_spearman`
    （对秩求相关），后者叫 `_pearson`（对涨跌幅求相关）。**两个名字掩盖了它们是
    同一段算式**：斯皮尔曼的定义就是「先转秩、再求皮尔逊」，所以那两份实现里
    真正不同的只有「进来的是不是秩」和「样本数下限是几」，都在调用方那一侧。

    两份并存的代价和 `MIN_SECTORS` 一样是静默的：给其中一份补一条判据（比如
    方差为 0 时记一条 note、或者改成样本方差），另一条链路会在同样的输入上保持
    沉默，而两条都不报错。

    **只算一次开方（`(var_l * var_r) ** 0.5`）**，取自 `seesaw` 那份：跷跷板要扫
    C(n,2) 对板块，是两处里唯一的热路径。与 `rotation` 原先的两次开方在最后一位
    上可能差一个 ULP，而两处的产物都过 `rounded(..., 4)`，落盘的数一个字不变。
    """
    size = len(left)
    if size < 2 or size != len(right):
        return None
    mean_left = sum(left) / size
    mean_right = sum(right) / size
    cov = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True))
    var_left = sum((a - mean_left) ** 2 for a in left)
    var_right = sum((b - mean_right) ** 2 for b in right)
    if var_left <= 0.0 or var_right <= 0.0:
        return None
    return cov / (var_left * var_right) ** 0.5


def ranks(values: list[float]) -> list[float]:
    """平均秩，并列取均值。

    并列不处理的话，一堆同值会按输入顺序拿到不同的秩，让相关系数带上一点与数据
    无关的信息。

    和 `pearson()` 一起收在这里，是因为「转秩再求相关」是同一件事的两半：秩留在
    `rotation` 里而相关收到这里，下一个要算秩相关的调用方仍然只能再抄一份秩。
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        average = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            result[order[position]] = average
        index = end + 1
    return result


def rounded(value: float | None, digits: int = 6) -> float | None:
    """落盘前收一次小数位，None 原样透传。

    收在这里是因为板块读数会整份序列化进简报与台账 JSON：不收的话，一个
    `0.030000000000000027` 会原样印进产物，而它和 `0.03` 在下一次 diff 里是
    两个不同的值。
    """
    return None if value is None else round(value, digits)
