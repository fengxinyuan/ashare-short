from __future__ import annotations

from ashare_decision_support.news import NewsDoc, macro_context_of, read_macro_links


def doc(title: str, summary: str = "") -> NewsDoc:
    return NewsDoc(title=title, summary=summary, published_at="2026-08-07 08:30:00")


def test_oil_rally_produces_upstream_and_downstream_links() -> None:
    digest = read_macro_links([doc("布伦特原油涨3%，中东局势升级")])

    assert digest.ok is True
    pairs = {(item.mechanism, item.direction, item.targets) for item in digest.links}
    assert ("油价上行", "bullish", ("石油石化", "油服", "油运")) in pairs
    assert ("能源成本抬升", "bearish", ("航空", "化纤")) in pairs


def test_gold_and_dollar_move_are_recorded_as_separate_macro_topics() -> None:
    digest = read_macro_links(
        [
            doc("现货黄金涨2%，避险情绪升温"),
            doc("美元指数走高，离岸人民币回落"),
        ]
    )

    topics = {item.topic for item in digest.links}
    assert "gold" in topics
    assert "usd" in topics


def test_china_macro_beat_is_marked_as_a_pro_cyclical_signal() -> None:
    digest = read_macro_links([doc("中国7月官方制造业PMI 50.4，高于预期 49.8，前值 49.9")])

    chain = next(item for item in digest.links if item.topic == "china_macro")
    assert chain.direction == "bullish"
    assert chain.expectation == "beat"
    assert chain.targets == ("顺周期", "工业金属", "机械设备")


def test_us_macro_miss_flips_to_a_dovish_growth_support_read() -> None:
    digest = read_macro_links([doc("美国CPI低于预期，核心通胀继续回落")])

    chain = next(item for item in digest.links if item.topic == "us_macro")
    assert chain.direction == "bullish"
    assert chain.expectation == "miss"


def test_earnings_surprise_is_recorded_but_priced_in_is_left_as_a_note() -> None:
    digest = read_macro_links([doc("某消费龙头中报超预期，二季度利润率改善")])

    chain = next(item for item in digest.links if item.topic == "earnings_expectation")
    assert chain.direction == "bullish"
    assert chain.expectation == "beat"
    assert any("已被股价提前兑现" in note for note in digest.notes)


def test_empty_input_degrades_to_an_empty_digest() -> None:
    digest = read_macro_links([])

    assert digest.ok is False
    assert digest.item_count == 0
    assert digest.notes


def test_overseas_korea_geopolitics_and_policy_are_exposed_as_auditable_links() -> None:
    digest = read_macro_links(
        [
            doc("费城半导体指数上涨，纳斯达克走高"),
            doc("KOSPI上涨，SK海力士带动存储芯片走强"),
            doc("中美出口管制升级，国产替代预期升温"),
            doc("中央汇金增持ETF，稳定市场预期"),
        ]
    )

    topics = {item.topic for item in digest.links}
    assert {"us_equities", "korea_market", "geopolitics", "policy_state_support"} <= topics

    korea = next(item for item in digest.links if item.topic == "korea_market")
    assert korea.targets == ("半导体", "存储芯片", "消费电子", "面板")


def test_product_catalysts_cover_known_and_newly_named_categories() -> None:
    digest = read_macro_links(
        [
            doc("六氟磷酸锂报价上调，多氟多等氟化工企业受关注"),
            doc("锗价格上涨，云南锗业相关产业链获催化"),
            doc("生猪价格上涨，养殖端供给收缩"),
            doc("未知品类价格上涨，相关产品供不应求"),
        ]
    )

    products = [item for item in digest.links if item.topic == "product_catalyst"]
    assert any(
        item.mechanism.startswith("六氟磷酸锂") and "基础化工" in item.targets for item in products
    )
    assert any(item.mechanism.startswith("锗") and "有色金属" in item.targets for item in products)
    assert any(
        item.mechanism.startswith("生猪") and item.targets == ("农林牧渔",) for item in products
    )
    assert any("未知品类" in item.mechanism and "待映射" in item.targets[0] for item in products)


def test_product_catalyst_rejects_currency_ranges_and_market_indexes() -> None:
    digest = read_macro_links(
        [
            doc("离岸人民币兑美元报6.74元，日内交投于价格区间"),
            doc("彭博电动汽车价格回报指数上涨"),
        ]
    )

    assert not [item for item in digest.links if item.topic == "product_catalyst"]


def test_macro_context_of_turns_links_into_unique_topic_direction_and_expectation_tags() -> None:
    digest = read_macro_links(
        [
            doc("布伦特原油涨3%，中东局势升级"),
            doc("中国7月官方制造业PMI 50.4，高于预期 49.8，前值 49.9"),
            doc("WTI原油继续上涨，油运景气跟随抬升"),
        ]
    )

    context = macro_context_of(digest)

    assert context["recorded"] is True
    assert context["topics"] == ["china_macro", "geopolitics", "oil"]
    assert "oil:bullish" in context["signals"]
    assert "oil:bearish" in context["signals"]
    assert context["expectations"] == ["china_macro:beat"]


def test_macro_context_of_keeps_a_recorded_but_empty_digest_distinct_from_unrecorded() -> None:
    context = macro_context_of(read_macro_links([]))

    assert context["recorded"] is True
    assert context["topics"] == []
    assert context["signals"] == []
    assert context["expectations"] == []
