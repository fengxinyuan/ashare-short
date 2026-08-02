"""Chinese A-share sentiment lexicon.

Hand-curated, not learned. It is kept in its own module because it is the part
most likely to need tuning: adding a word here is a one-line change that needs
no code review of the scoring logic.

Weights are relative, not calibrated probabilities. The scale is roughly:
  3.0  regulatory / existential (退市, 立案调查)
  2.0  hard financial fact (预增, 预亏, 中标)
  1.0  soft narrative (关注, 概念, 传闻)
"""

from __future__ import annotations

# Positive drivers, weighted by how often they actually precede a move.
BULLISH_TERMS: dict[str, float] = {
    # Hard corporate facts
    "中标": 2.0, "签约": 1.5, "大额订单": 2.0, "订单": 1.2, "增持": 2.0, "回购": 1.8,
    "业绩预增": 2.5, "净利润增长": 2.0, "扭亏": 2.2, "超预期": 2.2, "创新高": 1.8,
    "量产": 1.8, "投产": 1.5, "扩产": 1.5, "满产": 1.8, "供不应求": 2.0,
    "涨价": 2.0, "提价": 2.0, "获批": 2.0, "中标金额": 2.0, "重组": 1.8, "资产注入": 2.0,
    "高送转": 1.5, "分红": 1.0, "股权激励": 1.5,
    # Policy / macro
    "政策支持": 2.0, "国家支持": 2.0, "专项债": 1.2, "补贴": 1.5, "减税": 1.5,
    "国产替代": 2.0, "自主可控": 1.8, "首台套": 1.5, "试点": 1.2, "规划": 1.0,
    "十五五": 1.2, "利好": 2.0,
    # Market behaviour
    "涨停": 2.0, "大涨": 1.8, "飙升": 1.8, "新高": 1.5, "突破": 1.5, "主力流入": 1.8,
    "北向资金流入": 1.8, "龙头": 1.2, "机构调研": 1.2, "上调评级": 2.0, "买入评级": 1.8,
    "增持评级": 1.5, "稀缺": 1.2, "景气": 1.5, "复苏": 1.5,
}

# Negative drivers. Regulatory items are weighted highest: they are the ones
# that cannot be traded out of.
BEARISH_TERMS: dict[str, float] = {
    # Regulatory / existential
    "立案调查": 3.0, "立案": 3.0, "退市": 3.0, "风险警示": 3.0, "财务造假": 3.0,
    "内幕交易": 3.0, "操纵市场": 3.0, "被处罚": 2.5, "行政处罚": 2.5, "问询函": 2.2,
    "关注函": 2.0, "警示函": 2.2, "监管": 1.2, "违规": 2.2, "失信": 2.2, "冻结": 2.2,
    "诉讼": 1.8, "仲裁": 1.5, "停牌": 2.0,
    # Hard financial facts
    "业绩预亏": 2.5, "预亏": 2.5, "亏损": 2.0, "净利润下降": 2.0, "商誉减值": 2.5,
    "计提": 1.8, "减值": 1.8, "低于预期": 2.2, "下滑": 1.5, "降价": 1.8,
    # Shareholder behaviour
    "减持": 2.5, "清仓式减持": 3.0, "质押": 1.8, "平仓": 2.2, "解禁": 2.0, "限售股解禁": 2.2,
    "辞职": 1.2, "离职": 1.2,
    # Market behaviour
    "跌停": 2.0, "大跌": 1.8, "暴跌": 2.2, "新低": 1.5, "破位": 1.8, "主力流出": 1.8,
    "北向资金流出": 1.8, "下调评级": 2.0, "卖出评级": 2.0, "减持评级": 1.5,
    "利空": 2.0, "承压": 1.5, "产能过剩": 2.0, "价格战": 2.0,
}

# Amplifiers multiply the weight of the term they qualify.
AMPLIFIERS: dict[str, float] = {
    "大幅": 1.5, "显著": 1.3, "急剧": 1.5, "创纪录": 1.6, "全面": 1.3,
    "首次": 1.3, "史上": 1.5, "巨额": 1.6, "重大": 1.4, "严重": 1.5,
}

# Negations flip the polarity of a matched term within `NEGATION_WINDOW` chars.
NEGATIONS: tuple[str, ...] = ("不", "未", "无", "没有", "否认", "辟谣", "取消", "终止", "暂缓")

# Characters. A negation only applies to a term that starts within this many
# characters after it, so "未中标" flips but "未来公司中标" does not.
NEGATION_WINDOW = 4

# Titles containing these are routine filings that carry no directional signal.
# Dropping them keeps the per-stock polarity from being dominated by boilerplate.
NOISE_TERMS: tuple[str, ...] = (
    "会议决议公告", "独立董事", "监事会", "章程", "会计师事务所", "募集资金存放",
    "持股5%以上股东", "投资者关系活动记录", "股东大会通知", "更正公告", "澄清公告",
)
