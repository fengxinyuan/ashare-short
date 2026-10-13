from __future__ import annotations

import json
from datetime import date

from ashare_decision_support.jobs.morning_select import _attach_theme_context
from ashare_decision_support.news import NewsDoc, SocialSnapshotSource, load_themes, scan_themes
from ashare_decision_support.notify.render import render_theme_radar
from ashare_decision_support.sectors.base import SectorMetrics


def metric(name: str, *, ret_5d: float = 0.0, breadth: float = 0.0) -> SectorMetrics:
    return SectorMetrics(name, constituents=10, covered=10, ret_5d=ret_5d, breadth=breadth)


def test_social_only_topic_is_a_lead_not_a_confirmed_theme() -> None:
    signals = scan_themes(
        [],
        [NewsDoc(title="磷化铟和薄膜铌酸锂成为热点", source="社媒快照:xiaohongshu")],
        {"电子": metric("电子", ret_5d=0.04), "通信": metric("通信", ret_5d=0.03)},
    )

    signal = next(item for item in signals if item.name == "光芯片材料")
    assert signal.stage == "社媒线索，等待事实验证"
    assert signal.price_confirmed is True


def test_verified_event_and_two_sector_confirmation_upgrade_the_theme() -> None:
    signals = scan_themes(
        [NewsDoc(title="交换机扩容拉动高速PCB和CPO订单", source="财经快讯")],
        [],
        {
            "电子": metric("电子", ret_5d=0.04),
            "通信": metric("通信", breadth=0.75),
            "计算机": metric("计算机"),
        },
    )

    signal = next(item for item in signals if item.name == "AI高速互连")
    assert signal.stage == "价格确认观察"
    assert signal.price_confirmed is True


def test_social_snapshot_reads_an_auditable_export(tmp_path) -> None:
    path = tmp_path / "xhs_20260807.json"
    path.write_text(
        json.dumps(
            {
                "platform": "xiaohongshu",
                "items": [
                    {
                        "title": "电子布供需观察",
                        "published_at": "2026-08-07 18:00",
                        "url": "https://example.test/topic",
                        "likes": 123,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    docs, notes = SocialSnapshotSource(path).fetch_market(date(2026, 8, 7))

    assert not notes
    assert len(docs) == 1
    assert docs[0].source == "社媒快照:xiaohongshu"
    assert docs[0].url == "https://example.test/topic"
    assert docs[0].engagement == 123


def test_social_snapshot_rejects_items_older_than_three_days(tmp_path) -> None:
    path = tmp_path / "douyin_20260808.json"
    path.write_text(
        json.dumps(
            {
                "platform": "douyin",
                "items": [{"title": "旧热点", "published_at": "2026-08-04 08:00"}],
            }
        ),
        encoding="utf-8",
    )

    docs, _ = SocialSnapshotSource(path).fetch_market(date(2026, 8, 8))

    assert docs == []


def test_rendered_theme_radar_keeps_evidence_and_invalidation() -> None:
    markdown = render_theme_radar(
        [
            {
                "name": "PCB关键材料",
                "stage": "事件预警，等待价格确认",
                "sectors": ["电子", "基础化工"],
                "verified_docs": 1,
                "social_docs": 1,
                "transmission": "高速板升级传导至电子布和覆铜板",
                "invalidation": "材料报价没有跟随",
                "evidence": [
                    {
                        "title": "电子布供需观察",
                        "source": "社媒快照:xiaohongshu",
                        "url": "https://example.test/topic",
                        "engagement": 123,
                    }
                ],
            }
        ]
    )

    assert "## 主题雷达" in markdown
    assert "材料报价没有跟随" in markdown
    assert "互动量 123" in markdown
    assert "https://example.test/topic" in markdown


def test_custom_consumer_theme_can_be_added_without_a_code_change(tmp_path) -> None:
    path = tmp_path / "themes.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "本地消费",
                    "terms": ["夜间经济", "消费券"],
                    "sectors": ["商贸零售"],
                    "transmission": "消费政策传导至线下零售",
                    "invalidation": "核销和销售不及预期",
                }
            ]
        ),
        encoding="utf-8",
    )

    themes = load_themes(path)
    signals = scan_themes(
        [NewsDoc(title="多地加码消费券支持夜间经济", source="官方")],
        [],
        {"商贸零售": metric("商贸零售", breadth=0.8)},
        themes,
    )

    assert any(item.name == "本地消费" and item.stage == "价格确认观察" for item in signals)


def test_verified_theme_is_attached_to_its_sector_candidates_only() -> None:
    rows = [{"symbol": "000001.SZ", "sector": "电子"}, {"symbol": "000002.SZ", "sector": "银行"}]
    signal = scan_themes(
        [NewsDoc(title="交换机扩容拉动高速PCB和CPO订单", source="财经快讯")],
        [],
        {"电子": metric("电子", ret_5d=0.04), "通信": metric("通信", breadth=0.75)},
    )[0]

    _attach_theme_context(rows, [signal])

    assert rows[0]["theme_context"][0]["name"] == "AI高速互连"
    assert "theme_context" not in rows[1]


def test_attack_news_is_not_low_altitude_evidence() -> None:
    """「无人机袭击」命中了「无人机」，却不是低空经济的线索。

    2026-09-14 实测：沙特输油管道遇袭、乌克兰列车遭撞击两条都被印成了
    「低空与商业航天」的证据。排除词按主题定义，命中就整条不算。
    """
    signals = scan_themes(
        [
            NewsDoc(title="沙特输油管道遭无人机袭击停运", source="央视财经"),
            NewsDoc(title="一列开往华沙的列车在乌克兰遭无人机撞击", source="快讯"),
            NewsDoc(
                title="报道称朝鲜人民军进行协同火力打击训练",
                summary="参训部队出动各种攻击无人机、巡航导弹、战术弹道导弹等新型战斗系统",
                source="财联社",
            ),
            NewsDoc(title="胡塞武装称使用大量弹道导弹和无人机打击沙特军事设施", source="快讯"),
        ],
        [],
        {},
    )

    assert not any(item.name == "低空与商业航天" for item in signals)


def test_evidence_is_ranked_by_how_many_theme_terms_it_hits() -> None:
    """证据栏只印前几条：同时命中三个主题词的产业消息要排在擦边命中一个词的公告前面。"""
    signals = scan_themes(
        [
            NewsDoc(title="公告全知道：PCB+毫米波雷达+无人机", source="财联社"),
            NewsDoc(title="商业航天：引力一号运载火箭完成卫星发射", source="财联社"),
        ],
        [],
        {},
    )

    signal = next(item for item in signals if item.name == "低空与商业航天")
    assert signal.verified_docs == 2
    assert signal.headlines[0] == "商业航天：引力一号运载火箭完成卫星发射"


def test_a_single_theme_term_buried_in_the_body_is_not_evidence() -> None:
    """标题不沾边、正文擦边碰到一个词的消息不算证据。

    2026-09-14 实测：「特斯拉 Roadster 发布会」正文提了一句 SpaceX 火箭发射，
    「算力词元贷推广」正文说展台上摆着运载火箭模型，两条都被印成了商业航天的证据。
    正文命中要至少两个不同的主题词才收；标题命中一个就够。
    """
    body_only = NewsDoc(
        title="特斯拉预告将于10月1日举办活动，展示下一代Roadster跑车",
        summary="并可能进一步引发有关特斯拉与SpaceX合并的猜测，准备火箭发射",
        source="快讯",
    )
    body_two_terms = NewsDoc(
        title="服贸会金融科创联展",
        summary="展台上有运载火箭模型，配套卫星互联网终端同台展示",
        source="快讯",
    )
    title_hit = NewsDoc(title="零界航空与峰飞航空达成30架eVTOL采购协议", source="快讯")

    signal = next(
        item
        for item in scan_themes([body_only, body_two_terms, title_hit], [], {})
        if item.name == "低空与商业航天"
    )

    assert signal.verified_docs == 2
    assert signal.headlines[0] == title_hit.title
    assert body_only.title not in signal.headlines


def test_custom_theme_can_declare_its_own_exclusions(tmp_path) -> None:
    path = tmp_path / "themes.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "无人机物流",
                    "terms": ["无人机"],
                    "sectors": ["交通运输"],
                    "exclude": ["袭击"],
                }
            ]
        ),
        encoding="utf-8",
    )

    signals = scan_themes(
        [NewsDoc(title="无人机袭击油田", source="快讯")],
        [NewsDoc(title="无人机配送试点扩容", source="社媒快照:douyin")],
        {},
        load_themes(path),
    )

    signal = next(item for item in signals if item.name == "无人机物流")
    assert signal.verified_docs == 0
    assert signal.social_docs == 1
