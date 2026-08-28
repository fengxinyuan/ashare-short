"""个股舆情的相关性闸门：区分「关于这家公司」与「顺带提到这家公司」。

**为什么需要它。** `stock_news_em` 返回的是东财个股页面挂的文章，其中很大一部分是
**行业综述、资金流榜单、概念涨幅榜**——它们把几十只票列在正文里，于是每一只都
「有新闻」。这类文档进到 `SymbolNewsAgent` 之后和真正的公司新闻同等对待，而词典
是对整段文本打分的，于是**别家公司的利好利空被记到了这只票头上**。

2026-08-02 实测 14 只标的，过完 72 小时时效闸门后存活 11 条个股文档，
**标题点名本公司的 0 条**，全部是综述与榜单。其中三条还产生了方向：

| 标的 | 读数 | 实际出处 |
|---|---|---|
| 601519 大智慧 | **-1.0（立案）** | 立案的是**湘财证券、国盛证券** |
| 600900 长江电力 | **+1.0（中标）** | 中标的是**天溯计量** |
| 601939 建设银行 | +1.0（新高） | 「工行、建行、交行股价创年内新高」榜单 |

大智慧那条一路走到了盘前简报和台账里：`个股舆情 1 条偏空（-1.2，关键词：立案）`。
一家公司因为另外两家被证监会立案而扣了分——这不是权重问题，是**归属错了**。

**判据是标题，不是正文。** 综述与榜单的共同特征恰恰是「正文里有、标题里没有」：
标题写的是行业或概念（「传媒行业资金流入榜」「财税数字化概念上涨6.43%」），公司名
只出现在被列举的那串里。真正的公司新闻反过来——标题一定点名（「中国神华：定州三期
机组全部建成投运」「牧原股份：董事及高管增持计划实施完毕」）。所以标题点名是这两类
之间**唯一稳定**的分界，而且它不需要理解语义。

**一个例外：标题用「公司」「控股股东」自指的也算。** 「控股股东拟大幅减持，公司收到
问询函」显然是这家公司的事，只是标题用代词指自己——这是公司公告最常见的写法。只认
公司名会把减持与问询一起挡掉，而那正是个股舆情里最该看到的东西。实测那 11 条综述
没有一条命中这些自指词，所以这个放宽不会把要挡的那批放回来。

**不知道名字时放行。** 拿不到证券名称（master 查询失败）就没有判据，此时保持原样
放行而不是一律丢弃——闸门要挡的是「明确不相关」的文档，不是「无法判断」的文档。
这和 `jobs/sources.py:fetch_market_scan` 对数据不足时的处理是同一条约定。

**闸门只影响打分。** `news/daily_read.py` 那一层拿的是未经闸门与 72 小时截断的原始
个股文档——打分要回答「最近有没有事」，通读要回答「这只票是什么情况」，两者该看的
窗口本来就不同。

**公告不走这条闸门。** 交易所公告本来就是按代码挂到标的上的，标题是「年报｜2025 年
年度报告」这种形式，不含公司名。它们在 `SymbolNewsAgent` 里单独并入，不经过这里。
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
#
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
