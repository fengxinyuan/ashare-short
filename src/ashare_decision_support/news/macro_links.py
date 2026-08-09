"""Deterministic macro linkage and expectation-gap extraction.

This is the non-LLM counterpart to `daily_read.py`: instead of asking a model
to narrate cross-market transmission, this module records a bounded set of
well-known macro chains as structured facts.  The goal is not to replace the
free-form read, but to make sure key links such as oil / gold / dollar /
Fed / PMI / earnings surprise are visible even when no model is involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.sectors.keywords import SECTOR_KEYWORDS

TOPIC_OIL = "oil"
TOPIC_GOLD = "gold"
TOPIC_USD = "usd"
TOPIC_RATES = "rates"
TOPIC_CHINA_MACRO = "china_macro"
TOPIC_US_MACRO = "us_macro"
TOPIC_EARNINGS = "earnings_expectation"
TOPIC_US_EQUITIES = "us_equities"
TOPIC_KOREA = "korea_market"
TOPIC_METALS = "industrial_metals"
TOPIC_GEOPOLITICS = "geopolitics"
TOPIC_POLICY = "policy_state_support"
TOPIC_PRODUCT = "product_catalyst"

EXPECT_NONE = "none"
EXPECT_BEAT = "beat"
EXPECT_MISS = "miss"
EXPECT_INLINE = "inline"

UP_TERMS = ("涨", "上涨", "上调", "走高", "攀升", "上行", "飙升", "大涨", "反弹", "新高")
DOWN_TERMS = ("跌", "下跌", "下调", "走低", "回落", "下行", "暴跌", "大跌", "回调", "新低")
EXPECT_BEAT_TERMS = ("高于预期", "超预期", "好于预期", "强于预期", "优于预期")
EXPECT_MISS_TERMS = ("低于预期", "不及预期", "逊于预期", "弱于预期")
EXPECT_INLINE_TERMS = ("符合预期", "持平预期")
HAWKISH_TERMS = ("加息", "鹰派", "转鹰", "higher for longer", "维持高利率", "缩表")
DOVISH_TERMS = ("降息", "鸽派", "转鸽", "降准", "降息预期升温", "流动性投放")


@dataclass(frozen=True)
class MacroLink:
    topic: str
    trigger: str
    mechanism: str
    targets: tuple[str, ...]
    direction: str
    evidence: tuple[str, ...] = ()
    expectation: str = EXPECT_NONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "trigger": self.trigger,
            "mechanism": self.mechanism,
            "targets": list(self.targets),
            "direction": self.direction,
            "evidence": list(self.evidence),
            "expectation": self.expectation,
        }


@dataclass
class MacroDigest:
    ok: bool = False
    links: list[MacroLink] = field(default_factory=list)
    coverage: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def item_count(self) -> int:
        return len(self.links)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "links": [item.to_dict() for item in self.links],
            "coverage": dict(self.coverage),
            "notes": list(self.notes),
            "item_count": self.item_count,
        }


def macro_context_of(digest: MacroDigest | dict[str, Any] | None) -> dict[str, Any]:
    """压成台账可验证的宏观标签。"""
    if digest is None:
        return {}

    if isinstance(digest, MacroDigest):
        raw_links = [item.to_dict() for item in digest.links]
    elif isinstance(digest, dict):
        raw_links = list(digest.get("links") or [])
    else:
        return {}

    topics: set[str] = set()
    signals: set[str] = set()
    expectations: set[str] = set()
    for item in raw_links:
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic") or "").strip()
        direction = str(item.get("direction") or "").strip()
        expectation = str(item.get("expectation") or EXPECT_NONE).strip()
        if not topic:
            continue
        topics.add(topic)
        if direction and direction != "mixed":
            signals.add(f"{topic}:{direction}")
        if expectation and expectation != EXPECT_NONE:
            expectations.add(f"{topic}:{expectation}")

    return {
        "recorded": True,
        "topics": sorted(topics),
        "signals": sorted(signals),
        "expectations": sorted(expectations),
    }


def read_macro_links(docs: list[NewsDoc] | None) -> MacroDigest:
    docs = list(docs or [])
    if not docs:
        return MacroDigest(notes=["没有可用新闻，宏观链路为空"])

    coverage: dict[str, int] = {}
    links: list[MacroLink] = []
    seen: set[tuple[str, str, str, tuple[str, ...], str]] = set()
    saw_expectation = False

    for doc in docs:
        text = str(getattr(doc, "text", "") or "").strip()
        if not text:
            continue
        for category in _categories_of(text):
            coverage[category] = coverage.get(category, 0) + 1
        expectation = expectation_of(text)
        if expectation != EXPECT_NONE:
            saw_expectation = True

        for link in _links_from_doc(doc, expectation):
            key = (link.topic, link.trigger, link.mechanism, link.targets, link.direction)
            if key in seen:
                continue
            seen.add(key)
            links.append(link)

    notes: list[str] = []
    if saw_expectation:
        notes.append("预期差只回答标题里的超预期/不及预期，不判断是否已被股价提前兑现")
    if not links:
        notes.append("未识别到确定性的宏观传导链；原始新闻仍可交给外部 AI 做自由分析")
    return MacroDigest(
        ok=bool(links), links=links[:12], coverage=dict(sorted(coverage.items())), notes=notes
    )


def expectation_of(text: str) -> str:
    if _contains_any(text, EXPECT_BEAT_TERMS):
        return EXPECT_BEAT
    if _contains_any(text, EXPECT_MISS_TERMS):
        return EXPECT_MISS
    if _contains_any(text, EXPECT_INLINE_TERMS):
        return EXPECT_INLINE
    return EXPECT_NONE


def _links_from_doc(doc: NewsDoc, expectation: str) -> list[MacroLink]:
    text = doc.text
    title = doc.title or doc.text[:48]
    evidence = (doc.title[:24] if doc.title else text[:24],)
    links: list[MacroLink] = []

    links.extend(_commodity_links(text, title, evidence, expectation))
    links.extend(_global_market_links(text, title, evidence, expectation))
    links.extend(_product_links(text, title, evidence, expectation))

    if _contains_any(text, ("美元指数", "美元", "人民币", "离岸人民币")):
        direction = _fx_direction(text)
        if direction != "mixed":
            links.append(
                MacroLink(
                    topic=TOPIC_USD,
                    trigger=title,
                    mechanism="美元与汇率变化",
                    targets=("成长股估值", "人民币资产风险偏好"),
                    direction="bearish" if direction == "bullish" else "bullish",
                    evidence=evidence,
                    expectation=expectation,
                )
            )

    if _contains_any(text, HAWKISH_TERMS):
        links.append(
            MacroLink(
                topic=TOPIC_RATES,
                trigger=title,
                mechanism="利率偏鹰",
                targets=("成长股估值", "黄金"),
                direction="bearish",
                evidence=evidence,
                expectation=expectation,
            )
        )
    elif _contains_any(text, DOVISH_TERMS):
        links.append(
            MacroLink(
                topic=TOPIC_RATES,
                trigger=title,
                mechanism="利率偏鸽",
                targets=("成长股估值", "黄金"),
                direction="bullish",
                evidence=evidence,
                expectation=expectation,
            )
        )

    if _contains_any(text, ("PMI", "社融", "信贷", "进出口", "GDP", "CPI", "PPI", "工业增加值")):
        direction = _macro_data_direction(text, expectation, china=True)
        if direction != "mixed":
            links.append(
                MacroLink(
                    topic=TOPIC_CHINA_MACRO,
                    trigger=title,
                    mechanism="国内宏观数据变化",
                    targets=("顺周期", "工业金属", "机械设备"),
                    direction=direction,
                    evidence=evidence,
                    expectation=expectation,
                )
            )

    if _contains_any(text, ("非农", "美债", "美国CPI", "美国PCE", "美国失业率", "美国通胀")):
        direction = _macro_data_direction(text, expectation, china=False)
        if direction != "mixed":
            links.append(
                MacroLink(
                    topic=TOPIC_US_MACRO,
                    trigger=title,
                    mechanism="美国宏观数据影响利率预期",
                    targets=("成长股估值", "黄金"),
                    direction=direction,
                    evidence=evidence,
                    expectation=expectation,
                )
            )

    if (
        _contains_any(text, ("财报", "业绩", "中报", "半年报", "业绩预告", "业绩快报"))
        and expectation != EXPECT_NONE
    ):
        links.append(
            MacroLink(
                topic=TOPIC_EARNINGS,
                trigger=title,
                mechanism="业绩预期差",
                targets=("相关板块情绪",),
                direction="bullish"
                if expectation == EXPECT_BEAT
                else "bearish"
                if expectation == EXPECT_MISS
                else "mixed",
                evidence=evidence,
                expectation=expectation,
            )
        )
    return links


def _commodity_links(
    text: str, title: str, evidence: tuple[str, ...], expectation: str
) -> list[MacroLink]:
    links: list[MacroLink] = []
    direction = _price_direction(text)
    if _contains_any(text, ("原油", "布伦特", "WTI")) and direction != "mixed":
        upstream = ("石油石化", "油服", "油运") if direction == "bullish" else ("石油石化", "油服")
        links.append(
            MacroLink(
                TOPIC_OIL,
                title,
                "油价上行" if direction == "bullish" else "油价回落",
                upstream,
                direction,
                evidence,
                expectation,
            )
        )
        links.append(
            MacroLink(
                TOPIC_OIL,
                title,
                "能源成本抬升" if direction == "bullish" else "能源成本回落",
                ("航空", "化纤"),
                "bearish" if direction == "bullish" else "bullish",
                evidence,
                expectation,
            )
        )
    if _contains_any(text, ("黄金", "现货金", "COMEX黄金", "金价")) and direction != "mixed":
        links.append(
            MacroLink(
                TOPIC_GOLD,
                title,
                "避险与金价变化",
                ("黄金", "贵金属"),
                direction,
                evidence,
                expectation,
            )
        )
    return links


# 这是“品类 → 一级行业”的补充词典，不是穷举表。对不在这里的新材料，下面的
# `_product_name` 仍会保留品类和原始证据，只是不把它伪映射到一个行业。
PRODUCT_SECTORS: dict[str, tuple[str, ...]] = {
    "六氟磷酸锂": ("基础化工", "电力设备"),
    "氢氟酸": ("基础化工", "电子"),
    "制冷剂": ("基础化工",),
    "萤石": ("基础化工",),
    "锗": ("有色金属", "电子"),
    "镓": ("有色金属", "电子"),
    "锑": ("有色金属",),
    "铟": ("有色金属", "电子"),
    "钨": ("有色金属", "机械设备"),
    "钼": ("有色金属", "钢铁"),
    "稀土": ("有色金属",),
    "碳酸锂": ("有色金属", "电力设备"),
    "多晶硅": ("电力设备", "基础化工"),
    "生猪": ("农林牧渔",),
    "猪价": ("农林牧渔",),
    "鸡价": ("农林牧渔",),
    "纸浆": ("轻工制造",),
    "航运": ("交通运输",),
    "存储芯片": ("电子",),
    "碳化硅": ("电子", "基础化工"),
}
PRODUCT_MARKERS = tuple(sorted(PRODUCT_SECTORS, key=len, reverse=True))
PRODUCT_PATTERN = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{2,12})(?:价格|报价|现货价|期货价)")
GENERIC_PRODUCT_SUFFIXES = (
    "酸",
    "盐",
    "气",
    "矿",
    "金属",
    "钢",
    "煤",
    "油",
    "纸",
    "浆",
    "料",
    "粉",
    "砂",
    "脂",
    "胶",
    "纤维",
    "晶圆",
    "电池",
)
SUPPLY_TIGHTENING = ("停产", "停机", "检修", "减产", "供给收缩", "库存下降", "供不应求")
SUPPLY_LOOSENING = ("复产", "扩产", "库存增加", "供给过剩", "需求疲弱")


def _product_links(
    text: str, title: str, evidence: tuple[str, ...], expectation: str
) -> list[MacroLink]:
    direction = _price_direction(text)
    if direction == "mixed":
        if _contains_any(text, SUPPLY_TIGHTENING):
            direction = "bullish"
        elif _contains_any(text, SUPPLY_LOOSENING):
            direction = "bearish"
    if direction == "mixed":
        return []

    product = _product_name(text)
    if not product:
        return []
    targets = PRODUCT_SECTORS.get(product) or _sector_targets(text)
    mechanism = f"{product}价格/供需变化"
    return [
        MacroLink(
            topic=TOPIC_PRODUCT,
            trigger=title,
            mechanism=mechanism,
            targets=targets or ("相关产业链（待映射）",),
            direction=direction,
            evidence=evidence,
            expectation=expectation,
        )
    ]


def _product_name(text: str) -> str | None:
    marker = next((item for item in PRODUCT_MARKERS if item in text), None)
    if marker:
        return marker
    match = PRODUCT_PATTERN.search(text)
    if not match:
        return None
    product = match.group(1)
    # “人民币价格区间”或“电动汽车价格指数”是汇率/指数读数，并非 A 股细分品类。
    # 未知品类只有长得像原料或产品名时才保留，宁可漏掉也不制造错误传导。
    if any(char.isdigit() for char in product) or any(
        term in product for term in ("指数", "人民币", "美元", "汇率", "彭博", "就业")
    ):
        return None
    if product.startswith("未知") or product.endswith(GENERIC_PRODUCT_SUFFIXES):
        return product
    return None


def _sector_targets(text: str) -> tuple[str, ...]:
    return tuple(
        sector for sector, terms in SECTOR_KEYWORDS.items() if any(term in text for term in terms)
    )[:3]


def _global_market_links(
    text: str, title: str, evidence: tuple[str, ...], expectation: str
) -> list[MacroLink]:
    """有固定 A 股映射的海外、地缘与政策信号，不替代原始新闻核验。"""
    links: list[MacroLink] = []
    price_mappings = (
        (
            TOPIC_METALS,
            ("铜", "COMEX铜", "伦铜", "铜价", "铝价", "锂价", "稀土"),
            "工业金属与资源品价格变化",
            ("有色金属", "资源股", "新能源上游"),
        ),
        (
            TOPIC_US_EQUITIES,
            ("纳斯达克", "标普500", "道琼斯", "费城半导体", "费半"),
            "美股风险偏好与科技映射",
            ("AI算力", "半导体", "消费电子", "成长股估值"),
        ),
        (
            TOPIC_KOREA,
            ("韩国股市", "韩股", "KOSPI", "KOSDAQ", "三星电子", "SK海力士"),
            "韩股及存储/电子产业链映射",
            ("半导体", "存储芯片", "消费电子", "面板"),
        ),
    )
    for topic, terms, mechanism, targets in price_mappings:
        direction = _price_direction(text) if _contains_any(text, terms) else "mixed"
        if direction != "mixed":
            links.append(
                MacroLink(topic, title, mechanism, targets, direction, evidence, expectation)
            )

    if _contains_any(
        text,
        ("关税", "制裁", "出口管制", "实体清单", "科技封锁", "中美", "台海", "中东局势", "红海"),
    ):
        links.append(
            MacroLink(
                TOPIC_GEOPOLITICS,
                title,
                "地缘与贸易限制改变风险偏好或国产替代预期",
                ("军工", "自主可控", "半导体", "航运", "能源"),
                "mixed",
                evidence,
                expectation,
            )
        )

    if _contains_any(
        text,
        (
            "中央汇金",
            "国家队",
            "增持ETF",
            "ETF增持",
            "平准基金",
            "维稳",
            "稳定资本市场",
            "资本市场改革",
            "产业支持",
        ),
    ):
        links.append(
            MacroLink(
                TOPIC_POLICY,
                title,
                "政策或稳定市场信号",
                ("宽基指数", "政策受益板块", "市场风险偏好"),
                "bullish",
                evidence,
                expectation,
            )
        )
    return links


def _categories_of(text: str) -> tuple[str, ...]:
    categories: list[str] = []
    mapping = (
        (TOPIC_OIL, ("原油", "布伦特", "WTI")),
        (TOPIC_GOLD, ("黄金", "现货金", "COMEX黄金", "金价")),
        (TOPIC_USD, ("美元指数", "美元", "人民币", "离岸人民币")),
        (TOPIC_RATES, HAWKISH_TERMS + DOVISH_TERMS),
        (TOPIC_CHINA_MACRO, ("PMI", "社融", "信贷", "进出口", "GDP", "CPI", "PPI", "工业增加值")),
        (TOPIC_US_MACRO, ("非农", "美债", "美国CPI", "美国PCE", "美国失业率", "美国通胀")),
        (TOPIC_EARNINGS, ("财报", "业绩", "中报", "半年报", "业绩预告", "业绩快报")),
        (TOPIC_US_EQUITIES, ("纳斯达克", "标普500", "道琼斯", "费城半导体", "费半")),
        (TOPIC_KOREA, ("韩国股市", "韩股", "KOSPI", "KOSDAQ", "三星电子", "SK海力士")),
        (TOPIC_METALS, ("铜", "COMEX铜", "伦铜", "铜价", "铝价", "锂价", "稀土")),
        (
            TOPIC_GEOPOLITICS,
            (
                "关税",
                "制裁",
                "出口管制",
                "实体清单",
                "科技封锁",
                "中美",
                "台海",
                "中东局势",
                "红海",
            ),
        ),
        (
            TOPIC_POLICY,
            (
                "中央汇金",
                "国家队",
                "增持ETF",
                "ETF增持",
                "平准基金",
                "维稳",
                "稳定资本市场",
                "资本市场改革",
                "产业支持",
            ),
        ),
    )
    for name, terms in mapping:
        if _contains_any(text, terms):
            categories.append(name)
    return tuple(categories)


def _price_direction(text: str) -> str:
    up = _contains_any(text, UP_TERMS)
    down = _contains_any(text, DOWN_TERMS)
    if up and not down:
        return "bullish"
    if down and not up:
        return "bearish"
    return "mixed"


def _macro_data_direction(text: str, expectation: str, *, china: bool) -> str:
    if expectation == EXPECT_BEAT:
        return "bullish" if china else "bearish"
    if expectation == EXPECT_MISS:
        return "bearish" if china else "bullish"
    if "PMI" in text:
        if "50." in text or "51" in text or "52" in text or "53" in text or "54" in text:
            return "bullish"
        if "49." in text or "48." in text or "47." in text:
            return "bearish"
    return "mixed"


def _fx_direction(text: str) -> str:
    dollar_up = ("美元指数" in text or "美元" in text) and _contains_any(text, UP_TERMS)
    dollar_down = ("美元指数" in text or "美元" in text) and _contains_any(text, DOWN_TERMS)
    rmb_down = ("人民币" in text or "离岸人民币" in text) and _contains_any(text, DOWN_TERMS)
    rmb_up = ("人民币" in text or "离岸人民币" in text) and _contains_any(text, UP_TERMS)

    if dollar_up or rmb_down:
        return "bullish"
    if dollar_down or rmb_up:
        return "bearish"
    return "mixed"


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)
