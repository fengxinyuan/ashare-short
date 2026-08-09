"""基于同花顺静态文件的板块分类，替代 `taxonomy.py` 的 TickFlow universe 那条路。

这一层只做一件事：把 `adapters/ths_taxonomy.py` 的标的画像折成 `SectorDefinition`。
**产出的类型和 `TickFlowSwTaxonomy` 完全一样**，所以 `sectors/momentum.py`、
`sectors/predict.py`、`jobs/morning.py` 的板块计算一行都不用改——换的是数据来源，
不是板块层的契约。

两种分类并列，不是替代关系：

```text
ThsIndustryTaxonomy   申万一/二/三级   31 / 90 / 257   回答「这是什么生意」
ThsConceptTaxonomy    同花顺概念        321（过闸后）    回答「这是什么题材」
```

**超短线真正轮动的单位是概念不是行业。** 「半导体」「AI 算力」「存储芯片」在申万
里全都压在「电子」或「计算机」下面，而它们完全可以同一天一个涨停潮一个跌。行业
分类答不了这个问题，这正是概念这一路存在的理由。

概念这一路的闸门写在 `adapters/ths_taxonomy.py`：黑名单（融资融券、深股通、
国企改革这类**交易资格与政策口号**，不是主题）、上游空值（字面量 `nan`），
以及**只有下限没有上限**的规模闸门。被挡掉的标签仍然留在标的画像里照常印进简报。

概念之间基本正交，所以 31 + 321 个板块并排排名不会产出一堆重复读数：
2026-08-09 实测 321 个概念里 Jaccard ≥ 0.5 的只有两对（金属锌/金属铅 0.74、
网络游戏/手机游戏 0.58），不需要额外的去重层。
"""

from __future__ import annotations

from ashare_decision_support.adapters.ths_taxonomy import (
    CONCEPT_MIN_SIZE,
    LEVELS,
    ThsTaxonomySnapshot,
    ThsTaxonomySource,
)
from ashare_decision_support.sectors.base import SectorDefinition, SectorTaxonomy
from ashare_decision_support.sectors.taxonomy import SectorTaxonomyError

CONCEPT_LEVEL = "CONCEPT"

# 一个行业少于这么多成分股就不单列，与 `TickFlowSwTaxonomy` 的默认值一致。
MIN_CONSTITUENTS = 5


class _StaticTaxonomy(SectorTaxonomy):
    """共用的取数与缓存。子类只决定「怎么分组」。"""

    def __init__(self, source: ThsTaxonomySource | None = None) -> None:
        self.source = source or ThsTaxonomySource()
        self._snapshot: ThsTaxonomySnapshot | None = None
        self._cached: list[SectorDefinition] | None = None

    @property
    def snapshot(self) -> ThsTaxonomySnapshot:
        """整份标的画像。名称、行业路径、概念标签都从这里取。"""
        if self._snapshot is None:
            self._snapshot = self.source.load()
        return self._snapshot

    @property
    def notes(self) -> list[str]:
        return list(self.snapshot.notes)

    def list_sectors(self) -> list[SectorDefinition]:
        if self._cached is not None:
            return self._cached
        snapshot = self.snapshot
        if not snapshot.ok:
            # 不返回空列表。空列表在下游读起来是「今天没有板块」，而真相是
            # 「分类取不到」——这两件事该导向完全不同的动作。同 `taxonomy.py`。
            raise SectorTaxonomyError(
                "同花顺静态分类不可用且无缓存，无法产出板块定义："
                + "；".join(snapshot.notes)
            )
        self._cached = self._build(snapshot)
        return self._cached

    def _build(self, snapshot: ThsTaxonomySnapshot) -> list[SectorDefinition]:
        raise NotImplementedError


class ThsIndustryTaxonomy(_StaticTaxonomy):
    """申万一/二/三级行业。`level` 用 SW1 / SW2 / SW3，与既有参数保持一致。"""

    name = "ths-industry"

    def __init__(
        self,
        source: ThsTaxonomySource | None = None,
        level: str = "SW1",
        min_constituents: int = MIN_CONSTITUENTS,
    ) -> None:
        if level not in LEVELS:
            raise ValueError(f"unsupported level {level!r}; expected one of {sorted(LEVELS)}")
        super().__init__(source)
        self.level = level
        self.min_constituents = min_constituents

    def _build(self, snapshot: ThsTaxonomySnapshot) -> list[SectorDefinition]:
        groups = snapshot.industry_groups(self.level)
        definitions = [
            SectorDefinition(
                name=name,
                level=self.level,
                # 排序后再定序，让缓存与两次运行之间的成分股顺序稳定、可 diff。
                symbols=tuple(dict.fromkeys(sorted(symbols))),
            )
            for name, symbols in sorted(groups.items())
        ]
        return [item for item in definitions if item.size >= self.min_constituents]


class ThsConceptTaxonomy(_StaticTaxonomy):
    """同花顺概念板块。规模下限在这里生效，黑名单在 adapter 层就已经滤掉了。

    **没有规模上限。** 见 `adapters/ths_taxonomy.py:CONCEPT_MIN_SIZE` 上方那段：
    旧的 400 上限会把机器人概念、人形机器人、AI 应用、低空经济这一整批超短主战场
    全部挡在板块排序之外。「大概念涨等于大盘涨」这个担心由超额涨幅接管。

    `max_size=None` 即不设上限；传一个整数仍然生效，留给确实想收紧的调用方。
    """

    name = "ths-concept"

    def __init__(
        self,
        source: ThsTaxonomySource | None = None,
        min_size: int = CONCEPT_MIN_SIZE,
        max_size: int | None = None,
    ) -> None:
        super().__init__(source)
        self.min_size = min_size
        self.max_size = max_size
        self.excluded_by_size = 0

    def _build(self, snapshot: ThsTaxonomySnapshot) -> list[SectorDefinition]:
        groups = snapshot.concept_groups()
        kept: list[SectorDefinition] = []
        excluded = 0
        for name, symbols in sorted(groups.items()):
            if len(symbols) < self.min_size:
                excluded += 1
                continue
            if self.max_size is not None and len(symbols) > self.max_size:
                excluded += 1
                continue
            kept.append(
                SectorDefinition(
                    name=name,
                    level=CONCEPT_LEVEL,
                    symbols=tuple(dict.fromkeys(sorted(symbols))),
                )
            )
        self.excluded_by_size = excluded
        return kept


def reconcile(
    static_taxonomy: SectorTaxonomy, other: SectorTaxonomy, sample_limit: int = 5
) -> list[str]:
    """静态文件与 TickFlow 的一次性对账，产出 notes。**只报告，不修正。**

    静态文件已经是主分类，这一步存在的意义是：它由第三方维护、更新频率未知，
    而**过期的分类外观完全正常**。拿另一路独立来源比一次，是唯一能让「这份文件
    是不是已经陈旧了」变成可观测量的办法。

    对不上不抛错——两套分类的口径本来就不完全相同（TickFlow 缺北交所、缺片时会
    少成分股）。这里报出差异让人判断，自动「修正」只会把两个不确定的东西混成一个。
    """
    try:
        left = {item.name: set(item.symbols) for item in static_taxonomy.list_sectors()}
    except SectorTaxonomyError as exc:
        return [f"分类对账跳过：静态分类不可用（{exc}）"]
    try:
        right = {item.name: set(item.symbols) for item in other.list_sectors()}
    except Exception as exc:
        return [f"分类对账跳过：对照源不可用（{type(exc).__name__}）"]
    if not left or not right:
        return []

    left_symbols = {symbol for symbols in left.values() for symbol in symbols}
    right_symbols = {symbol for symbols in right.values() for symbol in symbols}
    notes = [
        (f"分类对账：静态 {len(left)} 个板块 / {len(left_symbols)} 只，"
        f"对照 {len(right)} 个板块 / {len(right_symbols)} 只")
    ]

    only_right = sorted(right_symbols - left_symbols)
    if only_right:
        # 这一条最要紧：对照源有、静态文件没有的标的，正是「静态文件过期了」的
        # 直接证据（新股上市后要等文件更新才会出现）。
        notes.append(
            f"⚠️ {len(only_right)} 只标的只在对照源里有，静态分类可能已过期："
            + "、".join(only_right[:sample_limit])
            + ("等" if len(only_right) > sample_limit else "")
        )

    missing_names = sorted(set(right) - set(left))
    if missing_names:
        notes.append(
            f"对照源有而静态分类没有的板块名 {len(missing_names)} 个："
            + "、".join(missing_names[:sample_limit])
        )
    return notes
