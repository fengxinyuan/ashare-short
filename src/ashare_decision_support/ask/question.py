"""把一句话拆成「问的是哪几只、问的是哪件事」。确定性，无模型。

`ask` 原来对问题文本**一个字都不看**——装配只按时刻决定。于是问「丸美生物低开
了怎么办」和问「今天该看什么」拿到的是同一份上下文：全部持仓、
整份台账统计一起倒出来，模型得自己在里面找那一只票。答案跟着散，是结构决定的，
不是措辞不好。

这一层只回答两个问题，答完交给 `plan.py`：

- **哪几只。** 代码直接认；名字只认系统已经知道的那些（持仓、最近一次算过
  的结果）。认不出来的名字**列进 `unresolved` 明说**，不猜——猜错一个字，后面
  整条链路会拿另一只票的数据来回答你的问题，而且看不出错在哪。
- **哪件事。** 买点、卖点、低开、风险、大盘、复盘、选股。一句话可以同时命中几个：
  「低开了该不该止损」既是低开也是卖点，两块都得装。

**意图不产生任何数字**，它只决定装什么、按什么顺序问。这正是它敢用关键词表实现的
理由：认错意图的代价是多跑一步或少跑一步，不是给错价。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ashare_decision_support.symbols import normalize_symbol

INTENT_ENTRY = "entry"
INTENT_EXIT = "exit"
INTENT_GAP = "gap"
INTENT_RISK = "risk"
INTENT_MARKET = "market"
INTENT_REVIEW = "review"
INTENT_SCREEN = "screen"

INTENT_LABELS = {
    INTENT_ENTRY: "买点",
    INTENT_EXIT: "卖点",
    INTENT_GAP: "跳空/低开",
    INTENT_RISK: "风险",
    INTENT_MARKET: "大盘与板块",
    INTENT_REVIEW: "复盘与台账",
    INTENT_SCREEN: "选股",
}

# 关键词表。命中即算，不做互斥——一句话本来就可以同时问几件事。
# 全部按 `str.upper()` 之后的文本匹配，所以 ascii 关键词写成大写；中文不受影响。
INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    INTENT_ENTRY: (
        "买点",
        "低吸",
        "抄底",
        "进场",
        "建仓",
        "加仓",
        "补仓",
        "接回",
        "能买",
        "可以买",
        "要不要买",
        "该不该买",
        "买入",
        "挂单",
        "上车",
        "入手",
    ),
    INTENT_EXIT: (
        "卖点",
        "止盈",
        "止损",
        "减仓",
        "清仓",
        "出掉",
        "该不该卖",
        "要不要卖",
        "卖出",
        "持仓",
        "浮盈",
        "浮亏",
        "套牢",
        "拿不拿",
        "走不走",
    ),
    INTENT_GAP: ("低开", "高开", "跳空", "平开", "竞价", "低走", "低位开盘"),
    INTENT_RISK: (
        "风险",
        "踩雷",
        "爆雷",
        "公告",
        "退市",
        "立案",
        "问询",
        "减持",
        "解禁",
        "商誉",
        "停牌",
        "ST",
        "利空",
        "黑天鹅",
    ),
    INTENT_MARKET: ("大盘", "指数", "板块", "主线", "情绪", "仓位", "环境", "题材", "风格", "赛道"),
    INTENT_REVIEW: (
        "复盘",
        "台账",
        "兑现",
        "胜率",
        "统计",
        "回顾",
        "做得怎么样",
        "对不对",
        "准不准",
    ),
    INTENT_SCREEN: (
        "看什么",
        "关注什么",
        "买什么",
        "选什么",
        "有什么机会",
        "有没有机会",
        "候选",
        "选股",
        "值得",
        "推荐",
        "哪些股票",
        "什么股票",
        "入手的股票",
    ),
}

# 交易约束。它们**不进任何计算**，只影响报告末尾那段纪律怎么写——写进去是因为
# 「保守型、不过夜」这类前提由你在问题里给出，系统没有别的地方能知道。
CONSTRAINT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "保守": ("保守", "稳健", "怕亏", "小仓"),
    "激进": ("激进", "搏一把", "重仓"),
    "不过夜": ("不过夜", "当天走", "日内", "T+0", "不隔夜"),
    "次日兑现": ("T+1", "次日", "明天卖", "隔夜"),
}

# `600519` / `600519.SH` / `SH600519`。前后的数字断言挡掉日期与金额里的六位串：
# 20260802 不是标的，13506.00 里的 13506 也不是。
_CODE_PATTERN = re.compile(r"(?<!\d)((?:SH|SZ|BJ)?\d{6}(?:\.(?:SH|SZ|BJ))?)(?!\d)", re.IGNORECASE)

# 证券简称的常见词尾。用它只为一件事：问题里出现了一个**看起来像股票名**但系统
# 不认识的词时，能指名道姓地说「这个我不认识」，而不是默默按空标的往下跑。

# 不收「股」这个字：`[一-鿿]{1,4}股` 会吃掉「这只股」「减持股」「白酒股」这类
# 说法，把一句正常的问话报成一串不认识的简称——一条帮不上忙的提示比没有更烦。
_NAME_SUFFIXES = (
    "生物",
    "科技",
    "医药",
    "股份",
    "集团",
    "电子",
    "能源",
    "材料",
    "智能",
    "汽车",
    "银行",
    "证券",
    "传媒",
    "食品",
    "化工",
    "机械",
    "重工",
    "电力",
    "环保",
    "光电",
    "通信",
    "软件",
    "数据",
    "网络",
    "制药",
)
_NAME_PATTERN = re.compile(r"[一-鿿]{1,4}(?:" + "|".join(_NAME_SUFFIXES) + r")")

# 行业词长得和简称一样，但它们不是标的。列在这里而不是靠长度过滤，是因为
# 「新能源汽车」五个字、「丸美生物」四个字，长度分不开它们。
_NOT_A_NAME = {
    "新能源汽车",
    "新能源",
    "半导体",
    "人工智能",
    "军工电子",
    "生物医药",
    "医药生物",
    "电力设备",
    "有色金属",
    "基础化工",
    "国防军工",
    "商业贸易",
    "机械设备",
    "汽车",
    "证券",
    "银行",
    "传媒",
    "科技股",
    "医药股",
    "白酒股",
    "题材股",
    "个股",
    "这只股",
}


@dataclass
class QuestionSpec:
    """一句话解析出来的东西。全部字段都可能为空——空也是结论。"""

    raw: str = ""
    symbols: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def symbol_specific(self) -> bool:
        """问的是不是某几只票。决定后面按标的分节还是按板块分节。"""
        if INTENT_SCREEN in self.intents and not self.symbols:
            return False
        return bool(self.symbols) or any(
            intent in self.intents for intent in (INTENT_ENTRY, INTENT_EXIT, INTENT_GAP)
        )

    def describe(self) -> str:
        parts = []
        if self.symbols:
            parts.append("标的 " + "、".join(self.symbols))
        if self.intents:
            parts.append("关注 " + "、".join(INTENT_LABELS.get(i, i) for i in self.intents))
        if self.constraints:
            parts.append("约束 " + "、".join(self.constraints))
        return "　".join(parts) or "无特定标的与意图"

    def to_dict(self) -> dict:
        return {
            "问题": self.raw,
            "标的": self.symbols,
            "意图": [INTENT_LABELS.get(i, i) for i in self.intents],
            "未能解析的名字": self.unresolved,
            "交易约束": self.constraints,
            "解析说明": self.notes,
        }


def parse_question(
    text: str | None,
    known_names: dict[str, str] | None = None,
    fallback_intents: list[str] | None = None,
) -> QuestionSpec:
    """解析一句话。`known_names` 是 简称 → 代码 的映射，通常来自持仓表。

    没提问、或者问题里什么都没认出来时，`fallback_intents` 顶上——那是调用方
    按时刻给的默认（盘前问「今天怎么样」问的是大盘与选股，盘中问的是买卖点）。
    """
    spec = QuestionSpec(raw=(text or "").strip())
    if not spec.raw:
        spec.intents = list(fallback_intents or [])
        return spec

    haystack = spec.raw.upper()
    spec.symbols = _extract_symbols(spec.raw, known_names or {})
    spec.intents = [
        intent
        for intent, words in INTENT_KEYWORDS.items()
        if any(word.upper() in haystack for word in words)
    ]
    spec.constraints = [
        label
        for label, words in CONSTRAINT_KEYWORDS.items()
        if any(word.upper() in haystack for word in words)
    ]

    if not spec.intents:
        spec.intents = list(fallback_intents or [])
        if spec.intents:
            spec.notes.append("问题里没有可识别的意图关键词，按当前时段的默认关注点装配")

    spec.unresolved = _unresolved_names(spec.raw, known_names or {})
    if spec.unresolved:
        spec.notes.append(
            "问题里这些词看着像证券简称，但本地名称缓存里没有，因此**没有**为它们取数："
            + "、".join(spec.unresolved)
            + "。请在问题里写股票代码"
        )
    if not spec.symbols and spec.symbol_specific:
        spec.notes.append("问题指向具体标的，但没解析出任何代码；下面按持仓表里的标的回答")
    return spec


def _extract_symbols(text: str, known_names: dict[str, str]) -> list[str]:
    found: list[str] = []
    for match in _CODE_PATTERN.finditer(text):
        symbol = normalize_symbol(match.group(1))
        if symbol:
            found.append(symbol)

    # 长名字优先匹配：「丸美生物」和「丸美」同时在册时，短的那个会先命中并
    # 吃掉长的那一个。
    for name in sorted(known_names, key=len, reverse=True):
        if name and name in text:
            found.append(normalize_symbol(known_names[name]))
    return list(dict.fromkeys(symbol for symbol in found if symbol))


def _unresolved_names(text: str, known_names: dict[str, str]) -> list[str]:
    known = set(known_names)
    names: list[str] = []
    for match in _NAME_PATTERN.finditer(text):
        name = match.group()
        if name in known or name in _NOT_A_NAME:
            continue
        # 已经被代码解析出来的那一只，名字再被认出来也不算「未解析」。
        if any(name in entry for entry in known):
            continue
        names.append(name)
    return list(dict.fromkeys(names))[:5]
