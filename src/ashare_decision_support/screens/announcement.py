"""Announcement-based risk screening.

The categories below are the exchange-assigned labels that appear in the
`stock_notice_report` feed, so this screen does not depend on parsing free
text — the category alone carries the verdict for most cases. Title matching is
kept as a second pass because the fatal events (立案调查, 财务造假) are filed
under the generic `其他` bucket rather than getting a category of their own.

Two tiers on purpose:
  BLOCKING  — removes the symbol. These cannot be traded out of.
  WARNING   — kept, but the reason travels with the candidate into the brief.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Exchange categories that end the discussion.
BLOCKING_CATEGORIES: frozenset[str] = frozenset(
    {
        "其它风险提示公告",
        "其他风险提示公告",
        "资金占用",
    }
)

# Title fragments that are fatal wherever they are filed.
BLOCKING_TITLE_TERMS: tuple[str, ...] = (
    "立案调查",
    "被立案",
    "退市风险警示",
    "终止上市",
    "暂停上市",
    "财务造假",
    "涉嫌信息披露违法",
    "行政处罚决定书",
    "市场禁入",
)

# Categories worth surfacing but not disqualifying on their own.
WARNING_CATEGORIES: frozenset[str] = frozenset(
    {
        "股票交易异常波动",
        "股份质押、冻结",
        "回复问询函公告",
        "诉讼仲裁",
        "限售股份上市流通",
    }
)

WARNING_TITLE_TERMS: tuple[str, ...] = (
    "减持",
    "问询函",
    "关注函",
    "警示函",
    "业绩预亏",
    "商誉减值",
    "解禁",
    "停牌",
)


@dataclass
class AnnouncementRisk:
    symbol: str
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocking)


class AnnouncementScreen:
    """Groups a day of whole-market announcements into per-symbol verdicts."""

    name = "announcement-screen"

    def __init__(self, block_on_title: bool = True) -> None:
        self.block_on_title = block_on_title

    def evaluate(self, announcements: list[dict]) -> dict[str, AnnouncementRisk]:
        by_symbol: dict[str, AnnouncementRisk] = {}
        for item in announcements:
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                continue
            category = str(item.get("category", "")).strip()
            title = str(item.get("title", "")).strip()
            risk = by_symbol.setdefault(symbol, AnnouncementRisk(symbol=symbol))

            if category in BLOCKING_CATEGORIES:
                risk.blocking.append(f"{category}：{title}")
                continue
            if self.block_on_title:
                hit = next((term for term in BLOCKING_TITLE_TERMS if term in title), None)
                if hit:
                    risk.blocking.append(f"{hit}：{title}")
                    continue
            if category in WARNING_CATEGORIES:
                risk.warnings.append(f"{category}：{title}")
                continue
            hit = next((term for term in WARNING_TITLE_TERMS if term in title), None)
            if hit:
                risk.warnings.append(f"{hit}：{title}")

        return {
            symbol: risk for symbol, risk in by_symbol.items() if risk.blocking or risk.warnings
        }
