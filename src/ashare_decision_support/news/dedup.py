"""去重与时效：把「采到多少条」变成「有多少条还算数」。

采集层解决的是能不能拿到，这一层解决的是拿到的东西该给多少分量。两件事都不做的
后果在实测里很具体：

- **跨源重复。** 财联社电报与东财全球快讯大量转发同一条内容，标题常常一字不差。
  同一个故事被计三次，板块的「讨论热度」就凭空翻三倍，而热度是要进打分的。
- **陈旧文档。** `stock_info_global_cls` 返回的是滚动列表，跨好几天。超短线里
  三天前的电报和半小时前的电报权重相同，等于让上周的故事参与今天的排序。

所以每条文档带一个 [0,1] 的权重：重复的直接丢，老的按半衰期折减，超过 `MAX_AGE_HOURS`
的整条丢弃——五天前的快讯出现在超短线简报里，是噪声不是信息。

**时间戳缺失不折减。** 新闻联播只给日期、部分源根本不给时间。把「不知道多老」
当成「很老」会把这些源整体抹掉；当成「很新」只是不折减，错得温和得多。这类文档
会单独计数并写进 note，让人知道有多少读数没经过时效校验。
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime

from ashare_decision_support.news.base import NewsDoc

# 权重减半所需的小时数。盘前 08:20 跑批看的是隔夜舆情，12 小时意味着昨天此刻的
# 消息还剩一半、前天此刻剩四分之一——和「隔夜」这个窗口对得上。
DEFAULT_HALF_LIFE_HOURS = 12.0

# 超过这个年龄整条丢弃，不再参与任何聚合。
MAX_AGE_HOURS = 72.0

# 时间戳解析格式，按出现频率排。各源实测形态：
#   财联社   发布日期 + 发布时间 → "2026-08-01 09:31:20"
#   东财快讯 发布时间           → "2026-08-01 09:31:20"
#   新闻联播 date               → "20260801"
#   个股新闻 发布时间           → "2026-08-01 09:31:20"
_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y%m%d%H%M%S",
    "%Y%m%d",
)

# 去重前先剥掉的装饰：全半角标点与空白。剥掉之后
# "央行开展 5000 亿元逆回购！" 与 "央行开展5000亿元逆回购" 同键。
_STRIP_PATTERN = re.compile(r"[\s　【】\[\]（）()《》<>「」“”\"'‘’|｜:：,，.。!！?？、~—\-_*#]+")

# 标题开头的方括号标签几乎总是来源或栏目名（【财联社】【独家】），不是内容。
# 只剥**开头**这一处：出现在标题中间的方括号往往是内容的一部分。
_LEADING_TAG = re.compile(r"^\s*[【\[（(]([^】\]）)]{1,12})[】\]）)]\s*")


def parse_published(value: str) -> datetime | None:
    """把各源五花八门的时间串解析成 datetime，解析不了就是 None。"""
    text = (value or "").strip()
    if not text:
        return None
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # 少数源会带毫秒或时区尾巴，截断到秒再试一次。
    trimmed = text[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(trimmed, fmt)
        except ValueError:
            continue
    return None


def recency_weight(
    published: datetime | None,
    now: datetime,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
) -> float:
    """半衰期折减，落在 [0, 1]。

    未知时间戳返回 1.0（见模块开头）。未来时间戳同样返回 1.0——那是时钟偏差或
    解析错误，不是「更新鲜」，让它超过 1.0 会把解析 bug 变成加分项。
    """
    if published is None:
        return 1.0
    age_hours = (now - published).total_seconds() / 3600.0
    if age_hours <= 0:
        return 1.0
    if age_hours >= MAX_AGE_HOURS:
        return 0.0
    if half_life_hours <= 0:
        return 1.0
    return round(0.5 ** (age_hours / half_life_hours), 4)


def dedup_key(doc: NewsDoc) -> str:
    """标题归一化后的去重键；标题为空时退回摘要前 40 字。"""
    base = doc.title or doc.summary[:40]
    stripped = _LEADING_TAG.sub("", base, count=1)
    # 整条标题就是一个方括号标签时不剥，剥完就没内容了。
    return _STRIP_PATTERN.sub("", stripped or base)


def weigh_docs(
    docs: list[NewsDoc],
    now: datetime,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
) -> tuple[list[NewsDoc], list[str]]:
    """去重 + 打时效权重，返回 (文档, notes)。

    重复项保留**最早发布**的那条，因为原发比转发更接近事件时间；转发时间会让
    一条隔夜消息看起来像是刚发生的。
    """
    if not docs:
        return [], []

    best: dict[str, tuple[NewsDoc, datetime | None]] = {}
    order: list[str] = []
    duplicates = 0
    undated = 0

    for doc in docs:
        key = dedup_key(doc)
        if not key:
            continue
        published = parse_published(doc.published_at)
        if published is None:
            undated += 1

        existing = best.get(key)
        if existing is None:
            best[key] = (doc, published)
            order.append(key)
            continue

        duplicates += 1
        _kept_doc, kept_at = existing
        if published is not None and (kept_at is None or published < kept_at):
            best[key] = (doc, published)

    weighted: list[NewsDoc] = []
    expired = 0
    for key in order:
        doc, published = best[key]
        weight = recency_weight(published, now, half_life_hours)
        if weight <= 0.0:
            expired += 1
            continue
        weighted.append(replace(doc, weight=weight))

    notes: list[str] = []
    if duplicates:
        notes.append(f"跨源重复舆情合并 {duplicates} 条，同一条消息不再重复计权")
    if expired:
        notes.append(f"超过 {MAX_AGE_HOURS:.0f} 小时的舆情丢弃 {expired} 条")
    if undated:
        notes.append(f"{undated} 条舆情无可解析发布时间，未做时效折减")
    return weighted, notes


def freshest(docs: list[NewsDoc]) -> datetime | None:
    """一批文档里最新的发布时间，用于在简报里标注舆情的 as-of。"""
    stamps = [parse_published(doc.published_at) for doc in docs]
    usable = [stamp for stamp in stamps if stamp is not None]
    return max(usable) if usable else None


def age_hours(published: datetime | None, now: datetime) -> float | None:
    if published is None:
        return None
    return round(max(0.0, (now - published).total_seconds() / 3600.0), 1)
