"""板块横截面的公共底座：取一格数、对齐一张面板。

`rotation.py`（按价格读「谁在涨」）与 `turnover.py`（按成交额读「谁在被交易」）
是同一张面板的两种读法：输入同为 `ths_sector_index.load_all_series()` 的
`{code: (板块名, 日线)}`，输出同为一张按板块排的横截面榜。两边原来各写了一份
`_close` / `_amount` / `_pct` / `_round`，以及一份 35 行、除了 note 措辞之外
**逐字相同**的 `_align`。

这类重复在对齐这一步上代价格外高，因为它是整层唯一的正确性闸门，而且两条链路
被它挡住的是**不同严重程度的错误**：

```text
轮动榜   一个停更三天的板块，它的「5 日涨幅」跨的是别人 8 天的区间
         → 错的是它自己那一行的名次
成交额榜 占比的分母是当日全体板块的合计额，缺席的那几天分母整体偏小
         → 错的是**其余每一个板块**的占比
```

两份实现里只要有一份漏改，坏掉的不是一行是整张榜——而且是后面那种，坏得静悄悄
（每个板块的占比都只偏高一点点，没有任何一行看起来不对）。

**措辞不并进来。** `align()` 返回的是事实（留下几个、丢掉几个、对齐到哪天），
不返回 note 文本。「板块轮动：…」和「成交额迁移：…」是两层各自的词汇表，
连同「为什么丢」的解释一起留在各自的模块里——共用的是判据，不是说法。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ashare_decision_support.numeric import positive


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


def rounded(value: float | None, digits: int = 6) -> float | None:
    """落盘前收一次小数位，None 原样透传。

    收在这里是因为板块读数会整份序列化进简报与台账 JSON：不收的话，一个
    `0.030000000000000027` 会原样印进产物，而它和 `0.03` 在下一次 diff 里是
    两个不同的值。
    """
    return None if value is None else round(value, digits)
