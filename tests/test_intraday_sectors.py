"""盘中板块截面与板块跃迁告警。

这一层的失效方式和个股监控不同，所以断言的重点也不同：

- 个股告警漏报的代价是钱；板块告警**误报**的代价是通道被静音。377 个板块里
  几十个同时「转强」是普涨日的常态，所以噪声预算（同板块合并、每轮上限、
  全天上限）和跃迁判定本身一样重要，这里逐条钉住。
- 排序口径是**超额**不是涨幅。普涨日里绝对涨幅排出来的榜没有信息，
  而这个错误不会让任何东西崩——它只会让推送开始说废话。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from ashare_decision_support.adapters.market_scan import MarketRow, MarketScan
from ashare_decision_support.adapters.ths_taxonomy import (
    CONCEPT_MIN_SIZE,
    ThsTaxonomySource,
    is_tradable_concept,
)
from ashare_decision_support.jobs.monitor import run_monitor
from ashare_decision_support.monitor.loop import run_monitor_loop
from ashare_decision_support.monitor.sector_session import SectorPassResult, SectorSession
from ashare_decision_support.monitor.sector_triggers import (
    MAX_PER_HOUR,
    SectorTriggerEngine,
    _PushLedger,
    fired_sectors_today,
    load_sector_state,
)
from ashare_decision_support.notify.base import NotifierCapabilities
from ashare_decision_support.sectors import intraday
from ashare_decision_support.sectors.base import SectorDefinition
from ashare_decision_support.sectors.intraday import SectorRead, SectorSnapshot
from ashare_decision_support.sectors.ths_taxonomy import ThsConceptTaxonomy, ThsIndustryTaxonomy


class RecordingNotifier:
    capabilities = NotifierCapabilities(channel="recording", markdown=True, max_bytes=100_000)

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send(self, title: str, content: str) -> bool:
        self.messages.append((title, content))
        return True

# ---- 测试替身 --------------------------------------------------------------


class JsonResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


def opener_for(files: dict[str, object]):
    def opener(request, timeout):
        return JsonResponse(files[request.full_url.rsplit("/", 1)[-1]])

    return opener


def row(code: str, name: str, pct: float | None, amount_yi: float = 1.0) -> MarketRow:
    return MarketRow(
        code=code,
        name=name,
        price=10.0,
        change_pct=pct,
        amount=None if amount_yi is None else amount_yi * 1e8,
    )


class FakeScan:
    def __init__(self, rows: list[MarketRow], notes: list[str] | None = None) -> None:
        self._scan = MarketScan(rows={item.code: item for item in rows}, notes=notes or [])

    def fetch(self):
        return self._scan


class FakeTaxonomy:
    def __init__(self, definitions: list[SectorDefinition]) -> None:
        self._definitions = definitions

    def list_sectors(self) -> list[SectorDefinition]:
        return self._definitions


class BrokenTaxonomy:
    def list_sectors(self):
        raise RuntimeError("分类源炸了")


def make_read(
    name: str,
    excess: float,
    *,
    level: str = intraday.LEVEL_INDUSTRY,
    advance: float = 0.8,
    limit_up: int = 0,
    share: float = 0.01,
    watch_hits: tuple = (),
) -> SectorRead:
    return SectorRead(
        name=name,
        level=level,
        median_pct=excess + 0.1,
        excess_pct=excess,
        advance_ratio=advance,
        amount_yi=share * 10000,
        amount_share=share,
        covered=50,
        total=60,
        limit_up=limit_up,
        leaders=(("甲", 9.9), ("乙", 7.0)),
        watch_hits=watch_hits,
    )


def snapshot_of(reads: list[SectorRead], when: datetime) -> SectorSnapshot:
    return SectorSnapshot(
        as_of=when,
        reads=reads,
        breadth_advance=0.55,
        market_median_pct=0.1,
        market_amount_yi=6000.0,
        tradable=True,
    )


# ---- 分类源：并集合并 ------------------------------------------------------


def test_a_symbol_missing_from_the_industry_file_is_not_dropped(tmp_path) -> None:
    """实测有 104 只票只在概念文件里。以行业文件为主循环会让它们整只消失——
    连概念板块都进不去，简报里也查不到它的画像。"""
    files = {
        "industries.json": [
            {"symbol": "600000", "name": "测试银行", "industries": ["银行", "银行Ⅱ"]}
        ],
        "concepts.json": [
            {"symbol": "600000.SH", "concepts": ["金融科技"]},
            {"symbol": "600744.SH", "name": "华银电力", "concepts": ["虚拟电厂", "绿色电力"]},
        ],
    }
    snapshot = ThsTaxonomySource(opener=opener_for(files), cache_dir=tmp_path).load(
        force_refresh=True
    )

    assert set(snapshot.profiles) == {"600000.SH", "600744.SH"}
    orphan = snapshot.profiles["600744.SH"]
    assert orphan.name == "华银电力"
    assert orphan.industries == ()          # 行业缺失只是少一个维度
    assert orphan.concepts == ("虚拟电厂", "绿色电力")
    assert any("只在概念文件里有" in note for note in snapshot.notes)


def test_industry_grouping_ignores_symbols_without_an_industry(tmp_path) -> None:
    """并集带进来的票没有行业，不能因此在行业分组里冒出一个空板块名。"""
    files = {
        "industries.json": [
            {"symbol": "600000", "name": "测试银行", "industries": ["银行"]}
        ],
        "concepts.json": [{"symbol": "600744.SH", "name": "华银电力", "concepts": ["核电"]}],
    }
    snapshot = ThsTaxonomySource(opener=opener_for(files), cache_dir=tmp_path).load(
        force_refresh=True
    )
    assert snapshot.industry_groups("SW1") == {"银行": ["600000.SH"]}


def test_cache_from_yesterday_is_refetched(tmp_path) -> None:
    """上游每天 00:00 UTC 刷新，所以昨天的缓存已经旧了。此前按 7 天缓存，
    概念成分表可以整整落后一周——而盘中板块监控读的正是这张表。"""
    files = {
        "industries.json": [{"symbol": "600000", "name": "今天的名字", "industries": ["银行"]}],
        "concepts.json": [],
    }
    source = ThsTaxonomySource(opener=opener_for(files), cache_dir=tmp_path)
    first = source.load(force_refresh=True)
    assert first.profiles["600000.SH"].name == "今天的名字"

    stale = json.loads((tmp_path / "ths_profiles.json").read_text(encoding="utf-8"))
    stale["fetched_on"] = "2026-08-08"
    stale["profiles"][0]["name"] = "昨天的名字"
    (tmp_path / "ths_profiles.json").write_text(
        json.dumps(stale, ensure_ascii=False), encoding="utf-8"
    )

    again = ThsTaxonomySource(opener=opener_for(files), cache_dir=tmp_path).load()
    assert again.from_cache is False
    assert again.profiles["600000.SH"].name == "今天的名字"


def test_shared_source_loads_one_snapshot_for_industry_and_concepts(tmp_path) -> None:
    """同一轮的行业/概念不能各下载一遍，也不能读到两个不同版本的分类。"""
    files = {
        "industries.json": [
            {"symbol": "600000", "name": "测试银行", "industries": ["银行"]}
        ],
        "concepts.json": [
            {"symbol": "600000", "name": "测试银行", "concepts": ["金融科技"]}
        ],
    }
    calls: list[str] = []
    base_opener = opener_for(files)

    def recording_opener(request, timeout):
        calls.append(request.full_url)
        return base_opener(request, timeout)

    source = ThsTaxonomySource(opener=recording_opener, cache_dir=tmp_path)
    industry = ThsIndustryTaxonomy(source, min_constituents=1)
    concept = ThsConceptTaxonomy(source, min_size=1)

    assert industry.list_sectors()[0].name == "银行"
    assert concept.list_sectors()[0].name == "金融科技"
    assert industry.snapshot is concept.snapshot
    assert len(calls) == 2  # industries.json + concepts.json，各一次


# ---- 概念闸门 --------------------------------------------------------------


def test_a_large_concept_is_no_longer_excluded_by_size(tmp_path) -> None:
    """旧的 400 上限会把机器人概念(1215)、人形机器人(461)、AI应用(527) 这一整批
    超短主战场挡在板块排序之外——也就是「监控机器人板块」结构上不可能实现。"""
    members = [
        {"symbol": f"{600000 + i}", "name": f"票{i}", "concepts": ["机器人概念"]}
        for i in range(500)
    ]
    files = {
        "industries.json": [{"symbol": "600000", "name": "票0", "industries": ["机械设备"]}],
        "concepts.json": members,
    }
    source = ThsTaxonomySource(opener=opener_for(files), cache_dir=tmp_path)
    sectors = ThsConceptTaxonomy(source).list_sectors()

    assert [item.name for item in sectors] == ["机器人概念"]
    assert sectors[0].size == 500


def test_a_concept_below_the_floor_is_still_excluded(tmp_path) -> None:
    """下限保留：成分股太少时中位数由个别票决定，排名翻上翻下全是噪声。"""
    small = [
        {"symbol": f"{600000 + i}", "name": f"票{i}", "concepts": ["袖珍概念"]}
        for i in range(CONCEPT_MIN_SIZE - 1)
    ]
    files = {
        "industries.json": [{"symbol": "600000", "name": "票0", "industries": ["综合"]}],
        "concepts.json": small,
    }
    taxonomy = ThsConceptTaxonomy(ThsTaxonomySource(opener=opener_for(files), cache_dir=tmp_path))
    assert taxonomy.list_sectors() == []
    assert taxonomy.excluded_by_size == 1


def test_the_literal_nan_tag_is_not_a_concept() -> None:
    """上游把 NaN 直接字符串化了，实测有 4 只票带着一个叫 `nan` 的「概念」。
    它此前靠规模下限被顺带挡住，那是巧合不是判断。"""
    assert is_tradable_concept("nan") is False
    assert is_tradable_concept("融资融券") is False      # 交易资格，不是主题
    assert is_tradable_concept("2026中报预增") is False   # 带年份的财报事件
    assert is_tradable_concept("人形机器人") is True


# ---- 截面归并 --------------------------------------------------------------


def test_ranking_is_by_excess_not_by_absolute_move() -> None:
    """普涨日里 +1.5% 是落后。用绝对涨幅排出来的榜不会报错，只会开始说废话。"""
    rows = [row(f"60000{i}", f"甲{i}", 8.0) for i in range(5)]      # 领涨板块
    rows += [row(f"60010{i}", f"乙{i}", 5.0) for i in range(5)]     # 跟涨板块
    rows += [row(f"60020{i}", f"丙{i}", 5.0) for i in range(90)]    # 大盘本身就在涨
    scan = FakeScan(rows)
    taxonomy = FakeTaxonomy(
        [
            SectorDefinition("领涨", "SW1", tuple(f"60000{i}.SH" for i in range(5))),
            SectorDefinition("跟涨", "SW1", tuple(f"60010{i}.SH" for i in range(5))),
        ]
    )

    snapshot = intraday.take_snapshot({"SW1": taxonomy}, adapter=scan, now=datetime(2026, 8, 9, 10))
    ranked = snapshot.ranked("SW1")

    assert snapshot.market_median_pct == pytest.approx(5.0)
    assert [item.name for item in ranked] == ["领涨", "跟涨"]
    assert ranked[0].excess_pct == pytest.approx(3.0)
    # 涨了 5% 却毫无超额——绝对涨幅会把它排成「强势板块」。
    assert ranked[1].median_pct == pytest.approx(5.0)
    assert ranked[1].excess_pct == pytest.approx(0.0)


def test_a_suspended_constituent_is_left_out_of_the_denominator() -> None:
    """停牌没有涨跌幅。当成 0% 会同时拉平中位数和上涨占比。"""
    rows = [row(f"60000{i}", "涨", 6.0) for i in range(1, 6)]
    rows += [row("600006", "停牌", None)]
    rows += [row(f"60010{i}", f"其他{i}", 0.0) for i in range(10)]
    scan = FakeScan(rows)
    taxonomy = FakeTaxonomy(
        [SectorDefinition("测试", "SW1", tuple(f"60000{i}.SH" for i in range(1, 7)))]
    )

    read = intraday.take_snapshot(
        {"SW1": taxonomy}, adapter=scan, now=datetime(2026, 8, 9, 10)
    ).reads[0]

    assert read.covered == 5          # 停牌那只不计入
    assert read.total == 6
    assert read.advance_ratio == pytest.approx(1.0)
    assert read.median_pct == pytest.approx(6.0)


def test_a_first_day_listing_is_not_counted_as_a_limit_up() -> None:
    """上市首日无涨跌幅限制，实测 `N展芯 +396.9%`。用「≥ 涨停价」判定会把它
    算成涨停，而涨停家数是情绪判断里最不该掺假的那个计数。"""
    rows = [
        row("300001", "封板", 20.0),      # 创业板 20cm，真涨停
        row("300002", "新股", 396.9),     # 首日无限制，不是涨停
        row("600001", "主板封板", 10.0),  # 主板 10cm，真涨停
        row("600002", "普通", 3.0),
        row("600003", "普通", 1.0),
    ]
    rows += [row(f"60010{i}", f"其他{i}", 0.0) for i in range(10)]
    taxonomy = FakeTaxonomy(
        [
            SectorDefinition(
                "测试",
                "SW1",
                ("300001.SZ", "300002.SZ", "600001.SH", "600002.SH", "600003.SH"),
            )
        ]
    )

    read = intraday.take_snapshot(
        {"SW1": taxonomy}, adapter=FakeScan(rows), now=datetime(2026, 8, 9, 10)
    ).reads[0]

    assert read.limit_up == 2


def test_watched_symbols_are_recorded_on_the_sector() -> None:
    """「有色金属排名 12→3」本身没法执行——要知道这跟你手上的票有没有关系。"""
    rows = [row(f"60000{i}", f"票{i}", 5.0) for i in range(1, 6)]
    rows += [row(f"60010{i}", f"其他{i}", 0.0) for i in range(10)]
    taxonomy = FakeTaxonomy(
        [SectorDefinition("有色金属", "SW1", tuple(f"60000{i}.SH" for i in range(1, 6)))]
    )

    read = intraday.take_snapshot(
        {"SW1": taxonomy},
        adapter=FakeScan(rows),
        now=datetime(2026, 8, 9, 10),
        watched_symbols={"600003"},
    ).reads[0]

    assert read.watch_hits == (("600003", "票3"),)


def test_one_broken_taxonomy_does_not_take_down_the_other() -> None:
    """概念文件挂了行业排名照出，反之亦然。绑成一次成败等于让次要的否决主要的。"""
    rows = [row(f"60000{i}", f"票{i}", 5.0) for i in range(1, 6)]
    rows += [row(f"60010{i}", f"其他{i}", 0.0) for i in range(10)]
    taxonomy = FakeTaxonomy(
        [SectorDefinition("有色金属", "SW1", tuple(f"60000{i}.SH" for i in range(1, 6)))]
    )

    snapshot = intraday.take_snapshot(
        {"SW1": taxonomy, "CONCEPT": BrokenTaxonomy()},
        adapter=FakeScan(rows),
        now=datetime(2026, 8, 9, 10),
    )

    assert [item.name for item in snapshot.reads] == ["有色金属"]
    assert any("CONCEPT 分类读取异常" in note for note in snapshot.notes)


def test_thin_volume_marks_the_pass_untradable_but_still_records_it() -> None:
    """开盘头几分钟总量太小，横截面排名没有意义——但仍然要落盘，
    否则「较开盘」那一列没有基线。"""
    rows = [row(f"60000{i}", f"票{i}", 5.0, amount_yi=0.1) for i in range(1, 6)]
    rows += [row(f"60010{i}", f"其他{i}", 0.0, amount_yi=0.1) for i in range(10)]
    taxonomy = FakeTaxonomy(
        [SectorDefinition("测试", "SW1", tuple(f"60000{i}.SH" for i in range(1, 6)))]
    )

    snapshot = intraday.take_snapshot(
        {"SW1": taxonomy}, adapter=FakeScan(rows), now=datetime(2026, 8, 9, 9, 31)
    )

    assert snapshot.tradable is False
    assert snapshot.reads                       # 读数照样算出来了
    assert any("低于" in note and "闸门" in note for note in snapshot.notes)


def test_industry_is_listed_before_concept() -> None:
    """字母序会把 CONCEPT 排在 SW1 前面，于是每份简报都从 346 个概念开始读。"""
    snapshot = snapshot_of(
        [
            make_read("机器人概念", 3.0, level=intraday.LEVEL_CONCEPT),
            make_read("有色金属", 2.0),
        ],
        datetime(2026, 8, 9, 10),
    )
    assert snapshot.levels() == [intraday.LEVEL_INDUSTRY, intraday.LEVEL_CONCEPT]


def test_a_sector_with_too_few_quotes_gets_no_reading() -> None:
    """中位数由两只票决定时排名全是噪声——不给读数，而不是给一个坏读数。"""
    scan_rows = {item.code: item for item in [row("600001", "甲", 3.0), row("600002", "乙", 2.0)]}
    reads, thin = intraday.measure(
        scan_rows,
        [SectorDefinition("稀薄", "SW1", tuple(f"60000{i}.SH" for i in range(1, 7)))],
        market_median=0.0,
        total_amount_yi=100.0,
    )

    assert reads == []
    assert thin == 1


# ---- 序列落盘 --------------------------------------------------------------


def test_the_daily_series_round_trips(tmp_path) -> None:
    path = intraday.store_path("2026-08-09", root=tmp_path)
    first = snapshot_of([make_read("有色金属", 2.0, watch_hits=(("600111", "北方稀土"),))],
                        datetime(2026, 8, 9, 9, 40))
    intraday.append(first, path)
    intraday.append(snapshot_of([make_read("有色金属", 4.0)], datetime(2026, 8, 9, 10, 20)), path)

    history = intraday.load_history(path)

    assert [item.as_of.hour for item in history] == [9, 10]
    assert history[0].reads[0].watch_hits == (("600111", "北方稀土"),)
    assert history[0].rank_map(intraday.LEVEL_INDUSTRY) == {"有色金属": 1}


def test_a_half_written_line_does_not_destroy_the_rest_of_the_series(tmp_path) -> None:
    """进程被杀在写入中途是常事。一次崩溃不该让当天剩下的序列全部读不出来。"""
    path = intraday.store_path("2026-08-09", root=tmp_path)
    intraday.append(snapshot_of([make_read("有色金属", 2.0)], datetime(2026, 8, 9, 9, 40)), path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"as_of": "2026-08-09T10:00:00", "rea')
    intraday.append(snapshot_of([make_read("有色金属", 4.0)], datetime(2026, 8, 9, 10, 20)), path)

    assert len(intraday.load_history(path)) == 2


def test_the_baseline_is_the_first_line_not_the_whole_file(tmp_path) -> None:
    """告警层只要「当日第一条」。整份解析一遍只为取第一条，成本随当天已跑轮数
    线性涨——到收盘时是开盘时的三十倍。"""


def test_old_series_files_are_pruned_by_the_date_in_their_name(tmp_path) -> None:
    """一天约 3MB，是状态目录里最大的一类文件，一年不清就是 750MB。

    **按文件名里的日期删，不按 mtime**：mtime 会被备份、rsync、镜像重建改掉，
    于是「保留 30 天」在换一次部署方式之后可能变成删光或者一个不删。
    """
    today = date.today()
    fresh = intraday.store_path(str(today), root=tmp_path)
    recent = intraday.store_path(str(today - timedelta(days=5)), root=tmp_path)
    stale = intraday.store_path(str(today - timedelta(days=90)), root=tmp_path)
    junk = tmp_path / "intraday_sector_notadate.jsonl"
    for path in (fresh, recent, stale, junk):
        path.write_text("{}\n", encoding="utf-8")

    removed = intraday.prune_history(keep_days=30, root=tmp_path)

    assert removed == [stale.name]
    assert fresh.exists() and recent.exists()
    assert junk.exists()          # 名字对不上格式的不碰，宁可留着也不误删
    path = intraday.store_path("2026-08-09", root=tmp_path)
    for hour, excess in ((9, 1.0), (10, 3.0), (11, 5.0)):
        intraday.append(
            snapshot_of([make_read("有色金属", excess)], datetime(2026, 8, 9, hour, 40)), path
        )

    baseline = intraday.load_first(path)

    assert baseline is not None
    assert baseline.as_of.hour == 9
    assert baseline.reads[0].excess_pct == pytest.approx(1.0)


def test_a_restarted_session_still_compares_against_the_open(tmp_path) -> None:
    """盘中重启是常事（改配置、断网重连）。基线记在内存里的话，13:00 重启的进程
    会拿 13:00 当「开盘」，于是「较开盘」悄悄变成「较重启」——而那正是排名跃升
    这条规则的全部判据。"""
    rows = [row(f"60000{i}", f"票{i}", 0.0, amount_yi=20.0) for i in range(1, 6)]
    rows += [row(f"60010{i}", f"其他{i}", 0.0, amount_yi=20.0) for i in range(10)]
    definitions = [SectorDefinition("有色金属", "SW1", tuple(f"60000{i}.SH" for i in range(1, 6)))]

    def build() -> SectorSession:
        return SectorSession(
            session_date=date(2026, 8, 10),
            taxonomies={"SW1": FakeTaxonomy(definitions)},
            adapter=FakeScan(rows),
            store_root=tmp_path,
        )

    build().poll(datetime(2026, 8, 10, 9, 40))          # 开盘那一轮落了基线
    restarted = build()                                  # 换一个进程/实例
    baseline = intraday.load_first(restarted.path)

    assert baseline is not None
    assert baseline.as_of.hour == 9                      # 不是重启时刻


# ---- 跃迁告警 --------------------------------------------------------------


def engine_state(tmp_path):
    return SectorTriggerEngine(), load_sector_state(date(2026, 8, 9), tmp_path / "sector.json")


def test_the_first_pass_only_establishes_a_baseline(tmp_path) -> None:
    """首见不是跃迁。开盘那一条是**总览**，不是给每个强势板块各发一条——
    后者在普涨日是二十条。"""
    engine, state = engine_state(tmp_path)
    first = snapshot_of(
        [make_read(f"板块{i}", 3.0 - i * 0.2) for i in range(10)], datetime(2026, 8, 9, 9, 40)
    )

    alerts, _ = engine.evaluate(first, None, state, datetime(2026, 8, 9, 9, 40))

    assert [item.trigger for item in alerts] == ["session_open"]
    assert alerts[0].sector == ""
    assert any("前三" in line for line in alerts[0].detail)


def test_a_rank_surge_fires_once_and_then_stays_quiet(tmp_path) -> None:
    """一个板块一天升一次就够说明问题。反复报名次会退化成实况解说。"""
    engine, state = engine_state(tmp_path)
    others = [make_read(f"板块{i}", 3.0 - i * 0.2) for i in range(15)]
    first = snapshot_of([*others, make_read("有色金属", -1.0, advance=0.4)],
                        datetime(2026, 8, 9, 9, 40))
    later = snapshot_of([*others, make_read("有色金属", 5.0, advance=0.9)],
                        datetime(2026, 8, 9, 10, 20))

    engine.evaluate(first, None, state, datetime(2026, 8, 9, 9, 40))
    alerts, _ = engine.evaluate(later, first, state, datetime(2026, 8, 9, 10, 20))
    assert [item.sector for item in alerts] == ["有色金属"]
    assert alerts[0].trigger == "rank_surge"
    assert alerts[0].level == "ACTION"

    again, _ = engine.evaluate(later, first, state, datetime(2026, 8, 9, 10, 40))
    assert again == []


def test_one_sector_gets_one_push_even_when_four_rules_fire(tmp_path) -> None:
    """排名跃升 + 转强 + 涨停潮 + 成交额虹吸说的是同一件事的四个侧面，而每条都
    贴着一模一样的证据块。分四条推出去正是通道被静音的原因。"""
    engine, state = engine_state(tmp_path)
    others = [make_read(f"板块{i}", 3.0 - i * 0.2) for i in range(15)]
    first = snapshot_of([*others, make_read("有色金属", -1.0, advance=0.4)],
                        datetime(2026, 8, 9, 9, 40))
    later = snapshot_of(
        [*others, make_read("有色金属", 5.0, advance=0.95, limit_up=6, share=0.05)],
        datetime(2026, 8, 9, 10, 20),
    )

    engine.evaluate(first, None, state, datetime(2026, 8, 9, 9, 40))
    alerts, _ = engine.evaluate(later, first, state, datetime(2026, 8, 9, 10, 20))

    assert len(alerts) == 1
    body = "\n".join(alerts[0].detail)
    assert "同时触发" in body
    for folded in ("转强", "涨停潮", "成交额虹吸"):
        assert folded in body
    # 折进正文的触发器也要记账，否则下一轮会把它们再单独报一遍。
    for trigger in ("tone_strong", "limit_up_wave", "amount_surge"):
        assert state.fire_count("sector:SW1:有色金属", trigger) == 1


def test_a_leading_sector_that_fades_asks_for_a_decision(tmp_path) -> None:
    """上面几条都是「去哪找机会」，这条是「手上的该走了」——板块层面唯一
    能提前于个股止损的信号。"""
    engine, state = engine_state(tmp_path)
    padding = [make_read(f"板块{i}", -1.0 - i * 0.1, advance=0.3) for i in range(15)]
    strong = snapshot_of([make_read("有色金属", 4.0, advance=0.9), *padding],
                         datetime(2026, 8, 9, 9, 40))
    faded = snapshot_of([make_read("有色金属", 0.2, advance=0.5), *padding],
                        datetime(2026, 8, 9, 11, 0))

    engine.evaluate(strong, None, state, datetime(2026, 8, 9, 9, 40))
    alerts, _ = engine.evaluate(faded, strong, state, datetime(2026, 8, 9, 11, 0))

    assert [item.trigger for item in alerts] == ["fade"]
    assert alerts[0].level == "WARN"


def test_an_untradable_pass_produces_no_alerts(tmp_path) -> None:
    """排名已经被判为噪声，在噪声上做差分只会把噪声变成告警。"""
    engine, state = engine_state(tmp_path)
    first = snapshot_of([make_read(f"板块{i}", 3.0 - i * 0.2) for i in range(15)],
                        datetime(2026, 8, 9, 9, 31))
    noisy = snapshot_of([make_read(f"板块{i}", 3.0 - i * 0.2) for i in range(15)],
                        datetime(2026, 8, 9, 9, 33))
    noisy.tradable = False

    engine.evaluate(first, None, state, datetime(2026, 8, 9, 9, 31))
    alerts, notes = engine.evaluate(noisy, first, state, datetime(2026, 8, 9, 9, 33))

    assert alerts == []
    assert any("未过闸" in note for note in notes)


def test_truncation_is_never_silent(tmp_path) -> None:
    """**静默截断会让「只推了 3 条」读起来像「只发生了 3 件事」。**"""
    engine, state = engine_state(tmp_path)
    weak = [make_read(f"板块{i}", 0.0, advance=0.5) for i in range(15)]
    first = snapshot_of(weak, datetime(2026, 8, 9, 9, 40))
    surge = snapshot_of(
        [make_read(f"板块{i}", 5.0 - i * 0.05, advance=0.95, limit_up=5) for i in range(15)],
        datetime(2026, 8, 9, 10, 20),
    )

    engine.evaluate(first, None, state, datetime(2026, 8, 9, 9, 40))
    alerts, notes = engine.evaluate(surge, first, state, datetime(2026, 8, 9, 10, 20))

    assert len(alerts) == MAX_PER_HOUR      # 15 个板块同时转强，按额度截断
    assert any("未推" in note for note in notes)


def test_morning_noise_cannot_silence_the_afternoon(tmp_path) -> None:
    """**这一条是三层预算里「每小时」那一层存在的全部理由。**

    实测 30 轮模拟：早盘半小时的排名 churn 会把「全天 20 条」的额度一次吃光，
    于是 13:00 一个九家涨停的真涨停潮**一条都推不出来**——而早盘那些是成交额
    基数最小、名次最不稳的时段推出来的。额度按小时分配之后，早盘再吵也吵不掉
    下午的份。
    """
    engine, state = engine_state(tmp_path)
    # 机器人概念全天都在名单里，早盘垫底——真实盘面里 377 个板块每轮都有读数，
    # 一个「13:00 才第一次出现」的板块只会立基线（首见不是跃迁）。
    def board(robot_excess: float, robot_limit: int, hot: bool):
        return [
            make_read("机器人概念", robot_excess, advance=0.95 if hot else 0.1,
                      limit_up=robot_limit),
            *[
                make_read(f"板块{i}", (4.0 - i * 0.05) if hot else 0.0,
                          advance=0.95 if hot else 0.5)
                for i in range(20)
            ],
        ]

    baseline = snapshot_of(board(-3.0, 0, hot=False), datetime(2026, 8, 10, 9, 40))
    engine.evaluate(baseline, None, state, datetime(2026, 8, 10, 9, 40))

    # 早盘：连着几轮大批板块转强转弱，把这一小时的额度榨干
    for minute in (45, 50, 55):
        moment = datetime(2026, 8, 10, 9, minute)
        engine.evaluate(snapshot_of(board(-3.0, 0, hot=True), moment), baseline, state, moment)
        engine.evaluate(snapshot_of(board(-3.0, 0, hot=False), moment), baseline, state, moment)

    # 13:00：此前垫底的机器人概念出现九家涨停并冲到第一
    moment = datetime(2026, 8, 10, 13, 0)
    real = snapshot_of(board(8.0, 9, hot=False), moment)
    alerts, _ = engine.evaluate(real, baseline, state, moment)

    assert [item.sector for item in alerts] == ["机器人概念"]
    assert alerts[0].level == "ACTION"


def test_the_budget_counts_pushes_not_folded_triggers(tmp_path) -> None:
    """一条「排名跃升（同时触发：转强、涨停潮）」是**一条推送**。

    按触发器计数的话它记 3 笔，于是「全天最多 20 条」实际上七八条就闭嘴——
    而且闭得悄无声息。
    """
    engine, state = engine_state(tmp_path)
    others = [make_read(f"板块{i}", 3.0 - i * 0.2) for i in range(15)]
    first = snapshot_of([*others, make_read("有色金属", -1.0, advance=0.4)],
                        datetime(2026, 8, 9, 9, 40))
    later = snapshot_of(
        [*others, make_read("有色金属", 5.0, advance=0.95, limit_up=6, share=0.05)],
        datetime(2026, 8, 9, 10, 20),
    )

    engine.evaluate(first, None, state, datetime(2026, 8, 9, 9, 40))
    alerts, _ = engine.evaluate(later, first, state, datetime(2026, 8, 9, 10, 20))

    assert len(alerts) == 1
    # 记了 4 个触发器，但只扣 1 条额度（另一条额度是首轮的开盘总览）
    assert sum(int(r.get("count") or 0) for r in state.fired.values()) >= 4
    assert _PushLedger(state).spent_today() == 2


def test_alerts_touching_watched_symbols_are_ranked_first(tmp_path) -> None:
    """同一条「转强」，发生在你有仓位的方向上和发生在一个没碰过的方向上，
    紧急程度差一个量级。"""
    engine, state = engine_state(tmp_path)
    padding = [make_read(f"板块{i}", 0.0, advance=0.5) for i in range(15)]
    first = snapshot_of(
        [
            make_read("无关板块", 0.0, advance=0.5),
            make_read("我的板块", 0.0, advance=0.5, watch_hits=(("600111", "北方稀土"),)),
            *padding,
        ],
        datetime(2026, 8, 9, 9, 40),
    )
    later = snapshot_of(
        [
            make_read("无关板块", 6.0, advance=0.95),
            make_read("我的板块", 4.0, advance=0.95, watch_hits=(("600111", "北方稀土"),)),
            *padding,
        ],
        datetime(2026, 8, 9, 10, 20),
    )

    engine.evaluate(first, None, state, datetime(2026, 8, 9, 9, 40))
    alerts, _ = engine.evaluate(later, first, state, datetime(2026, 8, 9, 10, 20))

    assert [item.trigger for item in alerts] == ["tone_strong", "tone_strong"]
    assert alerts[0].sector == "我的板块"        # 超额更低，但命中持仓
    assert alerts[0].touches_watched is True


def test_a_strong_sector_outside_the_attention_band_stays_quiet(tmp_path) -> None:
    """排在第 120 名的概念转强不是新闻。377 个板块里，普涨的早盘会有几十个
    同时「转强」——不设这道闸，这个通道第一天就会被静音。"""
    engine, state = engine_state(tmp_path)
    hot = [make_read(f"热门{i}", 5.0 - i * 0.01, advance=0.9) for i in range(40)]
    first = snapshot_of([*hot, make_read("冷门", 0.0, advance=0.5)], datetime(2026, 8, 9, 9, 40))
    later = snapshot_of(
        [*hot, make_read("冷门", 1.5, advance=0.9)], datetime(2026, 8, 9, 10, 20)
    )

    engine.evaluate(first, None, state, datetime(2026, 8, 9, 9, 40))
    alerts, _ = engine.evaluate(later, first, state, datetime(2026, 8, 9, 10, 20))

    assert [item for item in alerts if item.sector == "冷门"] == []


def test_stock_and_sector_alert_state_do_not_share_a_file() -> None:
    """`AlertState` 是「读进来 → 改 → 整份写回」。两个引擎指向同一路径的话，
    后写的会把前一个的记录整片抹掉——而那是「今天已经发过了」的唯一凭证。"""
    from ashare_decision_support.state.alerts import AlertState

    assert load_sector_state(date(2026, 8, 9)).path != AlertState.load(date(2026, 8, 9)).path


def test_todays_sector_alerts_are_readable_by_chat_ask_and_state(tmp_path) -> None:
    """分成两个状态文件的代价：`chat` / `ask` / `state` 沿用
    `AlertState.load(today).fired_summary()` 时只看得到个股那一份，问「今天板块
    有什么异动」会答「没有任何告警」——而实际上推过。所以要有这条读回路径。

    顺带把 key 拆回可读形式：`fired_summary()` 里是 `SECTOR:SW1:有色金属`。
    """
    session = date(2026, 8, 9)
    path = tmp_path / "sector.json"
    engine = SectorTriggerEngine()
    state = load_sector_state(session, path)
    others = [make_read(f"板块{i}", 3.0 - i * 0.2) for i in range(15)]
    first = snapshot_of([*others, make_read("有色金属", -1.0, advance=0.4)],
                        datetime(2026, 8, 9, 9, 40))
    later = snapshot_of([*others, make_read("有色金属", 5.0, advance=0.95)],
                        datetime(2026, 8, 9, 10, 20))
    engine.evaluate(first, None, state, datetime(2026, 8, 9, 9, 40))
    engine.evaluate(later, first, state, datetime(2026, 8, 9, 10, 20))
    state.save()

    rows = fired_sectors_today(session, path)

    hit = next(row for row in rows if row["sector"] == "有色金属")
    assert hit["sector_label"] == "行业"
    assert hit["level"] == "SW1"
    assert hit["label"] == "排名跃升"
    assert hit["alert_level"] == "ACTION"

    # 开盘总览的 key 只有两段（没有层级和板块名）。**它也必须读得回来**——
    # 少了它，只推过一条总览的日子里 `state` 会报「今天没有板块告警」。
    opener = next(row for row in rows if row["trigger"] == "session_open")
    assert opener["sector"] == ""
    assert opener["sector_label"] == "全场"
    assert opener["label"] == "开盘板块概览"

    # 推送流水那条保留键在 observed 里，不该混进 fired 的读回结果
    assert all(not str(row["sector"]).startswith("__") for row in rows)


# ---- 会话编排 --------------------------------------------------------------


def test_the_session_degrades_to_a_note_when_the_scan_fails(tmp_path) -> None:
    """板块是旁证不是主线：它挂了不该让个股监控那一轮跟着停。"""

    class ExplodingAdapter:
        def fetch(self):
            raise RuntimeError("网络炸了")

    session = SectorSession(
        session_date=date(2026, 8, 9),
        taxonomies={"SW1": FakeTaxonomy([])},
        adapter=ExplodingAdapter(),
        store_root=tmp_path,
    )
    result = session.poll(datetime(2026, 8, 9, 10))

    assert result.ok is False
    assert result.alerts == []
    assert any("取数失败" in note for note in result.notes)


def test_the_session_writes_one_line_per_pass(tmp_path) -> None:
    rows = [row(f"60000{i}", f"票{i}", 5.0, amount_yi=20.0) for i in range(1, 6)]
    rows += [row(f"60010{i}", f"其他{i}", 0.0, amount_yi=20.0) for i in range(10)]
    session = SectorSession(
        session_date=date(2026, 8, 9),
        taxonomies={
            "SW1": FakeTaxonomy(
                [SectorDefinition("有色金属", "SW1", tuple(f"60000{i}.SH" for i in range(1, 6)))]
            )
        },
        adapter=FakeScan(rows),
        store_root=tmp_path,
        watch_symbols=lambda: ["600003.SH"],
    )

    session.poll(datetime(2026, 8, 9, 10, 0))
    session.poll(datetime(2026, 8, 9, 10, 10))

    history = intraday.load_history(session.path)
    assert len(history) == 2
    assert history[0].reads[0].watch_hits == (("600003", "票3"),)


# ---- 与个股循环的接线 ------------------------------------------------------


class CountingSectorSession:
    """记录被 poll 了几次，并按需产出一条告警。"""

    def __init__(self, alerts_on_pass: int | None = None, explode: bool = False) -> None:
        self.polls: list[datetime] = []
        self.alerts_on_pass = alerts_on_pass
        self.explode = explode

    def poll(self, now):
        self.polls.append(now)
        if self.explode:
            raise RuntimeError("板块炸了")
        alerts = []
        if self.alerts_on_pass is not None and len(self.polls) == self.alerts_on_pass:
            from ashare_decision_support.monitor.sector_triggers import SectorAlert

            alerts = [
                SectorAlert(
                    trigger="rank_surge",
                    level="ACTION",
                    headline="行业·有色金属 排名跃升 14→1",
                    detail=["证据"],
                    sector="有色金属",
                    sector_level="SW1",
                    as_of=now,
                )
            ]
        return SectorPassResult(snapshot=None, alerts=alerts, notes=[])


class QuietSession:
    """个股那一侧什么都不做，好让断言只落在板块节奏上。"""

    def poll(self, watch, now, in_session=True):
        from ashare_decision_support.monitor.feed import assess_feed
        from ashare_decision_support.monitor.session import PassResult

        return PassResult(health=assess_feed({}, 0, now, None, in_session))


def test_the_sector_pass_runs_on_its_own_cadence() -> None:
    """板块 pass 是 56 页并发的全市场扫描，比个股那轮贵得多；而板块排名在两分钟
    里的移动基本是噪声。所以它跟着同一个循环走，但只每 N 轮做一次。"""
    sector = CountingSectorSession()
    run_monitor_loop(
        session=QuietSession(),
        watch_symbols=list,
        notifiers=[],
        now_fn=lambda: datetime(2026, 8, 10, 10, 0),
        sleep=lambda seconds: None,
        max_passes=11,
        sector_session=sector,
        sector_every=5,
    )

    assert len(sector.polls) == 3          # 第 1、6、11 轮


def test_the_sector_pass_is_skipped_outside_the_session() -> None:
    """休市时全市场行情不动，落进序列的是一串重复截面，会把「较上轮」变成噪声。"""
    sector = CountingSectorSession()
    run_monitor_loop(
        session=QuietSession(),
        watch_symbols=list,
        notifiers=[],
        now_fn=lambda: datetime(2026, 8, 10, 12, 0),   # 午休
        sleep=lambda seconds: None,
        max_passes=6,
        sector_session=sector,
        sector_every=1,
    )

    assert sector.polls == []


def test_a_failing_sector_pass_does_not_kill_the_stock_loop() -> None:
    """板块是「先看哪里」，个股是「买卖点」。让前者有资格中断后者，
    等于用旁证决定主线跑不跑。"""
    stats = run_monitor_loop(
        session=QuietSession(),
        watch_symbols=list,
        notifiers=[],
        now_fn=lambda: datetime(2026, 8, 10, 10, 0),
        sleep=lambda seconds: None,
        max_passes=3,
        sector_session=CountingSectorSession(explode=True),
        sector_every=1,
    )

    assert stats.passes == 3
    assert stats.sector_passes == 0
    assert any("板块截面" in note and "异常" in note for note in stats.notes)


def test_sector_alerts_are_published_with_their_own_title() -> None:
    """标题是通知栏里唯一看得见的那一行，所以点名具体板块而不是只报条数。"""
    notifier = RecordingNotifier()
    stats = run_monitor_loop(
        session=QuietSession(),
        watch_symbols=list,
        notifiers=[notifier],
        now_fn=lambda: datetime(2026, 8, 10, 10, 0),
        sleep=lambda seconds: None,
        max_passes=1,
        sector_session=CountingSectorSession(alerts_on_pass=1),
        sector_every=1,
    )

    assert stats.sector_alerts == 1
    title, body = notifier.messages[0]
    assert "有色金属" in title
    assert "先看哪里" in body


# ---- 空名单 ----------------------------------------------------------------


def test_an_empty_position_list_still_runs_the_sector_monitor(monkeypatch) -> None:
    """空仓的日子恰恰是最需要「现在该看哪个方向」的那一天。此前名单为空时
    整个 `run_monitor` 直接 return，板块那一半跟着一起没了。"""
    sector = CountingSectorSession(alerts_on_pass=1)
    notifier = RecordingNotifier()

    summary = run_monitor(
        adapter=None,
        once=True,
        notifiers=[notifier],
        news_source=None,
        trade_date=date(2026, 8, 10),
        now=datetime(2026, 8, 10, 10, 0),
        sector_session=sector,
    )

    assert summary["watch"] == 0
    assert summary["sector_passes"] == 1
    assert summary["sector_alerts"] == 1
    assert any("只跑板块监控" in note for note in summary["notes"])
    assert notifier.messages


def test_disabling_sectors_on_an_empty_position_list_says_so(monkeypatch) -> None:
    summary = run_monitor(
        adapter=None,
        once=True,
        notifiers=[],
        news_source=None,
        trade_date=date(2026, 8, 10),
        now=datetime(2026, 8, 10, 10, 0),
        sectors=False,
    )

    assert summary["passes"] == 0
    assert any("没有任何可做的事" in note for note in summary["notes"])
