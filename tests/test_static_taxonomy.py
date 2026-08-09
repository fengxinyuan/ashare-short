"""静态板块分类的两条硬约定，以及那个「文件过期了没有」的探针。

板块分类是整条盘前链路的**第一步**：它错了，后面的动量、排序、选股全都建立在
一个错的成分股集合上，而且不会有任何一步报错。这个文件测的就是这一层最容易
静默出错的三件事。

`sectors/ths_taxonomy.py` 此前覆盖率 64%，未覆盖的正好是 `reconcile` 整个函数
和概念规模闸门——前者是**唯一**能让「这份第三方文件是不是已经陈旧」变成可观测
量的东西（见该函数 docstring），后者决定 321 个概念里哪些进排名。
"""

from __future__ import annotations

import pytest

from ashare_decision_support.adapters.ths_taxonomy import SymbolProfile, ThsTaxonomySnapshot
from ashare_decision_support.sectors import SectorTaxonomyError
from ashare_decision_support.sectors.ths_taxonomy import (
    ThsConceptTaxonomy,
    ThsIndustryTaxonomy,
    reconcile,
)


class _Source:
    """替身分类源：直接交出一个快照。"""

    def __init__(self, snapshot: ThsTaxonomySnapshot) -> None:
        self.snapshot = snapshot
        self.loads = 0

    def load(self) -> ThsTaxonomySnapshot:
        self.loads += 1
        return self.snapshot


def _profile(symbol: str, industry: str, concepts: tuple[str, ...] = ()) -> SymbolProfile:
    return SymbolProfile(
        symbol=symbol, name=f"票{symbol[:3]}", industries=(industry, "二级", "三级"), concepts=concepts
    )


def _snapshot(profiles: list[SymbolProfile], ok: bool = True, notes=None) -> ThsTaxonomySnapshot:
    return ThsTaxonomySnapshot(
        profiles={p.symbol: p for p in profiles}, ok=ok, notes=list(notes or [])
    )


def test_unavailable_classification_raises_instead_of_reporting_no_sectors() -> None:
    """**取不到分类**和**今天没有板块**必须是两件事。

    返回空列表在下游读起来是后者：`morning` 会照常往下跑，得出一份「没有板块
    值得做」的结论，而真相是这一步根本没拿到数据。两者该导向完全不同的动作，
    所以这里抛错——同 `taxonomy.py` 对 TickFlow 那一路的处置。
    """
    taxonomy = ThsIndustryTaxonomy(_Source(_snapshot([], ok=False, notes=["文件下载失败"])))

    with pytest.raises(SectorTaxonomyError, match="文件下载失败"):
        taxonomy.list_sectors()


def test_industry_groups_are_ordered_and_deduplicated() -> None:
    """成分股顺序必须稳定，否则缓存和两次运行之间没法 diff。"""
    profiles = [_profile(f"00000{i}.SZ", "电子") for i in range(6)]
    profiles.append(_profile("000005.SZ", "电子"))  # 重复标的
    taxonomy = ThsIndustryTaxonomy(_Source(_snapshot(profiles)), min_constituents=2)

    sectors = taxonomy.list_sectors()

    assert [s.name for s in sectors] == ["电子"]
    symbols = sectors[0].symbols
    assert list(symbols) == sorted(set(symbols))


def test_a_sector_below_the_floor_is_dropped_not_kept_as_a_thin_one() -> None:
    """成分股太少的板块，中位涨幅是噪声，不该和几百只的板块并排排名。"""
    profiles = [_profile(f"00000{i}.SZ", "电子") for i in range(6)]
    profiles += [_profile("000100.SZ", "冷门行业"), _profile("000101.SZ", "冷门行业")]
    taxonomy = ThsIndustryTaxonomy(_Source(_snapshot(profiles)), min_constituents=5)

    assert [s.name for s in taxonomy.list_sectors()] == ["电子"]


def test_the_snapshot_is_loaded_once_and_cached() -> None:
    """分类是整条链路的第一步，重复 load 会把文件读取乘上调用次数。"""
    source = _Source(_snapshot([_profile(f"00000{i}.SZ", "电子") for i in range(6)]))
    taxonomy = ThsIndustryTaxonomy(source, min_constituents=2)

    taxonomy.list_sectors()
    taxonomy.list_sectors()

    assert source.loads == 1


def test_concept_size_gate_reports_how_many_it_dropped() -> None:
    """被规模闸门挡掉的数量要能查——否则「概念只剩这些」看不出是闸门还是数据。"""
    profiles = [
        _profile(f"0000{i:02d}.SZ", "电子", concepts=("AI算力",)) for i in range(8)
    ]
    profiles += [_profile("000090.SZ", "电子", concepts=("小众概念",))]
    taxonomy = ThsConceptTaxonomy(_Source(_snapshot(profiles)), min_size=5)

    sectors = taxonomy.list_sectors()

    assert [s.name for s in sectors] == ["AI算力"]
    assert taxonomy.excluded_by_size == 1


def test_concept_upper_bound_is_optional_and_off_by_default() -> None:
    """`max_size=None` 即不设上限。设上限是调用方的选择，不是默认行为。

    默认不设上限是有意的：一个 800 只的概念（比如「融资融券」那类）已经在
    adapter 层的黑名单里挡掉了，剩下的大概念仍然是真题材。
    """
    profiles = [
        _profile(f"0000{i:02d}.SZ", "电子", concepts=("大概念",)) for i in range(12)
    ]
    source = _Source(_snapshot(profiles))

    assert [s.name for s in ThsConceptTaxonomy(source, min_size=5).list_sectors()] == ["大概念"]
    assert ThsConceptTaxonomy(_Source(_snapshot(profiles)), min_size=5, max_size=10).list_sectors() == []


# ---- reconcile：文件过期是「外观完全正常」的那种失效 -----------------------


class _Static:
    def __init__(self, sectors) -> None:
        self._sectors = sectors

    def list_sectors(self):
        return self._sectors


class _Broken:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def list_sectors(self):
        raise self.exc


def _definition(name: str, symbols: tuple[str, ...]):
    from ashare_decision_support.sectors.base import SectorDefinition

    return SectorDefinition(name=name, level="SW1", symbols=symbols)


def test_reconcile_flags_symbols_only_the_other_source_knows_about() -> None:
    """对照源有、静态文件没有的标的，就是「文件过期了」的直接证据。

    新股上市之后要等第三方更新文件才会出现，而在那之前静态分类**外观完全正常**：
    板块数对、成分股有、链路照跑。只有拿另一路独立来源比一次才看得见。
    """
    static = _Static([_definition("电子", ("000001.SZ", "000002.SZ"))])
    other = _Static([_definition("电子", ("000001.SZ", "000002.SZ", "301999.SZ"))])

    notes = reconcile(static, other)

    assert any("301999.SZ" in note and "可能已过期" in note for note in notes)


def test_reconcile_only_reports_and_never_repairs() -> None:
    """两套分类口径本来就不同，自动「修正」只会把两个不确定的东西混成一个。"""
    static_sectors = [_definition("电子", ("000001.SZ",))]
    static = _Static(static_sectors)
    other = _Static([_definition("电子", ("000001.SZ",)), _definition("北交所行业", ("830001.BJ",))])

    notes = reconcile(static, other)

    # 报出来了……
    assert any("北交所行业" in note for note in notes)
    # ……但静态分类一个字都没被改。
    assert static.list_sectors() == static_sectors


def test_reconcile_degrades_to_a_note_when_either_side_is_unavailable() -> None:
    """对账本身是旁证，它取不到不能中断盘前链路。"""
    good = _Static([_definition("电子", ("000001.SZ",))])

    static_down = reconcile(_Broken(SectorTaxonomyError("静态文件没了")), good)
    other_down = reconcile(good, _Broken(ConnectionError("对照源超时")))

    assert any("静态分类不可用" in note for note in static_down)
    assert any("对照源不可用" in note and "ConnectionError" in note for note in other_down)


def test_reconcile_says_nothing_when_there_is_nothing_to_compare() -> None:
    assert reconcile(_Static([]), _Static([_definition("电子", ("000001.SZ",))])) == []
