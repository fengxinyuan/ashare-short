"""个股新闻相关性筛选：标题点名公司，或用公司、控股股东等自指词。

行业综述与榜单的正文提及不足以归属该公司，避免误用其他公司的利好利空。
证券简称未知时放行；按代码关联的公告单独处理。
此闸门用于短期情绪评分，通读仍可使用更宽的原始材料。
"""

from __future__ import annotations

from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.symbols import bare_code

# 公司简称常见后缀。「牧原股份：董事增持」标题点名，但市场与媒体常写「牧原等猪企
# 迎利好」——去掉后缀之后的字号才是实际被引用的那个词。只剥这两个最规整的，
# 剥得越多越容易撞车（「同仁堂」剥不出东西，剥了反而危险）。
_STEM_SUFFIXES = ("股份有限公司", "股份", "集团")

# 剥完后缀至少要剩这么多字才当作可用字号。两字以下会误伤：「中国」「华夏」这类
# 前缀满大街都是，拿它去匹配标题等于放行一切。
MIN_STEM_CHARS = 2


# 标题里出现这些词，说明这篇是**站在公司自己的角度**写的，即使没重复公司名。
# 「控股股东拟大幅减持，公司收到问询函」就是这一类：它显然是这家公司的事，只是
# 标题用「公司」代指。少了这一条，闸门会把真正的减持与问询公告一起挡掉，而那正是
# 个股舆情里最该看到的东西。

# 实测的 11 条综述/榜单没有一条命中这些词——它们说的是「行业」「概念」「榜」，
# 或者点的是**别家**公司的名字，所以这一条不会把要挡的那批放回来。
_SELF_REFERENCE = (
    "本公司",
    "控股股东",
    "实控人",
    "实际控制人",
    "董事会",
    "监事会",
    "股东大会",
    "公司公告",
    "公司收到",
    "公司拟",
    "公司与",
)


def company_aliases(symbol: str, name: str | None) -> tuple[str, ...]:
    """这只票在标题里可能长什么样：证券简称、去后缀字号、六位代码。"""
    aliases: list[str] = []
    clean = (name or "").strip()
    if clean:
        aliases.append(clean)
        for suffix in _STEM_SUFFIXES:
            if clean.endswith(suffix):
                stem = clean[: -len(suffix)]
                if len(stem) >= MIN_STEM_CHARS:
                    aliases.append(stem)
                break
    code = bare_code(symbol)
    if code:
        aliases.append(code)
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def is_about(doc: NewsDoc, symbol: str, name: str | None) -> bool:
    """标题是否点名这只票，或以公司自身口吻在写。名称未知时一律 True。"""
    if not name or not name.strip():
        # 只剩代码可比，而代码几乎从不出现在标题里——此时没有有效判据，放行。
        return True
    title = doc.title or ""
    if any(alias in title for alias in company_aliases(symbol, name)):
        return True
    return any(marker in title for marker in _SELF_REFERENCE)


def filter_company_news(
    docs: list[NewsDoc], symbol: str, name: str | None
) -> tuple[list[NewsDoc], list[NewsDoc]]:
    """拆成 (真正关于这家公司的, 顺带提及的)。两边都返回，调用方决定怎么用。

    丢掉的那批**不是垃圾**——「传媒行业资金流入榜」对板块热度是有效输入，只是它
    不该被当成某一只票自己的消息。所以这里只做归类，不做删除。
    """
    if not docs:
        return [], []
    about: list[NewsDoc] = []
    mentions: list[NewsDoc] = []
    for doc in docs:
        (about if is_about(doc, symbol, name) else mentions).append(doc)
    return about, mentions
