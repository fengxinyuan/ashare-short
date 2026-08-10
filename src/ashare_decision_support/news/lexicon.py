"""Chinese A-share sentiment lexicon.

Hand-curated, not learned. It is kept in its own module because it is the part
most likely to need tuning: adding a word here is a one-line change that needs
no code review of the scoring logic.

Weights are relative, not calibrated probabilities. The scale is roughly:
  3.0  regulatory / existential (退市, 立案调查)
  2.0  hard financial fact (预增, 预亏, 中标)
  1.0  soft narrative (关注, 概念, 传闻)

本模块装的是**全部**词表，不只是带权的那张。下半部分是不带权、按类归并的事件
与风险词表——它们服务于打分与事件标签，但归口在这里的理由和上面那句一样：词
表只有一个家，加词才是一处改动。
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

# --- 事件与风险词表：不带权，按类归并 ---------------------------------------
#
# 上面那张表回答「这条消息偏多还是偏空、有多强」，下面这两张回答「它说的是**哪
# 一类事**」。两件事的消费者不同：情绪分进舆情读数，事件/风险类别进候选打分。
#
# **为什么搬到这里。** 这两张表此前各有两份：`news/events.py` 一份用来产出可解
# 释的事件标签，`scoring/context.py` 一份用来给候选打分。两份都叫 `RISK_TERMS`，
# 交集只有 4 个词——「退市 / 风险警示 / 财务造假 / 行政处罚 / 停牌」只在舆情那
# 份里。后果不是风格不一致，是**一条「公司被实施退市风险警示」在舆情层记成风险
# 事件、在打分层拿 0 分**，而 A 股最不该漏的就是这一类。搬进来之后加一个词是一
# 处改动，两层同时生效——这正是本模块开头那句话对情绪词表的承诺。


# 供给侧词表单列，因为它同时是**两种证据**：对个股是经营中断（停产），对板块是
# 供给收缩（利好）。`scoring/context.py` 靠这张表把「停产」从板块的风险里减掉，
# 所以它必须是同一份，不能各留一份副本。
SUPPLY_TERMS: tuple[str, ...] = (
    "供给收缩", "产能受限", "停产", "停机", "检修", "减产",
    "库存下降", "库存拐点", "供不应求", "产能置换",
)

# 事件类别 → 关键词。类别名会直接出现在简报里（「事件催化证据：订单」），
# 所以它是给人看的分类，不是内部枚举。
EVENT_TERMS: dict[str, tuple[str, ...]] = {
    "政策": (
        "政策支持", "政策利好", "专项规划", "专项债", "补贴",
        "试点", "产业规划", "设备更新", "以旧换新", "国产替代",
    ),
    "价格": (
        "涨价", "提价", "价格上调", "报价上调", "降价",
        "价格回落", "价格上涨", "纸价", "浆价",
    ),
    "供给": SUPPLY_TERMS,
    "订单": ("订单", "中标", "签约", "合同", "客户导入", "批量供货"),
    "业绩": (
        "业绩预增", "业绩增长", "净利润增长", "扭亏", "超预期",
        "预告上修", "预亏", "净利润下降", "低于预期",
    ),
    "资本运作": ("回购", "增持", "并购", "重组", "资产注入", "定增", "股权激励"),
}

# 风险类别 → 关键词。**按类归并不是为了排版，它是计数单位。** 「关于公司股票被
# 实施退市风险警示的公告」这一句里同时含有「退市」和「风险警示」，按词数扣分等
# 于同一件事罚两次；按类算它是一件事。打分层数类别，舆情层仍然报命中的词——
# 「哪一类」和「凭哪个词」是两个问题，各答各的。
#
# 「问询」而不是「问询函」：前者是后者的子串，两个都留会让「收到问询函」命中两
# 次；留短的那个还能接住「监管问询」。
RISK_TERMS: dict[str, tuple[str, ...]] = {
    "退市风险": ("退市", "风险警示", "财务造假"),
    "监管": ("立案", "行政处罚", "问询"),
    "股东减持": ("减持",),
    "诉讼": ("诉讼",),
    "业绩": ("预亏", "首亏", "增亏", "商誉减值"),
    "经营中断": ("停产", "停牌"),
}
