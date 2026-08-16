"""舆情通读（`news/daily_read.py`）与个股相关性闸门（`news/relevance.py`）。

这两层的共同点是**它们都可能悄悄地错**：通读会编出一条读着很顺但原文里没有的
传导链；相关性闸门会把「传媒行业资金流入榜」当成某只票自己的消息。两者出错时
都不会抛异常，所以只能靠测试把「必须丢弃」的那些情况钉死。
"""

from __future__ import annotations

from datetime import date

from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.news.daily_read import (
    MAX_MARKET_HEADLINES,
    SECTION_QUOTAS,
    DailyNewsReader,
    DailyRead,
    route_of,
)
from ashare_decision_support.news.relevance import (
    company_aliases,
    filter_company_news,
    is_about,
)


class FakeClient:
    """按脚本回答的 LLM 客户端。`configured` 可关，用来测未配 key 的路径。"""

    def __init__(self, reply: str = "", configured: bool = True) -> None:
        self.reply = reply
        self.configured = configured
        self.prompts: list[str] = []

    def chat(self, messages: list[dict]) -> str:
        self.prompts.append(messages[-1]["content"])
        return self.reply


def doc(title: str, published: str = "2026-08-02 09:00:00", **kwargs) -> NewsDoc:
    return NewsDoc(title=title, published_at=published, **kwargs)


# ---- 相关性闸门 -----------------------------------------------------------


def test_a_sector_roundup_is_not_this_company_s_own_news():
    """标题不点名的综述/榜单不算个股舆情。

    2026-08-02 实测：自选股与候选池 14 只过完时效闸门只剩 11 条个股文档，
    **标题点名本公司的 0 条**，全是这类。其中「因涉嫌违反账户实名制规定，湘财
    证券、国盛证券被证监会立案」被记到了大智慧头上，在盘前简报里印成
    「个股舆情 1 条偏空（-1.2，关键词：立案）」——扣分的理由是别人被立案。
    """
    roundup = doc("因涉嫌违反账户实名制规定，湘财证券、国盛证券被证监会立案")
    assert is_about(roundup, "601519.SH", "大智慧") is False

    own = doc("大智慧：2026年半年度业绩预告")
    assert is_about(own, "601519.SH", "大智慧") is True

    about, mentions = filter_company_news([roundup, own], "601519.SH", "大智慧")
    assert [d.title for d in about] == [own.title]
    assert [d.title for d in mentions] == [roundup.title]


def test_the_gate_lets_everything_through_when_the_name_is_unknown():
    """拿不到证券简称时没有判据，放行而不是清零。

    闸门要挡的是「明确不相关」，不是「无法判断」——和 `jobs/morning.py`
    对回调档位算不出来时的处理同一条约定。
    """
    item = doc("传媒行业资金流入榜：蓝色光标等15股净流入资金超亿元")
    assert is_about(item, "002517.SZ", None) is True
    about, mentions = filter_company_news([item], "002517.SZ", "")
    assert about and not mentions


def test_a_short_form_of_the_name_still_counts_as_naming_the_company():
    """「牧原等猪企迎利好」点的就是牧原股份。

    去掉 `股份`/`集团` 之后的字号才是媒体实际引用的那个词；不认它会把真正的
    公司新闻误判成综述。
    """
    assert company_aliases("002714.SZ", "牧原股份") == ("牧原股份", "牧原", "002714")
    assert is_about(doc("牧原等猪企迎利好：能繁母猪触政策底线"), "002714.SZ", "牧原股份")


def test_a_two_character_stem_is_not_shortened_further():
    """短名不再剥后缀，否则「中国」这种前缀会放行一切。"""
    assert company_aliases("600900.SH", "长江电力") == ("长江电力", "600900")
    assert is_about(doc("6只股收盘价创历史新高"), "600900.SH", "长江电力") is False


# ---- 通读：路由与配额 -----------------------------------------------------


def test_routing_sends_each_headline_to_the_section_that_explains_it():
    assert route_of(doc("韩国首尔综指涨超17%，SK海力士涨停")) == "海外市场"
    assert route_of(doc("特朗普宣布对进口钢铁加征关税")) == "美国政治与货币"
    assert route_of(doc("微软Q2云业务增长超预期")) == "海外公司"
    assert route_of(doc("以色列袭击伊朗核设施")) == "地缘与安全"
    assert route_of(doc("布伦特原油涨3%")) == "大宗与汇率"
    assert route_of(doc("国常会部署有效扩大国内需求")) == "国内政策"
    assert route_of(doc("中国7月官方制造业PMI 49.2")) == "宏观数据"
    assert route_of(doc("某公司披露异动公告")) == "A股与产业"


def test_overseas_headlines_survive_a_corpus_dominated_by_a_share_chatter():
    """配额取样，不是纯按新鲜度截断。

    实测语料里 A 股盘面快讯占绝大多数且时间最新，纯按时间取前 N 条会把海外那几节
    挤到个位数——而当天最关键的传导链（韩股存储涨停 → A 股半导体）恰恰在海外节里，
    且发生在更早的交易日。让这一层读不到海外，等于白做。
    """
    corpus = [
        doc(f"A股盘面快讯{i}", published=f"2026-08-02 1{i % 10}:00:00") for i in range(400)
    ]
    corpus += [
        doc(f"韩国首尔综指涨超{i}%", published="2026-07-31 15:00:00") for i in range(30)
    ]
    reader = DailyNewsReader(FakeClient(reply="{}"))
    reader.read(corpus, date(2026, 8, 2))

    prompt = reader.client.prompts[0]
    overseas = prompt.count("韩国首尔综指涨超")
    assert overseas == min(30, SECTION_QUOTAS["海外市场"]), overseas
    assert prompt.count("A股盘面快讯") <= MAX_MARKET_HEADLINES


# ---- 通读：核对与丢弃 -----------------------------------------------------


def _reader(reply: str) -> DailyNewsReader:
    return DailyNewsReader(FakeClient(reply=reply))


CORPUS = [
    doc("以色列袭击伊朗核设施，中东局势升级"),
    doc("韩国7月出口同比 62.8%，预期 59%"),
]


def test_a_chain_that_cannot_cite_the_corpus_is_dropped():
    """引用不到原文的结论一律丢弃——和 `llm_classifier` 丢对不上锚点的那条同理。"""
    reply = """{"chains":[
      {"trigger":"某个没人报道过的事","mechanism":"凭空推理","targets":["石油石化"],
       "direction":"bullish","evidence":["这句话原文里没有"]}],
      "mainlines":[],"risks":[],"holdings":[],"summary":""}"""
    result = _reader(reply).read(CORPUS, date(2026, 8, 2))
    assert result.ok is False
    assert not result.chains
    assert any("引用不到原文" in note for note in result.notes)


def test_a_chain_that_quotes_the_corpus_survives_with_its_evidence():
    reply = """{"chains":[
      {"trigger":"以色列袭击伊朗核设施","mechanism":"原油供给风险溢价",
       "targets":["石油石化","油运"],"direction":"bullish",
       "evidence":["以色列袭击伊朗核设施"]}],
      "mainlines":[],"risks":[],"holdings":[],"summary":"地缘推升油价"}"""
    result = _reader(reply).read(CORPUS, date(2026, 8, 2))
    assert result.ok is True
    chain = result.chains[0]
    assert chain.targets == ("石油石化", "油运")
    assert chain.direction == "bullish"
    assert chain.evidence
    assert "石油石化" in result.markdown()


def test_an_invented_number_discards_the_whole_read():
    """数字闸门与 `writer/llm.py` 同口径：原文里查不到的数字，整份丢弃。

    这是这一层唯一的硬约束——它可以自由推理因果，但不能凭空造出一个数。
    """
    reply = """{"chains":[
      {"trigger":"韩国7月出口同比 62.8%","mechanism":"存储周期共振",
       "targets":["半导体"],"direction":"bullish",
       "evidence":["韩国7月出口同比"]}],
      "mainlines":[],"risks":[],
      "holdings":[],"summary":"预计A股半导体今日高开3.7%"}"""
    result = _reader(reply).read(CORPUS, date(2026, 8, 2))
    assert result.ok is False
    assert any("数据中不存在的数字" in note for note in result.notes)


def test_a_quoted_number_is_allowed_because_it_is_in_the_corpus():
    reply = """{"chains":[],"mainlines":[
      {"text":"韩国7月出口同比 62.8% 高于预期，存储链景气","evidence":["韩国7月出口同比"]}],
      "risks":[],"holdings":[],"summary":""}"""
    result = _reader(reply).read(CORPUS, date(2026, 8, 2))
    assert result.ok is True
    assert result.mainlines[0].text.startswith("韩国7月出口")


def test_a_holdings_item_for_a_symbol_that_was_not_asked_about_is_dropped():
    """模型只能对喂进去的标的发言，不能自己添一只。"""
    reply = """{"chains":[],"mainlines":[],"risks":[],
      "holdings":[
        {"symbol":"600519.SH","text":"茅台大涨","evidence":["以色列袭击伊朗核设施"]},
        {"symbol":"601939.SH","text":"建行发行二级资本债","evidence":["建设银行完成发行"]}],
      "summary":""}"""
    symbol_docs = {"601939.SH": [doc("建设银行完成发行600亿元二级资本债券")]}
    result = _reader(reply).read(CORPUS, date(2026, 8, 2), symbol_docs=symbol_docs)
    assert [item.symbol for item in result.holdings] == ["601939.SH"]


def test_a_malformed_reply_degrades_to_an_empty_read():
    for reply in ("", "抱歉，我无法完成", "[]"):
        result = _reader(reply).read(CORPUS, date(2026, 8, 2))
        assert result.ok is False
        assert result.markdown() == ""
        assert result.notes


def test_no_key_means_no_read_and_no_exception():
    reader = DailyNewsReader(FakeClient(configured=False))
    result = reader.read(CORPUS, date(2026, 8, 2))
    assert result.ok is False
    assert not reader.client.prompts
    assert any("未配置" in note for note in result.notes)


def test_an_empty_read_renders_nothing():
    assert DailyRead().markdown() == ""


def test_company_own_news_survives_even_without_repeating_the_name():
    """「控股股东拟大幅减持，公司收到问询函」显然是这家公司的事。

    标题用「公司」代指自己是个股公告最常见的写法。只认公司名会把减持与问询
    一起挡掉——而那恰恰是个股舆情里最该看到的东西。
    """
    own = doc("控股股东拟大幅减持，公司收到问询函")
    assert is_about(own, "600519.SH", "贵州茅台") is True


def test_self_reference_does_not_let_the_roundups_back_in():
    """实测的 11 条综述/榜单没有一条命中自指词，所以放宽不会撤销那条修复。"""
    roundups = [
        "银行行业资金流出榜：工商银行、平安银行等净流出资金居前",
        "A股三大股指收涨：多模态AI概念股爆发，近4700股飘红",
        "华为盘古概念上涨6.69%，7股主力资金净流入超5000万元",
        "天溯计量披露异动公告：内外部经营环境未现重大变化 中标项目影响有限",
        "因涉嫌违反账户实名制规定，湘财证券、国盛证券被证监会立案",
    ]
    for title in roundups:
        assert is_about(doc(title), "601519.SH", "大智慧") is False, title
