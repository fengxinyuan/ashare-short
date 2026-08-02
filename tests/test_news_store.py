"""语料库测试。

这一层的价值全在「跨越单次运行」上，所以每条测试都要模拟至少两次采集——
只跑一次的话，它和临时缓存没有任何区别。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.news.store import NewsStore

TODAY = date(2026, 8, 3)
MORNING = datetime(2026, 8, 3, 8, 0, 0)


def doc(title: str, source: str = "cls", published: str = "", **kw) -> NewsDoc:
    return NewsDoc(title=title, source=source, published_at=published, **kw)


def store(tmp_path) -> NewsStore:
    return NewsStore(tmp_path / "corpus.sqlite3")


# ---- first_seen：这个模块存在的理由 ---------------------------------------


def test_first_seen_survives_a_second_collection_pass(tmp_path) -> None:
    """重复见到同一条消息，first_seen 必须停在第一次。

    这是整个模块的核心断言。如果 first_seen 跟着最后一次采集走，那么一条播了
    三天的旧闻在每天早上都会显示成「刚出现」——正好把这个字段要区分的两种情况
    弄反。
    """
    corpus = store(tmp_path)
    corpus.ingest([doc("央行开展 5000 亿元逆回购")], TODAY, now=MORNING)
    corpus.ingest([doc("央行开展 5000 亿元逆回购")], TODAY, now=MORNING + timedelta(hours=6))

    stored = corpus.recent(now=MORNING + timedelta(hours=6))
    assert len(stored) == 1, "同一条消息不该在库里变成两条"
    assert stored[0].first_seen_at == MORNING
    assert stored[0].last_seen_at == MORNING + timedelta(hours=6)
    assert stored[0].seen_count == 2


def test_a_story_carried_three_days_is_distinguishable_from_a_brand_new_one(tmp_path) -> None:
    """临时缓存里这两条长得一模一样，这正是它顶不上语料库的地方。"""
    corpus = store(tmp_path)
    old = doc("某公司拟收购标的资产")
    corpus.ingest([old], TODAY, now=MORNING - timedelta(days=3))
    corpus.ingest([old], TODAY, now=MORNING)
    corpus.ingest([doc("突发：某公司获大额订单")], TODAY, now=MORNING)

    by_title = {item.title: item for item in corpus.recent(now=MORNING)}
    assert by_title["某公司拟收购标的资产"].age_hours(MORNING) == 72.0
    assert by_title["突发：某公司获大额订单"].age_hours(MORNING) == 0.0
    assert by_title["突发：某公司获大额订单"].is_fresh(MORNING)
    assert not by_title["某公司拟收购标的资产"].is_fresh(MORNING)


def test_dedup_key_matches_the_batch_layer(tmp_path) -> None:
    """库内去重与批内去重必须同口径，否则 first_seen 变成「最近一次换标题的时间」。

    `dedup.py` 会剥掉开头的来源标签与全部标点，所以下面三种写法是同一条消息。
    """
    corpus = store(tmp_path)
    corpus.ingest([doc("【财联社】央行开展5000亿元逆回购")], TODAY, now=MORNING)
    corpus.ingest([doc("央行开展 5000 亿元逆回购！", source="em")], TODAY, now=MORNING)
    corpus.ingest([doc("[独家] 央行开展5000亿元逆回购。", source="wscn")], TODAY, now=MORNING)

    stored = corpus.recent(now=MORNING)
    assert len(stored) == 1
    assert stored[0].seen_count == 3


def test_cross_source_spread_is_counted_separately_from_repeats(tmp_path) -> None:
    """四个源转同一条，和一个源推四次，不是一回事。

    seen_count 会被同一个源的重复推送刷高，所以「传播有多广」必须单独数源。
    """
    corpus = store(tmp_path)
    title = "重要会议定调明年经济工作"
    for source in ("cls", "em", "cctv", "wscn"):
        corpus.ingest([doc(title, source=source)], TODAY, now=MORNING)

    spread = corpus.recent(now=MORNING)[0]
    assert spread.source_count == 4

    corpus2 = store(tmp_path / "other")
    for _ in range(4):
        corpus2.ingest([doc(title, source="cls")], TODAY, now=MORNING)

    repeat = corpus2.recent(now=MORNING)[0]
    assert repeat.seen_count == 4
    assert repeat.source_count == 1, "同一个源推四次不等于四个源都在说"


# ---- 归因标签 -------------------------------------------------------------


def test_labels_are_written_after_ingest_not_during(tmp_path) -> None:
    """归因失败时原文必须还在——它是事后核对「当时看到了什么」的唯一依据。"""
    corpus = store(tmp_path)
    item = doc("光伏组件价格上涨")
    corpus.ingest([item], TODAY, now=MORNING)

    # 归因还没跑，文档已经在库里了
    assert corpus.recent(now=MORNING)[0].sectors == ()

    corpus.label(item, sectors=["电力设备"], direction="bullish", event_type="涨价")
    labelled = corpus.recent(now=MORNING)[0]
    assert labelled.sectors == ("电力设备",)
    assert labelled.direction == "bullish"


def test_recent_filters_by_sector(tmp_path) -> None:
    corpus = store(tmp_path)
    pv, chip = doc("光伏组件涨价"), doc("存储芯片降价")
    corpus.ingest([pv, chip], TODAY, now=MORNING)
    corpus.label(pv, sectors=["电力设备"])
    corpus.label(chip, sectors=["电子"])

    assert [d.title for d in corpus.recent(sector="电力设备", now=MORNING)] == ["光伏组件涨价"]
    assert [d.title for d in corpus.recent(sector="电子", now=MORNING)] == ["存储芯片降价"]


# ---- 热度曲线 -------------------------------------------------------------


def test_sector_heat_counts_new_stories_not_repeats(tmp_path) -> None:
    """一条消息被转四次不代表热度翻四倍——那是同一个故事。

    `dedup.py` 已经用这条理由丢掉了重复项，热度曲线必须同口径，否则「热度」
    测的是转发勤快程度。
    """
    corpus = store(tmp_path)
    hot = doc("板块利好一")
    corpus.ingest([hot], TODAY, now=MORNING)
    corpus.label(hot, sectors=["电力设备"])
    # 同一条在后面的桶里被反复看到
    for hour in (1, 2, 3):
        corpus.ingest([hot], TODAY, now=MORNING + timedelta(hours=hour))

    curve = corpus.sector_heat("电力设备", within_hours=12, now=MORNING + timedelta(hours=4))
    assert len(curve) == 1, "重复播报不该在后面的时间桶里制造新的热度"
    assert curve[0].fresh_docs == 1


def test_sector_heat_shows_a_rising_curve(tmp_path) -> None:
    corpus = store(tmp_path)
    for hour, count in ((0, 1), (1, 2), (2, 5)):
        for index in range(count):
            item = doc(f"电力设备消息 {hour}-{index}")
            corpus.ingest([item], TODAY, now=MORNING + timedelta(hours=hour))
            corpus.label(item, sectors=["电力设备"])

    curve = corpus.sector_heat("电力设备", within_hours=12, now=MORNING + timedelta(hours=3))
    assert [point.fresh_docs for point in curve] == [1, 2, 5]


# ---- 失败契约 -------------------------------------------------------------


def test_an_unusable_store_never_breaks_the_chain(tmp_path) -> None:
    """语料库写不进去是分析能力的损失，绝不能变成链路中断。

    调用方此时已经拿到了实时文档——和 `NewsCache` 同一条约定。
    """
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("这是个文件，不是目录", encoding="utf-8")
    corpus = NewsStore(blocked / "corpus.sqlite3")

    assert corpus.ok is False
    result = corpus.ingest([doc("任意消息")], TODAY, now=MORNING)
    assert result.ok is False
    assert any("不影响本轮分析" in note for note in result.notes)
    # 所有读方法照样安全返回
    assert corpus.recent(now=MORNING) == []
    assert corpus.sector_heat("电力设备", now=MORNING) == []
    assert corpus.first_seen(doc("任意消息")) is None
    assert corpus.stats()["ok"] is False


def test_docs_without_a_usable_key_are_skipped_not_crashed(tmp_path) -> None:
    corpus = store(tmp_path)
    result = corpus.ingest([doc(""), doc("   "), doc("正常标题")], TODAY, now=MORNING)

    assert result.inserted == 1
    assert result.skipped == 2


def test_freshness_annotation_reports_without_touching_weights(tmp_path) -> None:
    """时效折权归 dedup.py 按发布时间算，两套折减叠起来会把同一件事扣两遍。"""
    corpus = store(tmp_path)
    old = doc("旧闻")
    corpus.ingest([old], TODAY, now=MORNING - timedelta(days=2))

    docs = [old, doc("新闻")]
    returned, notes = corpus.annotate_freshness(docs, now=MORNING)

    assert [item.weight for item in returned] == [1.0, 1.0], "不该改权重"
    assert any("首次出现" in note for note in notes)
    assert any("重复播报" in note for note in notes)


def test_a_schema_bump_rebuilds_rather_than_mixing_old_rows(tmp_path) -> None:
    """老库按旧口径写的行会静默参与新口径的统计——和板块分类缓存同一条教训。"""
    import ashare_decision_support.news.store as store_module

    path = tmp_path / "corpus.sqlite3"
    NewsStore(path).ingest([doc("旧口径写下的记录")], TODAY, now=MORNING)

    original = store_module.SCHEMA_VERSION
    try:
        store_module.SCHEMA_VERSION = original + 1
        rebuilt = NewsStore(path)
        assert rebuilt.ok is True
        assert rebuilt.recent(now=MORNING) == []
    finally:
        store_module.SCHEMA_VERSION = original
