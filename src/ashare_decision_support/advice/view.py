"""买卖点的呈现原语：三条不经 LLM 的输出路径共用的那几段格式化。

`ask/context.py`、`chat/session.py`、`monitor/triggers.py` 各自要把同一份买卖点
摊成人看的文本。三处原来各写了一份优先级排序、一份换手率/成交额拼接、一份买入
区间格式化——**逐字相同**。

各写一份的代价不是重复本身，是漂移：改了对话里的排序而没改问答里的，同一只票
在 `ask` 里排第一、在 `chat` 里排第四，而两边读的是同一个 JSON。这类分叉不会
报错，只会让人以为系统换了看法。

这一层只做格式化，不做判断——所有数字都由 `engine.py` 算完落在 payload 里。
"""

from __future__ import annotations

import json
from typing import Any

# 动作的展示优先级：离动作越近排越前。同一动作内按评分降序。
# 数字本身没有含义，只用来定序；`engine.py` 产出的 action 之外的值落到 2
# （等同「等触发」），因为未知动作既不该顶上来也不该沉到底。
_ACTION_PRIORITY = {
    "breakout_follow": 0,
    "buy_pullback": 1,
    "wait_trigger": 2,
    "wait_reclaim": 3,
    "reduce_risk": 4,
}
_DEFAULT_PRIORITY = 2


def short(value: Any, limit: int = 160) -> str:
    """把任意值压成一行。超长截断——这是没配 key 时唯一的输出路径，不能刷屏。"""
    text = (
        json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, (dict, list))
        else str(value)
    )
    return text if len(text) <= limit else text[: limit - 1] + "…"


def advice_sort_key(item: dict[str, Any]) -> tuple[int, float]:
    """排序键：先按动作离操作的远近，再按评分降序。"""
    shortline = item.get("shortline") or {}
    score = shortline.get("score")
    if not isinstance(score, (int, float)):
        raw = item.get("score")
        score = raw if isinstance(raw, (int, float)) else 0.0
    action = str(shortline.get("action") or "")
    return (_ACTION_PRIORITY.get(action, _DEFAULT_PRIORITY), -float(score))


def sort_advice_items(items: Any) -> list[dict[str, Any]]:
    """按 `advice_sort_key` 排序，顺手滤掉非 dict 的脏条目。"""
    return sorted(
        [item for item in (items or []) if isinstance(item, dict)],
        key=advice_sort_key,
    )


def is_advice_like(block: Any) -> bool:
    """这一块是不是买卖点 payload。

    认的是**字段形状**而不是 key 名：同一份数据在 `advice` / `实时买卖点` /
    `最近一次买卖点` 三个名字下出现过，按名字判会漏掉其中两个。
    """
    if not isinstance(block, dict):
        return False
    symbols = block.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        return False
    return any(
        isinstance(item, dict)
        and (
            item.get("entry_low") is not None or item.get("shortline") or item.get("trade_context")
        )
        for item in symbols
    )


def entry_band(item: dict[str, Any], *, placeholder: str = "—") -> str:
    """买入区间。只有下沿时就报下沿，两头都没有时报占位符。"""
    low, high = item.get("entry_low"), item.get("entry_high")
    if low is not None and high is not None:
        return f"{low}-{high}"
    if low is not None:
        return str(low)
    return placeholder


def trade_context_parts(turnover_ratio: Any, session_amount: Any) -> list[str]:
    """换手率与成交额。

    取标量而不是取整个 `trade_context`，是因为调用方一边拿到的是 dict、一边拿到
    的是 `TradeContext` 对象；在这里做类型分支等于把两种形状的知识搬进呈现层。
    """
    parts: list[str] = []
    if isinstance(turnover_ratio, (int, float)):
        parts.append(f"换手率 {turnover_ratio:.2f}%")
    if isinstance(session_amount, (int, float)):
        parts.append(f"成交额 {session_amount / 1e8:.2f} 亿")
    return parts


def trade_context_parts_from_dict(item: dict[str, Any]) -> list[str]:
    """`trade_context_parts` 的 dict 入口，供读落盘 JSON 的那两条路径用。"""
    trade = item.get("trade_context") or {}
    if not isinstance(trade, dict):
        return []
    return trade_context_parts(trade.get("turnover_ratio"), trade.get("session_amount"))
