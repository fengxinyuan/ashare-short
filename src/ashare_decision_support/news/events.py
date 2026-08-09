"""Deterministic event evidence extracted from industry and company headlines."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

EVENT_TERMS: dict[str, tuple[str, ...]] = {
    "政策": (
        "政策支持",
        "政策利好",
        "专项规划",
        "专项债",
        "补贴",
        "试点",
        "产业规划",
        "设备更新",
        "以旧换新",
        "国产替代",
    ),
    "价格": (
        "涨价",
        "提价",
        "价格上调",
        "报价上调",
        "降价",
        "价格回落",
        "价格上涨",
        "纸价",
        "浆价",
    ),
    "供给": (
        "供给收缩",
        "产能受限",
        "停产",
        "停机",
        "检修",
        "减产",
        "库存下降",
        "库存拐点",
        "供不应求",
        "产能置换",
    ),
    "订单": ("订单", "中标", "签约", "合同", "客户导入", "批量供货"),
    "业绩": (
        "业绩预增",
        "业绩增长",
        "净利润增长",
        "扭亏",
        "超预期",
        "预告上修",
        "预亏",
        "净利润下降",
        "低于预期",
    ),
    "资本运作": ("回购", "增持", "并购", "重组", "资产注入", "定增", "股权激励"),
}

RISK_TERMS: tuple[str, ...] = (
    "立案",
    "财务造假",
    "退市",
    "风险警示",
    "减持",
    "问询函",
    "行政处罚",
    "诉讼",
    "商誉减值",
    "停牌",
)


@dataclass(frozen=True)
class EventEvidence:
    event_types: tuple[str, ...] = ()
    hits: tuple[str, ...] = ()
    risk_hits: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "event_types": list(self.event_types),
            "hits": list(self.hits),
            "risk_hits": list(self.risk_hits),
        }


def detect_events(texts: Iterable[str] | None) -> EventEvidence:
    """Extract bounded, explainable event tags from a group of texts."""
    text = " ".join(str(item) for item in (texts or []) if item)
    if not text:
        return EventEvidence()

    matched: list[tuple[str, str]] = []
    for event_type, terms in EVENT_TERMS.items():
        for term in terms:
            if term in text:
                matched.append((event_type, term))

    risk_hits = tuple(term for term in RISK_TERMS if term in text)
    return EventEvidence(
        event_types=tuple(dict.fromkeys(event_type for event_type, _ in matched)),
        hits=tuple(dict.fromkeys(term for _, term in matched)),
        risk_hits=risk_hits,
    )
