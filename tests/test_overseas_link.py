"""外盘 → A 股板块映射。

**最要紧的是 `test_every_target_exists_in_the_real_taxonomy`。** 映射表的目标名
是手写的中文板块名，写错一个字（比如把「港口航运」写成「航运港口」）不会报错、
不会抛异常，只会让那条映射**静默地永远匹配不上任何板块**——而这套系统里最贵的
错误一直是这一类。
"""

import json
from pathlib import Path

import pytest

from ashare_decision_support.adapters.global_market import (
    SINA_INSTRUMENTS,
    GlobalMarketReading,
    GlobalMarketSnapshot,
)
from ashare_decision_support.sectors.overseas_link import (
    BEARISH,
    BULLISH,
    MAPPINGS,
    STRENGTH_NOTABLE,
    STRENGTH_STRONG,
    THRESHOLDS,
    UNMAPPED,
    Mapping,
    read_overseas_links,
)

# 真实分类缓存。**刻意绕开 conftest 的 ASHARE_DS_HOME 隔离**——这条断言要校的正是
# 线上那份分类，用夹具校等于把 taxonomy 抄第二份，而两份会各自演化。
LIVE_TAXONOMY = Path.home() / ".ashare-ds" / "taxonomy" / "ths_profiles.json"


def _snapshot(*rows: tuple[str, str, float | None]) -> GlobalMarketSnapshot:
    return GlobalMarketSnapshot(
        readings=[
            GlobalMarketReading(
                key=key,
                name=name,
                symbol=f"x_{key}",
                source="sina",
                last=100.0,
                change_pct=change,
                as_of="2026-08-20 09:48:02",
            )
            for key, name, change in rows
        ]
    )


def test_every_target_exists_in_the_real_taxonomy() -> None:
    """映射表里的每个板块名都必须在同花顺分类里真实存在。"""
    if not LIVE_TAXONOMY.exists():
        pytest.skip(f"没有本机分类缓存（{LIVE_TAXONOMY}），跳过实名校验")

    profiles = json.loads(LIVE_TAXONOMY.read_text(encoding="utf-8"))["profiles"]
    known: set[str] = set()
    for row in profiles:
        known.update(row.get("industries") or [])
        known.update(row.get("concepts") or [])

    unknown = {
        target
        for mapping in MAPPINGS
        for target in mapping.targets
        if target not in known
    }
    assert not unknown, f"这些板块名在分类里不存在，映射会永远匹配不上：{sorted(unknown)}"


def test_live_and_history_instrument_tables_agree_on_shared_keys() -> None:
    """实时快照表与历史序列表**必须对同一个 key 指同一个品种**。

    两张表分开是对的——`adapters/global_market.py` 取当日快照走 `hq.sinajs.cn`，
    `adapters/overseas_history.py` 取历史日线走另外两个端点，symbol 格式完全不同。
    但基准率层要拿今天的读数去查历史分布，join 的键就是这个 key：拼写漂了一个字
    母，样本数会恒为 0，**而且两边都不报错**。

    只校交集：历史表只覆盖 7 个指数，实时表还有 ETF 与期货，那些暂时没有历史。
    """
    from ashare_decision_support.adapters.overseas_history import INSTRUMENTS

    shared = set(SINA_INSTRUMENTS) & set(INSTRUMENTS)
    assert shared, "两张表没有任何共同 key——基准率层将无从对接"

    for key in sorted(shared):
        live_name = SINA_INSTRUMENTS[key][1]
        history_name = INSTRUMENTS[key].name
        assert live_name == history_name, (
            f"{key} 在两张表里的名字不一致：实时={live_name!r} 历史={history_name!r}"
        )

    # 历史表里有、实时表里没有的 key，意味着能查基准率但拿不到今天的读数——
    # 那条链路是断的，值得在这里报出来而不是等到用的时候。
    orphaned = set(INSTRUMENTS) - set(SINA_INSTRUMENTS)
    assert not orphaned, f"这些品种有历史序列但没有当日读数：{sorted(orphaned)}"


def test_every_instrument_is_either_mapped_or_explicitly_unmapped() -> None:
    """品种表加了新东西时，必须在这里表态——映射它，或写明为什么不映射。

    不加这条断言的话，「在品种表里但不在映射表里」既可能是决定不映射，也可能是
    漏了，而读代码的人分不出来。
    """
    mapped = {mapping.instrument for mapping in MAPPINGS}
    covered = mapped | set(UNMAPPED)
    missing = set(SINA_INSTRUMENTS) - covered

    assert not missing, f"这些品种既没映射也没说明不映射的理由：{sorted(missing)}"
    assert not (mapped & set(UNMAPPED)), "同一个品种不能既被映射又被列为不映射"


def test_mappings_only_reference_instruments_that_exist() -> None:
    unknown = {m.instrument for m in MAPPINGS} - set(SINA_INSTRUMENTS)
    assert not unknown, f"映射表引用了品种表里没有的 key：{sorted(unknown)}"


def test_thresholds_are_ordered_and_only_cover_real_instruments() -> None:
    assert not set(THRESHOLDS) - set(SINA_INSTRUMENTS)
    for key, (notable, strong) in THRESHOLDS.items():
        assert 0 < notable < strong, f"{key} 的档位不是递增的"


def test_move_below_the_bar_is_quiet_not_a_link() -> None:
    """没到门槛的品种进 quiet，而且 ok 仍然为真。

    隔夜全球平静是一个合法结论。用 `links` 判 ok 会让「今晚没事」和「取数挂了」
    变成同一个状态，而那两件事对开盘预案是相反的信息。
    """
    read = read_overseas_links(_snapshot(("sox", "费城半导体", 0.4)))

    assert read.links == []
    assert read.quiet == ["费城半导体"]
    assert read.missing == []
    assert read.ok is True
    assert any("隔夜平静，不是没取到" in note for note in read.notes)


def test_missing_reading_is_not_the_same_as_a_quiet_one() -> None:
    read = read_overseas_links(_snapshot(("sox", "费城半导体", None)))

    assert read.missing == ["费城半导体"]
    assert read.quiet == []
    assert read.ok is False


def test_strength_has_two_bands() -> None:
    notable, strong = THRESHOLDS["sox"]

    weak = read_overseas_links(_snapshot(("sox", "费城半导体", -(notable + 0.1))))
    loud = read_overseas_links(_snapshot(("sox", "费城半导体", -(strong + 0.1))))

    assert weak.links[0].strength == STRENGTH_NOTABLE
    assert loud.links[0].strength == STRENGTH_STRONG


def test_direction_follows_the_move_for_same_sign_mappings() -> None:
    down = read_overseas_links(_snapshot(("sox", "费城半导体", -2.12)))
    up = read_overseas_links(_snapshot(("sox", "费城半导体", 2.12)))

    assert down.links[0].direction == BEARISH
    assert up.links[0].direction == BULLISH
    assert "半导体" in down.links[0].targets


def test_a_negative_sign_flips_the_direction_on_the_same_reading() -> None:
    """油价涨：石油石化利多、航空利空。**同一个读数，两个方向。**"""
    read = read_overseas_links(_snapshot(("wti", "WTI原油", 4.5)))

    upstream = next(item for item in read.links if "石油石化" in item.targets)
    airline = next(item for item in read.links if "航空运输" in item.targets)

    assert upstream.direction == BULLISH
    assert airline.direction == BEARISH
    assert upstream.change_pct == airline.change_pct == 4.5


def test_by_sector_keeps_conflicting_links_instead_of_netting_them() -> None:
    """一个板块被多条链路指向时，返回清单而不是净方向。

    压成一个符号就把分歧本身丢掉了，而分歧恰恰是要给人看的东西。
    """
    read = read_overseas_links(
        _snapshot(("sox", "费城半导体", -3.5), ("kospi", "KOSPI", 4.0))
    )
    semis = read.by_sector()["半导体"]

    assert len(semis) == 2
    assert {item.direction for item in semis} == {BULLISH, BEARISH}


def test_links_sort_strong_first() -> None:
    read = read_overseas_links(
        _snapshot(("sox", "费城半导体", -1.6), ("gold", "COMEX黄金", -2.5))
    )

    assert [item.strength for item in read.links] == [STRENGTH_STRONG, STRENGTH_NOTABLE]


def test_empty_snapshot_says_so_and_does_not_raise() -> None:
    read = read_overseas_links(GlobalMarketSnapshot())

    assert read.ok is False
    assert read.links == []
    assert "没有海外读数" in read.notes[0]


def test_notes_never_claim_a_prediction() -> None:
    """这一层的输出是陈述句。措辞闸门在别处，但这条纪律要在本层就守住。"""
    read = read_overseas_links(_snapshot(("sox", "费城半导体", -3.5)))
    text = " ".join(read.notes) + " " + read.links[0].headline

    assert "不是对A股次日走势的预测" in " ".join(read.notes)
    for word in ("将会", "预计", "明天会", "必然"):
        assert word not in text


def test_custom_thresholds_are_honoured() -> None:
    read = read_overseas_links(
        _snapshot(("sox", "费城半导体", 0.4)), thresholds={"sox": (0.1, 0.2)}
    )

    assert read.links and read.links[0].strength == STRENGTH_STRONG


def test_unknown_instrument_falls_back_to_the_default_threshold() -> None:
    read = read_overseas_links(
        _snapshot(("made_up", "自造品种", 9.9)),
        mappings=(Mapping("made_up", ("半导体",), 1, "测试用"),),
    )

    assert read.links[0].targets == ("半导体",)
