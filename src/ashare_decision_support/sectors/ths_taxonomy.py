"""将同花顺静态画像转换为行业、概念两种板块定义。

行业只有一个层级，概念是可重叠的题材标签；两者分别排名和选取。成分股关系
用于宽度统计、板块内选股及持仓归属，报告只展示少量代表股票。

概念数量动态产生，过滤规则由 adapters/ths_taxonomy.py 提供。主题重叠由
sectors/clusters.py 与板块预测链路处理。
"""

from __future__ import annotations

from ashare_decision_support.adapters.ths_taxonomy import (
    CONCEPT_MIN_SIZE,
    ThsTaxonomySnapshot,
    ThsTaxonomySource,
)
from ashare_decision_support.sectors.base import (
    LEVEL_CONCEPT,
    LEVEL_INDUSTRY,
    SectorDefinition,
    SectorTaxonomy,
    SectorTaxonomyError,
)

# 成分过少时不单列，避免个别股票主导板块读数。
MIN_CONSTITUENTS = 5


class _StaticTaxonomy(SectorTaxonomy):
    """共用的取数与缓存。子类只决定「怎么分组」。"""

    def __init__(self, source: ThsTaxonomySource | None = None) -> None:
        self.source = source or ThsTaxonomySource()
        self._snapshot: ThsTaxonomySnapshot | None = None
        self._cached: list[SectorDefinition] | None = None

    @property
    def snapshot(self) -> ThsTaxonomySnapshot:
        """整份标的画像。名称、行业、概念标签都从这里取。"""
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
            # 「分类取不到」——这两件事该导向完全不同的动作。
            raise SectorTaxonomyError(
                "同花顺静态分类不可用且无缓存，无法产出板块定义：" + "；".join(snapshot.notes)
            )
        self._cached = self._build(snapshot)
        return self._cached

    def _build(self, snapshot: ThsTaxonomySnapshot) -> list[SectorDefinition]:
        raise NotImplementedError


class ThsIndustryTaxonomy(_StaticTaxonomy):
    """同花顺行业；成分股用于宽度、板块内选股和持仓归属。"""

    name = "ths-industry"

    def __init__(
        self,
        source: ThsTaxonomySource | None = None,
        min_constituents: int = MIN_CONSTITUENTS,
    ) -> None:
        super().__init__(source)
        self.min_constituents = min_constituents

    def _build(self, snapshot: ThsTaxonomySnapshot) -> list[SectorDefinition]:
        groups = snapshot.industry_groups()
        definitions = [
            SectorDefinition(
                name=name,
                level=LEVEL_INDUSTRY,
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
                    level=LEVEL_CONCEPT,
                    symbols=tuple(dict.fromkeys(sorted(symbols))),
                )
            )
        self.excluded_by_size = excluded
        return kept


def static_taxonomies(
    source: ThsTaxonomySource | None = None,
) -> tuple[ThsIndustryTaxonomy, ThsConceptTaxonomy]:
    """构造共用 ThsTaxonomySource 的行业和概念分类，保证两层读取同一份快照。"""
    shared = source or ThsTaxonomySource()
    return ThsIndustryTaxonomy(source=shared), ThsConceptTaxonomy(source=shared)
