"""跑批链路的两处**可选数据源取数**：涨停池与逐笔资金。

`morning`（盘前）与 `post_close`（盘后）各要这两样东西，此前各写了一份——
两份 `_trade_flow` 34 行、两份 `_limit_pool` 15 行，**除了 note 的措辞之外逐字
相同**。

值得收口的不是行数，是这两个函数里的**失败处置**。它们是可选源，取数失败必须
是「这次没有这一项」而不是「这轮跑批没了」，于是每一份里都埋着同一串判断：

```text
开关关了      → 记一条 note，返回空，链路照跑
适配器是 None → 返回空（连默认适配器都造不出来，比如可选依赖没装）
没有 getter   → 记一条 note，返回空（适配器版本对不上）
取数抛异常    → 记一条 note，返回空
```

四条里漏掉任何一条，症状都不是报错，是**一次没有资金旁证的跑批**——而且盘前和
盘后会漏得不一样。这正是把它收成一份的理由。

**措辞留给调用方。** 盘前那条 note 要提 `--no-trade-flow`（用户是在命令行上关掉
它的），盘后那条要说「本次盘后卡」。共用的是取数与兜底的顺序，不是话术，同
`sectors/panel.py` 开头的那条纪律。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ashare_decision_support.adapters.lhb_seats import LhbSeatAdapter
from ashare_decision_support.adapters.limit_pool import LimitPoolAdapter
from ashare_decision_support.adapters.tencent_detail import default_trade_flow_adapter
from ashare_decision_support.fanout import gather


def fetch_limit_pool(
    adapter: Any | None,
    today: date,
    *,
    enabled: bool,
    notes: list[str],
    off_note: str,
) -> Any:
    """涨停池快照。关掉、取不到，都返回 None，链路继续。

    快照自己的 `notes` 与 `describe()` 一并倒进来：涨停池是情绪周期判定的唯一
    输入，「今天连板高度几何、炸板率多少」这几句话必须跟着数据一起进简报，
    否则读的人看到一个仓位上限却看不到它是从哪来的。
    """
    if not enabled:
        notes.append(off_note)
        return None
    source = adapter if adapter is not None else LimitPoolAdapter()
    try:
        snapshot = source.fetch_recent(today)
    except Exception as exc:
        notes.append(f"涨停池不可用（{type(exc).__name__}），本次无连板与炸板读数")
        return None
    notes.extend(snapshot.notes)
    if snapshot.ok:
        notes.extend(snapshot.describe())
    return snapshot


def fetch_trade_flow(
    adapter: Any | None,
    symbols: list[str],
    *,
    enabled: bool,
    notes: list[str],
    off_note: str,
    subject: str,
) -> dict[str, dict]:
    """候选/持仓的逐笔资金旁证，`{symbol: 摘要}`。取不到就是空字典。

    **数据是上一个交易日的整段成交**，盘前跑正好对得上；盘中跑拿到的是当日截至
    此刻的半段，那时「尾盘净额」这类读数根本没发生——所以摘要里带着交易日，
    由读的人判断时点对不对，这一层不替他判断。

    `subject` 只进「取不到」那条 note（「候选行无资金旁证」/「本次盘后卡无…」）。
    """
    if not enabled:
        notes.append(off_note)
        return {}
    if not symbols:
        return {}
    source = adapter if adapter is not None else default_trade_flow_adapter()
    if source is None:
        return {}
    getter = getattr(source, "get_trade_flow", None)
    if not callable(getter):
        notes.append("逐笔资金适配器缺少 get_trade_flow，已跳过")
        return {}
    try:
        flows, extra = getter(symbols)
    except Exception as exc:
        notes.append(f"逐笔资金不可用（{type(exc).__name__}），{subject}无资金旁证")
        return {}
    notes.extend(extra or [])
    # 适配器可能交出 dataclass，也可能已经是 dict。两种都收，其余形状**丢掉**
    # 而不是原样透传：这份摘要要整个序列化进简报与台账，放一个不认识的对象进去，
    # 出错的地方会在很远的 json.dumps 那里。
    mapped: dict[str, dict] = {}
    for symbol, flow in (flows or {}).items():
        if hasattr(flow, "to_dict"):
            mapped[str(symbol)] = flow.to_dict()
        elif isinstance(flow, dict):
            mapped[str(symbol)] = dict(flow)
    return mapped


def fetch_lhb_seats(
    adapter: Any | None,
    symbols: list[str],
    trade_date: date,
    *,
    enabled: bool,
    notes: list[str],
) -> dict[str, dict]:
    """龙虎榜席位是盘后披露事实，不可当作盘中资金流。

    **逐标的一次 HTTP，所以并发。** 实测每只 0.45s，而盘后候选池是几十只——串行
    要花十几秒，且这几十次请求彼此毫无依赖。`gather` 保序，所以 `mapped` 的插入
    顺序仍由 `symbols` 决定，不受完成顺序影响；下游按 symbol 取值不受影响，但
    note 里那句「x/y 只上榜」在两次跑之间必须是同一个顺序才好比对。
    """
    if not enabled or not symbols:
        return {}
    source = adapter if adapter is not None else LhbSeatAdapter()
    getter = getattr(source, "fetch", None)
    if not callable(getter):
        notes.append("龙虎榜席位适配器缺少 fetch，已跳过")
        return {}

    done, failed = gather(symbols, lambda symbol: getter(symbol, trade_date))
    for symbol, exc in failed:
        notes.append(f"龙虎榜席位 {symbol} 不可用（{type(exc).__name__}）")

    mapped: dict[str, dict] = {}
    for symbol, read in done:
        payload = read.to_dict() if hasattr(read, "to_dict") else read if isinstance(read, dict) else None
        if isinstance(payload, dict) and payload.get("covered"):
            mapped[symbol] = dict(payload)
    if mapped:
        notes.append(
            f"龙虎榜席位：{len(mapped)}/{len(symbols)} 只在 {trade_date} 上榜；"
            "为盘后披露事实，不代表盘中实时资金方向"
        )
    return mapped
