"""Official policy / regulatory listing source."""

from __future__ import annotations

from datetime import date

from ashare_decision_support.news.official_source import OfficialListing, OfficialNewsSource
from tests.fakes import PageOpener as Http

PAGE = """
<ul>
  <li><a href="/policy/1.html"><span>国务院发布扩大内需行动方案</span></a><span>2026-08-07</span></li>
  <li><a href="/policy/1.html">国务院发布扩大内需行动方案</a> 2026/08/07</li>
  <li><a href="/nav.html">政策</a> 2026-08-07</li>
</ul>
"""


def test_listing_maps_dated_headlines_with_canonical_urls() -> None:
    listing = OfficialListing("国务院政策", "https://official.example/list", "国务院")
    source = OfficialNewsSource(opener=Http({listing.url: PAGE}), listings=(listing,))

    docs, notes = source.fetch_market(date(2026, 8, 8))

    assert notes == []
    assert len(docs) == 1
    assert docs[0].title == "国务院发布扩大内需行动方案"
    assert docs[0].published_at == "2026-08-07"
    assert docs[0].url == "https://official.example/policy/1.html"
    assert docs[0].source == "国务院"


def test_one_failed_official_listing_keeps_the_others() -> None:
    live = OfficialListing("国务院政策", "https://official.example/live", "国务院")
    dead = OfficialListing("中国证监会", "https://official.example/dead", "中国证监会")
    source = OfficialNewsSource(
        opener=Http({live.url: PAGE}, dead={dead.url}), listings=(live, dead)
    )

    docs, notes = source.fetch_market(date(2026, 8, 8))

    assert len(docs) == 1
    assert any("中国证监会" in note for note in notes)


def test_source_balances_each_official_publisher() -> None:
    pbc = OfficialListing("中国人民银行", "https://official.example/pbc", "中国人民银行")
    csrc = OfficialListing("中国证监会", "https://official.example/csrc", "中国证监会")
    many = "".join(
        f'<a href="/{i}.html">中国人民银行第{i}项公开市场操作公告</a>2026-08-07' for i in range(5)
    )
    one = '<a href="/a.html">证监会发布上市公司监管规则适用指引</a>2026-08-07'
    source = OfficialNewsSource(opener=Http({pbc.url: many, csrc.url: one}), listings=(pbc, csrc))

    docs, _ = source.fetch_market(date(2026, 8, 8), limit=3)

    assert len(docs) == 3
    assert sum(doc.source == "中国证监会" for doc in docs) == 1


# 站点噪声：带日期、带链接，和一条公告只差在长得不像标题


def _one(page: str) -> list:
    listing = OfficialListing("国务院政策", "https://official.example/list", "国务院")
    source = OfficialNewsSource(opener=Http({listing.url: page}), listings=(listing,))
    docs, _ = source.fetch_market(date(2026, 8, 8))
    return docs


def test_script_bodies_never_become_headlines() -> None:
    """`<script>` 的**内容**要一起去掉，不能只去那两个尖括号。

    实测央行页面因此产出过一条 1121 字的「标题」，正文是
    `var wa='RSS订阅'; document.write(wa)`。这种东西进了语料，词典打分会照常
    在里面找多空词——它不会报错，只会让这一路的舆情读数是个随机数。
    """
    page = """
    <a href="/1.html"><script>var wa='RSS订阅利好增长'; document.write(wa);</script>
    国务院印发扩大内需行动方案</a> 2026-08-07
    """
    docs = _one(page)

    assert [doc.title for doc in docs] == ["国务院印发扩大内需行动方案"]


def test_navigation_bar_is_not_a_publication() -> None:
    """导航条与页脚同样带日期带链接，只能靠长度和分隔符认出来。"""
    nav = "首页 | 简 | 繁 | EN | 登录 个人中心 退出 | 邮箱 | 无障碍 EN"
    more = "更多>> 国务院公报 政策解读 新华时评：“发力提效”释放鲜明政策信号"
    footer = (
        "网站纠错 主办单位：国务院办公厅 运行维护单位：中国政府网运行中心 "
        + "版权所有：中国政府网 " * 6
    )
    page = "".join(
        f'<a href="/{i}.html">{text}</a> 2026-08-07'
        for i, text in enumerate([nav, more, footer, "国务院关于印发全民健身计划的通知"])
    )

    assert [doc.title for doc in _one(page)] == ["国务院关于印发全民健身计划的通知"]


def test_a_real_headline_may_contain_one_separator() -> None:
    """见竖线就丢会误伤真标题——这一条是实测存在的公告。

    所以判据是「三个以上竖线」而不是「有竖线」。写成前者是因为反例具体存在，
    不是因为想留余地。
    """
    real = "国务院常务会议解读 | 住房公积金将更好满足居民多样化住房消费需求"
    page = f'<a href="/a.html">{real}</a> 2026-08-07'

    assert [doc.title for doc in _one(page)] == [real]


def test_redirect_stub_yields_nothing_rather_than_looking_healthy() -> None:
    """跳转壳子是 HTTP 200、没有异常、一条带日期的链接都没有。

    这正是 `zhengce/zuixin.htm` 变成的样子，而这一路因此**静默产出 0 条**却
    仍被报成可达。用例钉住的是：这种页面解析结果必须是空，让上层的
    「未返回可用条目」note 有机会说话。
    """
    stub = (
        '<html><head><meta http-equiv="refresh" content="0;url=/zhengce/zuixin/home.htm">'
        '</head><body><a href="/zhengce/zuixin/home.htm">点击跳转</a></body></html>'
    )

    assert _one(stub) == []
