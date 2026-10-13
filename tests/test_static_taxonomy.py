"""静态板块分类：可用性、稳定分组、缓存与概念规模门槛。"""

from __future__ import annotations

import json

import pytest

from ashare_decision_support.adapters.security_master import SecurityMasterAdapter
from ashare_decision_support.adapters.ths_taxonomy import (
    CACHE_SCHEMA,
    LEGACY_CACHE_SCHEMA,
    SymbolProfile,
    ThsTaxonomySnapshot,
    ThsTaxonomySource,
)
from ashare_decision_support.jsoncache import write_stamped
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.sectors import LEVEL_CONCEPT, LEVEL_INDUSTRY, SectorTaxonomyError
from ashare_decision_support.sectors.intraday import default_taxonomies
from ashare_decision_support.sectors.ths_taxonomy import (
    ThsConceptTaxonomy,
    ThsIndustryTaxonomy,
    static_taxonomies,
)
from tests.fakes import json_response


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
        symbol=symbol,
        name=f"票{symbol[:3]}",
        industry=industry,
        concepts=concepts,
    )


def _snapshot(profiles: list[SymbolProfile], ok: bool = True, notes=None) -> ThsTaxonomySnapshot:
    return ThsTaxonomySnapshot(
        profiles={p.symbol: p for p in profiles}, ok=ok, notes=list(notes or [])
    )


def test_unavailable_classification_raises_instead_of_reporting_no_sectors() -> None:
    """**取不到分类**和**今天没有板块**必须是两件事。

    返回空列表在下游读起来是后者：`morning` 会照常往下跑，得出一份「没有板块
    值得做」的结论，而真相是这一步根本没拿到数据。两者该导向完全不同的动作，
    所以这里抛错并保留数据源失败的原因。
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
    assert sectors[0].level == LEVEL_INDUSTRY
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
    profiles = [_profile(f"0000{i:02d}.SZ", "电子", concepts=("AI算力",)) for i in range(8)]
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
    profiles = [_profile(f"0000{i:02d}.SZ", "电子", concepts=("大概念",)) for i in range(12)]
    source = _Source(_snapshot(profiles))

    assert [s.name for s in ThsConceptTaxonomy(source, min_size=5).list_sectors()] == ["大概念"]
    assert (
        ThsConceptTaxonomy(_Source(_snapshot(profiles)), min_size=5, max_size=10).list_sectors()
        == []
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (["电子", " 半导体 ", "集成电路"], "半导体"),
        (["", "半导体", "集成电路"], "半导体"),
        (["电子"], None),
        (["电子", "", "集成电路"], None),
        (["电子", None, "集成电路"], None),
        (["电子", "nan", "集成电路"], None),
        (["电子", {"name": "半导体"}, "集成电路"], None),
        ("电子·半导体·集成电路", None),
    ],
)
def test_taxonomy_and_metadata_project_the_same_industry_without_shifting_empty_slots(
    tmp_path, path, expected
):
    files = {
        "industries.json": [{"symbol": "600000", "name": "测试", "industries": path}],
        "concepts.json": [{"symbol": "600000", "concepts": ["先进封装"]}],
    }
    source = ThsTaxonomySource(
        opener=lambda request, timeout: json_response(files[request.full_url.rsplit("/", 1)[-1]]),
        cache_dir=tmp_path,
    )
    snapshot = source.load(force_refresh=True)
    master = SecurityMasterAdapter(
        opener=lambda url, timeout: json.dumps(files[url.rsplit("/", 1)[-1]]),
        cache_path=tmp_path / "master.json",
    ).load()

    assert snapshot.profiles["600000.SH"].industry == master.get("600000").industry == expected
    assert snapshot.profiles["600000.SH"].concepts == master.get("600000").concepts == ("先进封装",)
    sectors = ThsIndustryTaxonomy(source, min_constituents=1).list_sectors()
    assert [item.name for item in sectors] == ([expected] if expected else [])
    for filename, key in (("ths_profiles.json", "profiles"), ("master.json", "entries")):
        row = json.loads((tmp_path / filename).read_text(encoding="utf-8"))[key][0]
        assert row["industry"] == expected
        assert "industries" not in row


def test_legacy_caches_remain_readable_as_one_industry_and_preserve_concept_only_stocks(tmp_path):
    rows = [
        {"symbol": "600000", "name": "测试", "industries": ["电子", "半导体", "集成电路"]},
        {"symbol": "600001", "name": "无行业", "concepts": ["先进封装"]},
    ]
    assert write_stamped(
        tmp_path / "ths_profiles.json",
        {"profiles": rows, "fetched_on": str(beijing_today())},
        LEGACY_CACHE_SCHEMA,
    )
    assert write_stamped(tmp_path / "master.json", {"entries": rows}, 1)
    calls = []

    def no_fetch(*args, **kwargs):
        calls.append(args)
        raise AssertionError("旧缓存转换不需要网络")

    source = ThsTaxonomySource(cache_dir=tmp_path, opener=no_fetch)
    snapshot = source.load()
    master = SecurityMasterAdapter(cache_path=tmp_path / "master.json", opener=no_fetch).cached()

    assert snapshot.from_cache and snapshot.ok and master.ok
    assert snapshot.profiles["600000.SH"].industry == master.get("600000").industry == "半导体"
    assert snapshot.profiles["600001.SH"].industry is master.get("600001").industry is None
    assert snapshot.profiles["600001.SH"].concepts == master.get("600001").concepts == ("先进封装",)
    assert calls == []
    assert source._write_cache(snapshot)
    assert (
        json.loads((tmp_path / "ths_profiles.json").read_text(encoding="utf-8"))["schema"]
        == CACHE_SCHEMA
    )
    assert (
        ThsTaxonomySource(cache_dir=tmp_path, opener=no_fetch).load().profiles == snapshot.profiles
    )


def test_morning_and_intraday_use_identical_industry_and_concept_constituents():
    profiles = [_profile(f"6000{i:02d}.SH", "半导体", ("先进封装",)) for i in range(20)]
    source = _Source(_snapshot(profiles))
    industry, concept = static_taxonomies(source)
    intraday = default_taxonomies(source)

    assert set(intraday) == {LEVEL_INDUSTRY, LEVEL_CONCEPT}
    assert industry.list_sectors() == intraday[LEVEL_INDUSTRY].list_sectors()
    assert concept.list_sectors() == intraday[LEVEL_CONCEPT].list_sectors()
    assert all(item.source is source for item in (industry, concept, *intraday.values()))
