"""2026-09-14 盘前实跑暴露的几处输出质量问题，每处一个用例。

来源是当天的 `briefs/morning-20260914.md`：软银那条被读成「美元走弱→偏多」、
收评给元件板块记了「退市」风险词、盈亏比 0.06 的票进了候选表、轮动观察句在
简报里印了两遍、辩论层把 `news_risk_hits` 这种键名念给读者。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from ashare_decision_support.agents.debate import PROMPTS, ROLE_BEAR, ROLE_JUDGE
from ashare_decision_support.jobs.morning_select import _flag_st, _thin_reward_risk
from ashare_decision_support.news import LexiconSentiment, NewsDoc, read_macro_links
from ashare_decision_support.news.base import is_market_wrap
from ashare_decision_support.news.macro_links import _fx_direction, _trigger_title
from ashare_decision_support.notify.render import render_sector_brief
from ashare_decision_support.sectors.base import SectorDefinition
from ashare_decision_support.sectors.predict import SectorPredictor

# 宏观联动：美元方向


def test_currency_amount_next_to_a_stock_drop_is_not_a_dollar_move() -> None:
    """「投入 650 亿美元 … 股价大跌 13%」里的「美元」是计价单位，不是美元指数。"""
    text = "软银计划在10月前向OpenAI投入近650亿美元。受此影响，软银集团股价一度大跌13%"

    assert _fx_direction(text) == "mixed"
    digest = read_macro_links([NewsDoc(title=text, published_at="2026-09-14 07:00:00")])
    assert not [link for link in digest.links if link.topic == "usd"]


def test_dollar_and_rmb_moves_still_read_when_the_verb_follows_the_currency() -> None:
    assert _fx_direction("美元指数走高，离岸人民币回落") == "bullish"
    assert _fx_direction("美元指数跌破100，人民币升值") == "bearish"
    assert _fx_direction("人民币汇率指数按周跌0.15至101.49") == "bullish"
    assert _fx_direction("离岸人民币兑美元报6.74元，日内交投于价格区间") == "mixed"


def test_trigger_title_takes_the_bracket_headline_or_the_first_sentence() -> None:
    """快讯源把整段正文塞在 title 里；简报那一行只要标题句。"""
    body = "【OpenAI推迟上市叠加AI风险警示 软银集团股价大跌】" + "奥特曼表示公司今年不会上市。" * 20
    assert _trigger_title(NewsDoc(title=body)) == "OpenAI推迟上市叠加AI风险警示 软银集团股价大跌"

    long = "中国外汇交易中心：9月11日人民币汇率指数按周跌0.15至101.49。这是连续第三周下跌，" * 3
    cut = _trigger_title(NewsDoc(title=long))
    assert cut == "中国外汇交易中心：9月11日人民币汇率指数按周跌0.15至101.49"

    short = "集装箱运价六连涨 欧美航线行情冰火两重天"
    assert _trigger_title(NewsDoc(title=short)) == short


# 板块归因：全市场综述稿


def test_market_wrap_is_not_attributed_to_any_sector() -> None:
    """收评点到元件也点到退市股；它不是元件的消息，更不该给元件记「退市」风险词。"""
    definitions = [
        SectorDefinition("元件", "THS_INDUSTRY", ("600001.SH",)),
        SectorDefinition("银行", "THS_INDUSTRY", ("600002.SH",)),
    ]
    wrap = NewsDoc(title="9月11日收评：沪指放量跌超1%，元件板块领涨，银行走弱，*ST某某锁定退市")
    own = NewsDoc(title="覆铜板涨价函落地，被动元件厂商上调价格")

    by_sector = SectorPredictor(LexiconSentiment()).attribute_docs(definitions, [wrap, own])

    assert is_market_wrap(wrap) and not is_market_wrap(own)
    assert by_sector["元件"] == [own]
    assert by_sector["银行"] == []


def test_wrap_detection_only_reads_the_title() -> None:
    assert not is_market_wrap(NewsDoc(title="A股PCB概念股局部异动，超声电子触及涨停"))
    assert not is_market_wrap(NewsDoc(title="宏昌电子拟定增18亿元", summary="据收评报道"))
    assert is_market_wrap(NewsDoc(title="财联社9月14日早间新闻精选"))


# 候选门槛：盈亏比与 ST


def test_thin_reward_risk_is_read_from_the_scorer_warning() -> None:
    thin = {"warnings": ["日线评分口径：盈亏比仅 0.06，风险收益不划算"]}
    fine = {
        "warnings": ["日线评分口径：止损距离仅 5.04%，不足一个日波幅，按最小有效止损重算盈亏比"]
    }
    observation = {"warnings": ["日线评分口径：盈亏比仅 1.00，未达到盘中执行门槛，仅供观察"]}

    assert _thin_reward_risk(thin)
    assert not _thin_reward_risk(fine)
    assert not _thin_reward_risk(observation)
    assert not _thin_reward_risk({})


def test_preopen_observation_floor_stays_below_execution_rule() -> None:
    from ashare_decision_support.jobs.morning_select import REWARD_RISK_FLOOR

    assert REWARD_RISK_FLOOR == 0.8


def test_st_names_get_an_explicit_warning_on_the_row() -> None:
    rows = [
        {"symbol": "300087.SZ", "name": "ST荃银", "warnings": []},
        {"symbol": "600001.SH", "name": "退市海润"},
        {"symbol": "688183.SH", "name": "生益电子", "warnings": ["x"]},
    ]

    _flag_st(rows)

    assert any("ST/退市风险标的" in item for item in rows[0]["warnings"])
    assert any("ST/退市风险标的" in item for item in rows[1]["warnings"])
    assert rows[2]["warnings"] == ["x"]


# 简报：轮动观察句只印一遍


def test_rotation_observations_are_not_repeated_in_the_data_boundary() -> None:
    observation = "反复进入领涨前 5 的方向：元件"
    turnover_line = "占比与价格同时回落（没人看）：生物制品"
    other_note = "同花顺行业指数 90 个板块，最新交易日 20260911"

    markdown = render_sector_brief(
        date(2026, 9, 14),
        [],
        [],
        SimpleNamespace(direction="偏空", score=24.3, breadth=0.12, reasons=[]),
        0.27,
        data_notes=[observation, turnover_line, other_note],
        rotation_price={
            "sectors": 90,
            "trade_date": "20260911",
            "switch_label": "无明显轮动方向",
            "observations": [observation],
        },
        turnover={"sectors": 90, "trade_date": "20260911", "observations": [turnover_line]},
    )

    assert markdown.count(observation) == 1
    assert markdown.count(turnover_line) == 1
    assert markdown.count(other_note) == 1


# 辩论层：不念字段名


def test_debate_prompts_forbid_field_names_and_rank_keyword_risk_last() -> None:
    for role in (ROLE_BEAR, ROLE_JUDGE):
        assert "字段名" in PROMPTS[role]
        assert "news_risk_hits" in PROMPTS[role]
    assert "新闻风险词" in PROMPTS[ROLE_BEAR]
    assert "最多占一条" in PROMPTS[ROLE_BEAR]
