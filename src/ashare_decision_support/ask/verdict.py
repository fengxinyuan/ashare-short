"""规则算出来的**决策卡**：一只票一张，只有四种内容。

你说输出「没有重点、比较混乱、没有实质性的建议组织」。根源不在措辞，在结构：
一份把所有块平铺出来的上下文，读的人（和模型）没办法知道哪一条改变行动、哪一条
只是背景。加长度不会解决它，只会让它更长。

所以先在**规则层**把每只票压成一张卡：

| 卡上的东西 | 是什么 | 谁产生 |
|---|---|---|
| 结论标签 | 可执行 / 需确认 / 不成立 | 规则 |
| 触发条件 | 还差什么才算成立 | 规则 |
| 失效条件 | 什么发生就说明这次错了 | 规则 |
| 阻断项 | 此刻做不成、或者不该做的硬理由 | 规则 |

**三个标签的判据全部写死在这里，不经过模型。** 模型只解释这张卡；解释被数字闸门
丢掉的时候，卡还在。这就是「有重点」和「可丢弃」能同时成立的原因——原来那条路
是一次调用产出整个答案，一个编造的数字下去，三只票十五个小节一起消失。

排序同理，`rank` 是确定性函数。让模型排序等于让它在没有拟合模型的前提下给相对
强弱，那正是这套系统一直拒绝提供的东西（`docs/SOLUTION.md` §七）。模型能做的是
解释已经排好的顺序，以及指出第一名和最后一名的差别落在哪个读数上。

**卡本身要进 facts 再交给模型**（`plan.py` 就是这么装的）。理由和
`jobs/advise.py:_facts` 把候选集放进去是同一条：数字闸门比对的就是那份 facts，
卡上的止损距离若不在里面，模型引用它反而会被判成编造。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.numeric import finite

SIDE_BUY = "buy"
SIDE_SELL = "sell"

LABEL_ACTIONABLE = "可执行"
LABEL_CONFIRM = "需确认"
LABEL_BLOCKED = "不成立"

# 标签的含义按方向不同：
#   买入侧  可执行＝价位已进买入带且没有阻断；需确认＝还差一个条件；不成立＝有硬阻断
#   卖出侧  可执行＝止损或目标条件**已经触发**；需确认＝还没到；不成立＝此刻卖不出
LABEL_ORDER = {LABEL_ACTIONABLE: 0, LABEL_CONFIRM: 1, LABEL_BLOCKED: 2}

# 盈亏比门槛。和 `advice/engine.py` 里那条「不满足超短线最低要求」的警告同一个数，
# 写成常量是为了让两处一起改——它是人工设定、未经回测的，见 `docs/SOLUTION.md` §七。
MIN_RRR = 1.5


@dataclass
class Decision:
    """一只票此刻的决策卡。字段缺失一律 None，不填 0——「没算出来」和「算出来是零」
    在事后统计里必须能分开。"""

    symbol: str
    name: str | None = None
    side: str = SIDE_BUY
    label: str = LABEL_BLOCKED
    score: float | None = None
    last_price: float | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    stop_distance_pct: float | None = None
    targets: list[float] = field(default_factory=list)
    rrr: float | None = None
    triggers: list[str] = field(default_factory=list)
    invalidations: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    readings: list[str] = field(default_factory=list)
    holding: dict[str, Any] | None = None

    @property
    def actionable(self) -> bool:
        return self.label == LABEL_ACTIONABLE

    def headline(self) -> str:
        """一行结论。报告的每一节都从它开始——先说结论，再说依据。"""
        who = f"{self.symbol}{' ' + self.name if self.name else ''}"
        if self.side == SIDE_SELL:
            verb = {
                LABEL_ACTIONABLE: "卖出条件已触发",
                LABEL_CONFIRM: "持有中，卖点条件未触发",
                LABEL_BLOCKED: "此刻卖不出或无法判断",
            }[self.label]
        else:
            verb = {
                LABEL_ACTIONABLE: "买入条件已满足",
                LABEL_CONFIRM: "买入条件未满足，等触发",
                LABEL_BLOCKED: "本轮不成立",
            }[self.label]
        return f"{who}　**{self.label}**　{verb}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "名称": self.name,
            "方向": "卖出侧" if self.side == SIDE_SELL else "买入侧",
            "结论": self.label,
            "评分": self.score,
            "现价": self.last_price,
            "买入带": [self.entry_low, self.entry_high]
            if self.entry_low is not None
            else None,
            "止损": self.stop,
            "止损距离": self.stop_distance_pct,
            "目标": self.targets,
            "盈亏比": self.rrr,
            "触发条件": self.triggers,
            "失效条件": self.invalidations,
            "阻断项": self.blockers,
            "关键读数": self.readings,
            "持仓": self.holding,
        }


def assess(payload: dict[str, Any], holding: dict[str, Any] | None = None) -> Decision:
    """把 `jobs/advise.py:_facts` 里的一条 symbol 记录压成一张卡。

    `holding` 是持仓表里那条记录（有成本价才算）。有它就走卖出侧——同一只票
    没持仓时该盯买点、已持仓时该盯卖点，两套判断的输入和紧急程度本来就不同。
    """
    payload = payload or {}
    decision = Decision(
        symbol=str(payload.get("symbol") or ""),
        name=payload.get("name"),
        score=finite(payload.get("score")),
        last_price=finite(payload.get("last_price")),
        entry_low=finite(payload.get("entry_low")),
        entry_high=finite(payload.get("entry_high")),
        stop=finite(payload.get("stop")),
        targets=[value for value in (finite(t) for t in payload.get("targets") or []) if value],
        rrr=finite(payload.get("rrr")),
    )
    intraday = payload.get("intraday") or {}
    book = payload.get("order_book") or {}
    adjudication = payload.get("adjudication") or {}

    decision.blockers = _blockers(payload, adjudication)
    decision.readings = _readings(payload, intraday, book)
    if decision.stop and decision.last_price:
        decision.stop_distance_pct = round(
            (decision.last_price - decision.stop) / decision.last_price, 4
        )

    if holding and finite(holding.get("cost_price")):
        _sell_side(decision, holding, book)
    else:
        _buy_side(decision, book)
    return decision


def rank(decisions: list[Decision]) -> list[Decision]:
    """确定性排序：先按结论分档，再按盈亏比、评分。

    同分时按代码排——不是因为代码有意义，是因为**顺序不能随字典的插入顺序漂移**。
    今天第二、明天第三而底层数字没变，会让人以为发生了什么。
    """
    return sorted(
        decisions,
        key=lambda item: (
            LABEL_ORDER.get(item.label, 9),
            -(item.rrr or 0.0),
            -(item.score or 0.0),
            item.symbol,
        ),
    )


# 装配好的上下文里，买卖点可能来自两个地方。顺序即优先级：现算的那份永远压过
# 落盘的旧的那份，而用了哪一份必须报出来——旧数据当实时用是这套系统的头号事故。
ADVICE_BLOCKS = ("实时买卖点", "最近一次买卖点", "个股买卖点")


def from_context(
    blocks: dict[str, Any], focus: list[str] | None = None
) -> tuple[list[Decision], str | None]:
    """从装配好的 blocks 里生出全部决策卡，附带「数据来自哪一块」。

    `focus` 非空时只留这几只——问三只票就只出三张卡，不把持仓也一起铺开。
    """
    payload, source = _advice_payload(blocks)
    if payload is None:
        return [], None

    holdings = _holdings(blocks)
    wanted = set(focus or [])
    decisions = [
        assess(item, holdings.get(str(item.get("symbol") or "")))
        for item in payload.get("symbols") or []
        if isinstance(item, dict) and (not wanted or str(item.get("symbol") or "") in wanted)
    ]
    return decisions, source


def _advice_payload(blocks: dict[str, Any]) -> tuple[dict | None, str | None]:
    for name in ADVICE_BLOCKS:
        block = blocks.get(name)
        if isinstance(block, dict) and block.get("symbols"):
            return block, name
    return None, None


def _holdings(blocks: dict[str, Any]) -> dict[str, dict]:
    book = blocks.get("持仓")
    if not isinstance(book, dict):
        return {}
    return {
        str(entry.get("symbol") or ""): _normalise_holding(entry)
        for entry in book.get("明细") or []
        if isinstance(entry, dict)
    }


# `ask/context.py` 把持仓摊平成中文字段名（那是给人读的渲染需要），`state.Position`
# 用的是英文字段名。两种都收，在这里归一——判断逻辑只认一套键，否则「成本价」
# 取不到时会**静默**走成买入侧，而持仓最需要的恰恰是卖点那一套。
_HOLDING_ALIASES = {
    "成本价": "cost_price",
    "股数": "shares",
    "建仓日": "opened_on",
    "持有天数": "held_days",
    "计划止损": "plan_stop",
    "计划目标": "plan_target",
}


def _normalise_holding(entry: dict[str, Any]) -> dict[str, Any]:
    merged = dict(entry)
    for chinese, english in _HOLDING_ALIASES.items():
        if merged.get(english) is None and entry.get(chinese) is not None:
            merged[english] = entry[chinese]
    return merged


def summarise(decisions: list[Decision]) -> str:
    if not decisions:
        return "没有任何标的可评估"
    counts = dict.fromkeys(LABEL_ORDER, 0)
    for item in decisions:
        counts[item.label] = counts.get(item.label, 0) + 1
    body = " · ".join(f"{label} {counts.get(label, 0)}" for label in LABEL_ORDER)
    return f"共 {len(decisions)} 只：{body}"


# ---- 买入侧 ---------------------------------------------------------------


def _buy_side(decision: Decision, book: dict[str, Any]) -> None:
    last, low, high = decision.last_price, decision.entry_low, decision.entry_high

    if decision.stop is not None:
        decision.invalidations.append(
            f"跌破止损 {decision.stop}：买入依据消失，按纪律离场，不临场改止损"
        )
    if low is not None and last is not None and last < low:
        # 低开/急跌落到买入带下方。这**不是**更便宜的买点：回踩带被跌穿，
        # 说明当初画这条带的依据已经不在了。`advice/candidates.py` 里对同一
        # 情形的措辞是「这不是低吸位而是失守位」，两处必须说同一件事。
        decision.invalidations.append(
            f"现价 {last} 已在买入带下沿 {low} 之下：回踩带被跌穿，属失守而非折价"
        )

    if decision.blockers:
        decision.label = LABEL_BLOCKED
        return

    if low is None or high is None or decision.stop is None:
        decision.blockers.append("没有可用的买入位或止损位，给不出可执行的动作")
        decision.label = LABEL_BLOCKED
        return

    ready = True
    if last is not None and last > high:
        decision.triggers.append(f"回落进买入带 {low}–{high}（现价 {last} 在上沿之上）")
        ready = False
    elif last is not None and last < low:
        decision.triggers.append(f"重新站回 {low} 之上并出现止跌形态，才谈得上接")
        ready = False
    else:
        decision.triggers.append(f"现价已在买入带 {low}–{high} 内")

    if decision.rrr is None:
        decision.triggers.append("上方无参照位，盈亏比算不出来——没有兑现点的仓位不成立")
        ready = False
    elif decision.rrr < MIN_RRR:
        decision.triggers.append(f"盈亏比 {decision.rrr} 低于 {MIN_RRR}，不满足最低要求")
        ready = False

    if not _tradable_now(book):
        # 「到位了」和「动得了」是两个问题。封涨停时区间是真的进了，但挂单只能
        # 排队等开板——所以它降一档而不是照发，也不算硬阻断。
        decision.triggers.append("封涨停，此刻买不进，挂单只能排队等开板")
        ready = False

    decision.label = LABEL_ACTIONABLE if ready else LABEL_CONFIRM


# ---- 卖出侧 ---------------------------------------------------------------


def _sell_side(decision: Decision, holding: dict[str, Any], book: dict[str, Any]) -> None:
    decision.side = SIDE_SELL
    cost = finite(holding.get("cost_price"))
    last = decision.last_price
    # 手工填的止损/目标优先于系统算的：填进去的是决定，算出来的是建议。
    # 同 `monitor/holdings.py:effective_stop`。
    stop = finite(holding.get("plan_stop")) or decision.stop
    plan_target = finite(holding.get("plan_target"))
    targets = [plan_target] if plan_target else decision.targets

    unrealised = round(last / cost - 1.0, 4) if (last and cost) else None
    decision.holding = {
        "成本价": cost,
        "股数": holding.get("shares"),
        "建仓日": holding.get("opened_on"),
        "持有天数": holding.get("held_days"),
        "浮动盈亏": unrealised,
        "生效止损": stop,
        "生效目标": targets,
        "止损来源": "手填" if finite(holding.get("plan_stop")) else "系统算",
    }
    decision.stop = stop
    decision.targets = targets
    if stop and last:
        decision.stop_distance_pct = round((last - stop) / last, 4)

    if stop:
        decision.invalidations.append(f"跌破 {stop}：止损条件成立，按纪律走")
    if cost:
        decision.invalidations.append(f"跌破成本价 {cost}：这笔由盈转亏")

    if decision.blockers:
        decision.label = LABEL_BLOCKED
        return
    if last is None:
        decision.blockers.append("拿不到现价，无法判断持仓此刻处在哪一档")
        decision.label = LABEL_BLOCKED
        return

    triggered = False
    if stop and last <= stop:
        decision.triggers.append(f"现价 {last} 已跌破止损 {stop}")
        triggered = True
    if targets and last >= targets[0]:
        decision.triggers.append(f"现价 {last} 已触及目标 {targets[0]}")
        triggered = True
    if not triggered:
        parts = []
        if stop:
            parts.append(f"跌破 {stop} 止损")
        if targets:
            parts.append(f"触及 {targets[0]} 兑现")
        decision.triggers.append(
            "两个方向都还没到：" + "，或".join(parts) if parts else "无可判定的卖点位"
        )

    if triggered and _at_limit_down(book):
        # 该卖但卖不出，是一个必须说出口的状态——它不是「不用管」，是「今天做不到」。
        decision.blockers.append("封跌停：此刻卖不出，封死就只能等次日")
        decision.label = LABEL_BLOCKED
        return

    decision.label = LABEL_ACTIONABLE if triggered else LABEL_CONFIRM


# ---- 读数与阻断 -----------------------------------------------------------


def _blockers(payload: dict, adjudication: dict) -> list[str]:
    blockers: list[str] = []
    for warning in payload.get("warnings") or []:
        text = str(warning)
        # 风险筛除与模型弃权在 `advise` 里就是靠这两个前缀标出来的，这里沿用，
        # 不重新判定一遍——两处判据分开写，早晚会分叉。
        if text.startswith("⚠️"):
            blockers.append(text.lstrip("⚠️").strip())
        elif text.startswith("⚖️"):
            blockers.append(text.lstrip("⚖️").strip())

    if payload.get("session_date") and payload.get("is_current_session") is False:
        blockers.append(
            f"数据停在 {payload['session_date']}，不是当日盘中——价位按该日收盘结构给出，不能当实时用"
        )
    if adjudication.get("abstained") and not any("弃权" in item for item in blockers):
        blockers.append(f"模型裁决弃权：{adjudication.get('reason') or '未给理由'}")
    return list(dict.fromkeys(blockers))


def _readings(payload: dict, intraday: dict, book: dict) -> list[str]:
    """卡上引用的 L1 读数。只挑**会改变判断**的那几个，不是把 intraday 抄一遍。"""
    readings: list[str] = []
    if payload.get("trend_state"):
        readings.append(f"日线趋势 {payload['trend_state']}")
    if payload.get("wyckoff_bias"):
        readings.append(f"威科夫 {payload['wyckoff_bias']}")

    gap = finite(intraday.get("price_vs_open_pct"))
    open_price = finite(intraday.get("open"))
    open_gap = finite(intraday.get("open_gap_pct"))
    day_change = finite(intraday.get("day_change_pct"))
    if day_change is not None:
        # 先报「今天涨了多少」。下面两行拆的是这个数的构成（跳空多少 + 开盘后走了
        # 多少），但读的人最先要的是合计那一个——它是行情软件上的那个涨幅。
        readings.append(f"今日涨幅 {day_change:+.2%}")
    if open_price is not None:
        # 跳空和开盘后的走势是两件事，分开报。「低开 3% 然后一路涨回来」和
        # 「平开一路阴跌 3%」在只报一个数时长得一样。
        line = f"今开 {open_price}"
        if open_gap is not None:
            line += f"（较昨收 {open_gap:+.2%}{'，低开' if open_gap < 0 else ''}）"
        readings.append(line)
    if gap is not None:
        readings.append(f"现价较开盘 {gap:+.2%}")
    vwap_gap = finite(intraday.get("price_vs_vwap_pct"))
    if vwap_gap is not None:
        readings.append(f"较分时均价 {vwap_gap:+.2%}")
    ratio = finite(intraday.get("volume_ratio"))
    if ratio is not None:
        readings.append(f"量比 {ratio}")
    position = finite(intraday.get("range_position"))
    if position is not None:
        readings.append(f"日内位置 {position:.0%}")

    readings.extend(_book_lines(book))
    news = payload.get("news") or {}
    if isinstance(news, dict) and news.get("score") is not None:
        readings.append(f"个股舆情 {news.get('score')}（{news.get('doc_count', 0)} 条）")
    return readings


def _book_lines(book: dict) -> list[str]:
    """盘口只取三样：封板状态、墙、委比。**挂单可撤，只作旁证。**"""
    if not book:
        return []
    lines: list[str] = []
    if book.get("at_limit_up") and book.get("limit_up"):
        lines.append(f"封涨停 {book['limit_up']}：此刻买不进")
    elif book.get("at_limit_down") and book.get("limit_down"):
        lines.append(f"封跌停 {book['limit_down']}：此刻卖不出")
    if book.get("bid_wall"):
        lines.append(f"{book['bid_wall']}（挂单可撤，只作旁证）")
    if book.get("ask_wall"):
        lines.append(f"{book['ask_wall']}（挂单可撤，只作旁证）")
    imbalance = finite(book.get("imbalance"))
    if not book.get("is_degenerate") and imbalance is not None:
        lines.append(f"五档委比 {imbalance:+.2f}")
    return lines


def _tradable_now(book: dict) -> bool:
    # 拿不到涨跌停价时什么都不做：**未知不算封板**，否则一个缺失字段会悄悄
    # 压掉本该给出的结论。同 `monitor/triggers.py`。
    return not bool(book.get("at_limit_up"))


def _at_limit_down(book: dict) -> bool:
    return bool(book.get("at_limit_down"))
