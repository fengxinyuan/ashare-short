"""Markdown rendering for the daily briefs.

Kept separate from the notifiers so every channel delivers identical content and
the rendering stays unit-testable without any IO.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ashare_decision_support.models.schemas import DailyTechnicalSnapshot

DISCLAIMER = "> 本报告由规则引擎自动生成，仅供研究参考，不构成投资建议；系统不下单、不接管账户。"

SECTOR_CAVEAT = (
    "> 板块打分融合量价动量、关键词舆情与龙虎榜资金三项读数，权重为人工设定、**未经回测验证**，"
    "只用于决定「先看哪里」，不构成方向预测。"
)

ADVICE_CAVEAT = (
    "> 买卖点由日线结构与分时结构规则推导，**未经回测验证**；"
    "盘中时效未经实测，收盘后取到的是最后一个交易日的完整数据。"
)


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def _join(items: list[str], sep: str = "；") -> str:
    return sep.join(item for item in items if item)


def _signed_yi(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value / 1e8:+.2f}"


def _trade_flow_summary(flow: Any) -> str | None:
    if not isinstance(flow, dict) or not flow.get("ok", False):
        return None
    parts: list[str] = []
    buy_ratio = flow.get("buy_ratio")
    if isinstance(buy_ratio, (int, float)):
        parts.append(f"主动买占比 {buy_ratio:.0%}")
    net_amount = flow.get("net_amount")
    if isinstance(net_amount, (int, float)):
        parts.append(f"净额 {_signed_yi(net_amount)} 亿")
    large_net = flow.get("large_net_amount")
    if isinstance(large_net, (int, float)):
        parts.append(f"大单净额 {_signed_yi(large_net)} 亿")
    tail_net = flow.get("tail_net_amount")
    tail_ratio = flow.get("tail_ratio")
    if isinstance(tail_net, (int, float)) and isinstance(tail_ratio, (int, float)):
        parts.append(f"尾盘净额 {_signed_yi(tail_net)} 亿，占全天净流入 {tail_ratio:.0%}")
    elif isinstance(tail_net, (int, float)):
        parts.append(f"尾盘净额 {_signed_yi(tail_net)} 亿")
    return _join(parts) if parts else None


def _sector_lead(market_outlook, outlooks: list, position_factor: float) -> str:
    parts: list[str] = []
    if market_outlook is not None:
        if getattr(market_outlook, "trusted", True):
            parts.append(
                f"大盘{getattr(market_outlook, 'direction', '—')}，"
                f"综合读数 {getattr(market_outlook, 'score', 0.0):.1f}/100"
            )
        else:
            parts.append(f"大盘判断暂不完整，当前读数 {getattr(market_outlook, 'direction', '—')}")
        warnings = getattr(market_outlook, "warnings", []) or []
        if warnings:
            parts.append(f"先防 {warnings[0]}")
    if outlooks:
        parts.append("先看 " + "、".join(item.name for item in outlooks[:3]))
    parts.append(f"仓位建议 {position_factor:.2f}")
    return _join(parts)


def _advice_price_parts(item: Any) -> list[str]:
    """价位那一半：买点、止损、目标、盈亏比。

    和「该不该做」那一半分开，是因为两者的**缺失含义不同**——没有打法是判据没
    过，没有价位是数据不够。合在一个函数里，两种「—」会长得一模一样。
    """
    parts: list[str] = []
    entry_low = getattr(item, "entry_low", None)
    entry_high = getattr(item, "entry_high", None)
    if entry_low is not None and entry_high is not None:
        parts.append(f"重点看买入区间 {_fmt(entry_low)}-{_fmt(entry_high)}")
    elif entry_low is not None:
        parts.append(f"买点参考 {_fmt(entry_low)}")
    stop = getattr(item, "stop", None)
    if stop is not None:
        parts.append(f"止损 {_fmt(stop)}")
    targets = getattr(item, "targets", []) or []
    if targets:
        parts.append(f"首目标 {_fmt(targets[0])}")
    rrr = getattr(item, "rrr", None)
    if rrr is not None:
        parts.append(f"盈亏比 {rrr}")
    return parts


def _advice_lead(item: Any) -> str:
    parts: list[str] = []
    # 打法排在最前。它回答的是「今天这只票能不能做、凭什么做」，而后面全是
    # 「做的话在哪个价」——顺序反过来，读的人会先记住价位再想起该不该做。
    playbook = getattr(item, "playbook", None)
    if playbook:
        parts.append(f"打法 {playbook}")
    elif getattr(item, "setup", None):
        parts.append("今日无可用打法")
    shortline = getattr(item, "shortline", None)
    if shortline is not None and getattr(shortline, "summary", None):
        parts.append(shortline.summary)
    trend = getattr(item, "trend_state", None)
    if trend:
        parts.append(f"趋势 {trend}")
    parts.extend(_advice_price_parts(item))
    warnings = getattr(item, "warnings", []) or []
    if warnings:
        parts.append(f"注意 {warnings[0]}")
    return _join(parts)


def _trade_context_summary(item: Any) -> str | None:
    trade = getattr(item, "trade_context", None)
    if trade is None:
        return None
    parts: list[str] = []
    turnover = getattr(trade, "turnover_ratio", None)
    if turnover is not None:
        parts.append(f"换手率 {_fmt(turnover, suffix='%')}")
    amount = getattr(trade, "session_amount", None)
    if amount is not None:
        parts.append(f"成交额 {_fmt(amount / 1e8)} 亿")
    float_cap_yi = getattr(trade, "float_cap_yi", None)
    if float_cap_yi is not None:
        parts.append(f"流通市值 {_fmt(float_cap_yi)} 亿")
    return _join(parts) if parts else None


_SCORE_COMPONENT_LABELS = {
    "sector_relation": "板块关系",
    "capital_flow": "资金",
    "volume_price": "量价",
    "catalyst": "事件催化",
}


def _score_attribution(item: Any) -> str | None:
    """Render the deterministic score as technical baseline plus context deltas."""
    components = getattr(item, "score_components", None)
    if not isinstance(components, dict):
        return None

    technical = components.get("technical")
    news = components.get("news")
    context_delta = components.get("context_delta")
    detail = [
        f"{label} {value:+.1f}"
        for key, label in _SCORE_COMPONENT_LABELS.items()
        if isinstance((value := components.get(key)), (int, float)) and value
    ]
    parts: list[str] = []
    if isinstance(technical, (int, float)):
        parts.append(f"价格/结构基准 {technical:.1f}")
    if isinstance(news, (int, float)) and news:
        parts.append(f"个股舆情 {news:+.1f}")
    if isinstance(context_delta, (int, float)):
        suffix = f"（{'、'.join(detail)}）" if detail else ""
        parts.append(f"上下文 {context_delta:+.1f}{suffix}")
    elif detail:
        parts.append("、".join(detail))
    return "；".join(parts) or None


def _render_notes_section(notes: list[str], heading: str = "数据边界") -> list[str]:
    if not notes:
        return []

    buckets = [
        ("采集与覆盖", ("快讯", "成分股", "行业指数", "全市场", "舆情", "采样", "回溯", "入库", "缓存")),
        ("筛选与样本", ("过滤", "剔除", "ST", "退市", "可交易", "低吸", "候选", "样本")),
        ("异常与回退", ("失败", "不可用", "分歧", "回退", "缺失", "旧数据", "过时", "对不上", "校验")),
        ("记录与台账", ("台账", "对照组", "兑现", "核对", "first_seen", "last_seen", "journal")),
    ]
    grouped: dict[str, list[str]] = {name: [] for name, _ in buckets}
    grouped["其他"] = []

    for note in notes:
        placed = False
        for name, tokens in buckets:
            if any(token in note for token in tokens):
                grouped[name].append(note)
                placed = True
                break
        if not placed:
            grouped["其他"].append(note)

    lines: list[str] = [f"## {heading}"]
    for name, items in grouped.items():
        if not items:
            continue
        if name != "其他" and len(grouped) > 1:
            lines.append(f"### {name}")
        for note in items:
            lines.append(f"- {note}")
    return lines


def render_theme_radar(signals: list[dict[str, Any]] | None) -> str:
    """Render the discovery-only theme evidence carried by the facts bundle."""
    if not signals:
        return ""
    lines = ["## 主题雷达", "- 主题雷达用于提前发现线索，不改变板块评分，也不构成买卖指令。"]
    for item in signals[:10]:
        name = item.get("name") or "未命名主题"
        stage = item.get("stage") or "—"
        sectors = "、".join(item.get("sectors") or []) or "相关板块"
        lines.append(f"### {name}（{stage}）")
        lines.append(
            f"- 关联板块：{sectors}；已验证消息 {item.get('verified_docs', 0)} 条；"
            f"社媒线索 {item.get('social_docs', 0)} 条"
        )
        if item.get("transmission"):
            lines.append(f"- 传导：{item['transmission']}")
        if item.get("event_types"):
            lines.append(f"- 事件：{'、'.join(item['event_types'])}")
        if item.get("invalidation"):
            lines.append(f"- 失效：{item['invalidation']}")
        for evidence in (item.get("evidence") or [])[:3]:
            source = evidence.get("source") or "未知来源"
            heat = evidence.get("engagement")
            suffix = f"，互动量 {heat:g}" if isinstance(heat, (int, float)) else ""
            link = f" [{evidence['url']}]" if evidence.get("url") else ""
            lines.append(f"- 证据：{evidence.get('title') or '—'}（{source}{suffix}）{link}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _candidate_table(rows: list[dict]) -> list[str]:
    """候选行的标准表。盘前、盘后、执行卡三处同一份行结构（`SelectedSymbol.to_row`），
    所以共用同一张表——列错位在这里是最容易发生也最难看出来的一类 bug。"""
    lines = ["| 标的 | 打法 | 评分 | 现价 | 趋势 | 止损参考 | 目标参考 |", "|---|---|---|---|---|---|---|"]
    for row in rows:
        name = f" {row['name']}" if row.get("name") else ""
        lines.append(
            f"| {row['symbol']}{name} | {row.get('playbook') or '—'} | "
            f"{row.get('score', 0.0):.1f} | {_fmt(row.get('last_close'))} | "
            f"{row.get('trend_state') or '—'} | {_fmt(row.get('stop_reference'))} | "
            f"{_fmt(row.get('target_reference'))} |"
        )
    return lines


def _reject_reasons(row: dict) -> list[str]:
    """「这只票今天为什么不能买」——判据链自己给出的那句话。"""
    setup = row.get("setup") or {}
    reasons: list[str] = []
    for item in setup.get("rejected") or []:
        reasons.extend(str(text) for text in (item.get("reasons") or []))
    blocked = setup.get("blocked_by_regime") or []
    if blocked:
        reasons.append(f"{'、'.join(blocked)} 被今天的情绪周期挡掉")
    return reasons


def _symbol_label(row: dict) -> str:
    name = str(row.get("name") or "").strip()
    return f"{row['symbol']} {name}" if name else row["symbol"]


def _regime_line(payload: dict) -> str:
    regime = payload.get("regime") or {}
    cycle = regime.get("cycle") or "—"
    cap = regime.get("position_cap")
    parts = [f"**情绪周期** {cycle}"]
    if cap is not None:
        parts.append(f"**周期仓位上限** {cap:.2f}")
    factor = payload.get("position_factor")
    if factor is not None:
        parts.append(f"**本次仓位系数** {factor:.2f}")
    if regime.get("leader_only"):
        parts.append("**只做板块龙头**")
    if regime.get("trusted") is False:
        parts.append("⚠️ 周期判定不可信（涨停池缺失）")
    return "　".join(parts)


def _exit_lines(row: dict) -> list[str]:
    """卖出判据。**买入侧和卖出侧同时亮起来的那些票最该被看见**——
    「进场判据成立」和「天量滞涨成立」可以是同一天的事。"""
    exits = row.get("exits") or {}
    signals = exits.get("signals") or []
    if not signals:
        return []
    label = {"exit": "离场", "trim": "减仓", "watch": "观察"}
    lines = []
    for item in signals:
        lines.append(
            f"  - 【{label.get(item.get('action'), item.get('action'))}】{item.get('reason')}"
        )
    return lines


def _lhb_seat_summary(reading: Any) -> str | None:
    if not isinstance(reading, dict) or not reading.get("covered"):
        return None
    categories = reading.get("net_by_category") or {}
    parts = [f"{name}净额 {value / 1e4:+.0f} 万" for name, value in categories.items() if isinstance(value, (int, float))]
    concentration = reading.get("concentration")
    if isinstance(concentration, (int, float)):
        parts.append(f"最大席位占成交 {concentration:.1%}")
    return "；".join(parts) or None


def _post_close_conclusion(actionable: list[dict], watch_only: list[dict]) -> str:
    if actionable:
        top = actionable[0]
        lead = f"明天先盯 {top['symbol']}（{top.get('playbook') or '—'}）"
        if len(actionable) > 1:
            lead += f" 以及另外 {len(actionable) - 1} 只"
        return lead + "；先看有没有打法，再看价位。"
    if watch_only:
        # 「没有可用打法」和「没有标的」必须长得不一样。
        return (
            f"这 {len(watch_only)} 只**没有一只**满足今天允许的入场判据——"
            "不是它们不够好，是今天没有位置。逐只理由见下。"
        )
    return "没有可用日线，本次无法给出任何判断。"


def _actionable_card(row: dict) -> list[str]:
    """一只可做标的的整张卡片。"""
    lines = [
        (
            f"### {row['symbol']}{' ' + row['name'] if row.get('name') else ''}"
            f"（{row.get('playbook') or '—'}　{row.get('score', 0.0):.1f}）"
        )
    ]
    if row.get("reasons"):
        lines.append("- 为什么：" + "；".join(row["reasons"]))
    if row.get("warnings"):
        lines.append("- 风险：" + "；".join(row["warnings"]))
    trade_flow = _trade_flow_summary(row.get("trade_flow"))
    if trade_flow:
        lines.append("- 资金旁证：" + trade_flow)
    lhb = _lhb_seat_summary(row.get("lhb_seats"))
    if lhb:
        lines.append("- 龙虎榜席位（盘后披露）：" + lhb)
    exit_lines = _exit_lines(row)
    if exit_lines:
        # 买入判据成立、卖出判据同时亮着——这是这张卡上最该被读到的冲突。
        lines.append("- ⚠️ 卖出判据同时成立：")
        lines.extend(exit_lines)
    lines.append("")
    return lines


def _watch_only_card(row: dict) -> list[str]:
    name = f" {row['name']}" if row.get("name") else ""
    lines = [
        (
            f"- **{row['symbol']}{name}**（{row.get('trend_state') or '—'}　"
            f"{row.get('score', 0.0):.1f}）"
        )
    ]
    lines.extend(f"  - {reason}" for reason in _reject_reasons(row))
    trade_flow = _trade_flow_summary(row.get("trade_flow"))
    if trade_flow:
        lines.append(f"  - 资金旁证：{trade_flow}")
    lhb = _lhb_seat_summary(row.get("lhb_seats"))
    if lhb:
        lines.append(f"  - 龙虎榜席位（盘后披露）：{lhb}")
    return lines


def render_post_close_brief(payload: dict) -> str:
    """盘后简报。输入是 `jobs/post_close.py` 落盘的那份 payload，不是模型对象——
    简报与执行卡读同一份字典，两边就不可能对不上。"""
    lines: list[str] = []
    trade_date = payload.get("trade_date")
    lines.append(
        f"**交易日** {trade_date}　**策略版本** {payload.get('strategy_version') or '—'}"
    )
    lines.append(_regime_line(payload))
    lines.append("")

    actionable = list(payload.get("actionable") or [])
    watch_only = list(payload.get("watch_only") or [])

    lines.append("## 先看结论")
    lines.append(_post_close_conclusion(actionable, watch_only))
    lines.append("")

    lines.append("## 明天可以做的")
    if not actionable:
        lines.append("无。今日不产出买入候选。")
    else:
        lines.extend(_candidate_table(actionable))
        lines.append("")
        for row in actionable:
            lines.extend(_actionable_card(row))

    lines.append("## 只能看，不能做")
    if not watch_only:
        lines.append("无——本次的标的全部有可用打法。")
    else:
        lines.append("这些票今天没有满足判据的打法。**理由本身就是答案**，不是「没选上」。")
        lines.append("")
        for row in watch_only:
            lines.extend(_watch_only_card(row))
    lines.append("")

    lines.extend(_render_notes_section(payload.get("data_notes") or []))
    if payload.get("data_notes"):
        lines.append("")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _market_read_section(market_outlook) -> list[str]:
    if market_outlook is None:
        return []
    lines = ["## 大盘怎么读"]
    if not getattr(market_outlook, "trusted", True):
        # "没测出来" 和 "测出来是中性" 必须长得不一样。
        lines.append(f"**{market_outlook.direction}** — 覆盖率不足，本次不给方向判断。")
    else:
        lines.append(
            f"**{market_outlook.direction}**（综合读数 {market_outlook.score:.1f}／100，"
            f"{market_outlook.sectors_trusted} 个达标板块 "
            f"{market_outlook.constituents_covered} 只成分股）"
        )
    lines.extend(f"- {reason}" for reason in getattr(market_outlook, "reasons", []))
    lines.extend(f"- ⚠️ {warning}" for warning in getattr(market_outlook, "warnings", []))
    lines.append("")
    return lines


def _rotation_memory_section(rotation: dict | None) -> list[str]:
    if not rotation or not rotation.get("observations"):
        return []
    lines = [
        "## 轮动记忆",
        (
            f"**{rotation.get('regime') or '样本不足'}**"
            f"（近 {rotation.get('available_days') or 0} 个交易样本，"
            f"前 {rotation.get('leader_count') or 0} 主线）"
        ),
    ]
    lines.extend(f"- {text}" for text in rotation.get("observations") or [])
    lines.append("")
    return lines


def _rotation_price_section(rotation_price: dict | None) -> list[str]:
    """价格口径的轮动，独立成节。

    **不与「轮动记忆」合并**：那一节读的是系统自己的排名（要连续跑若干天才有
    样本），这一节读的是板块指数的真实价格（第一次跑就有当年全部日线）。合成
    一节会让读的人分不清结论的出处，而两者的可信度在系统刚上线的那几周相差极大。
    """
    if not rotation_price or not rotation_price.get("observations"):
        return []
    # 两个窗口各自的标签都摆出来。只报长窗口那个的话，读者会拿一个月的趋势
    # 结论去做一周的决定——而这两者在 2026-08-03 是反号的（月度「强者恒强」、
    # 周度「高低切换」）。
    adjacent = rotation_price.get("adjacent_label")
    heading = f"**{rotation_price.get('switch_label') or '样本不足'}**（月度）"
    if adjacent:
        heading += f"　**{adjacent}**（周度）"
    lines = [
        "## 轮动到哪了（指数价格口径）",
        heading
        + f"　{rotation_price.get('sectors') or 0} 个板块指数，"
        f"截至 {rotation_price.get('trade_date') or '—'}",
    ]
    lines.extend(f"- {text}" for text in rotation_price.get("observations") or [])
    lines.append("- 以上均为对**已发生**区间的测量，不含对明日的推断；本层不参与打分。")
    lines.append("")
    return lines


def _turnover_section(turnover: dict | None) -> list[str]:
    """成交额口径，独立成节。

    与轮动那节同源（同一批板块指数序列）但问的不是同一件事：那节用 close 问
    「谁在涨」，这节用 amount 问「谁在被交易、那是在买还是在跑」。合并会让
    「占比抬升」被顺手读成「资金流入」——而放量下跌同样会抬升占比。
    """
    if not turnover or not turnover.get("observations"):
        return []
    lines = ["## 成交额挪到哪了（成交额口径）"]
    total_change = turnover.get("total_change")
    if isinstance(total_change, (int, float)):
        # 标题行是这一节唯一会被扫一眼就走的行，所以块均值与窗口内走向反号时，
        # 两个数都要出现在这里。只印块均值的话，量能刚触底回升的那一周，最显眼
        # 的一行印的是「-1.8%」——而当天实际发生的是放量 33%。
        headline = f"**全场日均成交额 {total_change:+.1%}"
        total_trend = turnover.get("total_trend")
        if isinstance(total_trend, (int, float)) and total_trend * total_change < 0:
            headline += f"，但近端窗口内 {total_trend:+.1%}"
        lines.append(
            headline + "**"
            f"（{turnover.get('sectors') or 0} 个板块指数，"
            f"截至 {turnover.get('trade_date') or '—'}）"
        )
    lines.extend(f"- {text}" for text in turnover.get("observations") or [])
    lines.append(
        "- 成交额是**双边**的，一笔成交同时是买和卖；这一层只说注意力挪去了哪里，"
        "不等于「资金净流入」。本层不参与打分。"
    )
    lines.append("")
    return lines


def _earnings_section(earnings: dict | None) -> list[str]:
    if not earnings:
        return []
    lines = ["## 业绩面"]
    lines.extend(f"- {text}" for text in earnings.get("summary") or [])
    lines.append("- 业绩层是**双面不对称**的：亏损与大幅预减移除标的，预增只标注不加分。")
    lines.append("")
    return lines


def _sector_ranking_section(outlooks: list) -> list[str]:
    lines = ["## 板块怎么排"]
    if not outlooks:
        lines.append("无可用板块读数。")
        return lines
    lines.append("| # | 板块 | 评分 | 倾向 | 当日中位 | 5日中位 | 上涨占比 | 量比 | 覆盖 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for index, item in enumerate(outlooks, start=1):
        metrics = item.metrics
        lines.append(
            f"| {index} | {item.name} | {item.score:.1f} | {item.direction} | "
            f"{_pct(metrics.ret_1d)} | {_pct(metrics.ret_5d)} | {_pct(metrics.breadth, signed=False)} | "
            f"{_fmt(metrics.volume_ratio)} | {metrics.covered}/{metrics.constituents} |"
        )
    lines.append("")
    for item in outlooks:
        lines.append(f"### {item.name}（{item.score:.1f}　{item.direction}）")
        if item.reasons:
            lines.append("- 看点：" + "；".join(item.reasons))
        if item.warnings:
            lines.append("- 风险：" + "；".join(item.warnings))
        if item.headlines:
            lines.append("- 相关新闻：" + "　/　".join(item.headlines))
        event_types = getattr(item, "news_event_types", ()) or ()
        news_hits = getattr(item, "news_hits", ()) or ()
        if event_types:
            lines.append(
                "- 行业事件："
                + "、".join(event_types)
                + (f"（证据：{'、'.join(news_hits[:5])}）" if news_hits else "")
            )
        lines.append("")
    return lines


def _candidate_profile_lines(item: dict) -> list[str]:
    """标的画像与主题链路——**背景信息，不进个股评分**。

    单独一段是因为这两行的地位和上面的价位不同：行业/题材/主题只解释「它属于
    今天在动的哪一类」，不参与选它的判据。
    """
    lines: list[str] = []
    labels = []
    if item.get("industry_path"):
        labels.append(str(item["industry_path"]))
    concepts = list(item.get("concepts") or [])
    if concepts:
        labels.append("题材 " + "、".join(concepts[:3]) + ("…" if len(concepts) > 3 else ""))
    if labels:
        lines.append("  - 标的画像：" + "；".join(labels))
    theme_context = list(item.get("theme_context") or [])
    if theme_context:
        theme_summary = "；".join(
            f"{theme.get('name', '—')}（{theme.get('stage', '—')}）"
            for theme in theme_context[:3]
        )
        lines.append(f"  - 主题链路（只作背景，不进个股评分）：{theme_summary}")
        invalidation = [str(theme.get("invalidation")) for theme in theme_context if theme.get("invalidation")]
        if invalidation:
            lines.append("  - 主题失效条件：" + "；".join(invalidation[:2]))
    return lines


def _sector_candidate_card(item: dict) -> list[str]:
    """板块里一只标的的明细块。"""
    lines = [f"- **{_symbol_label(item)}**（{item['sector']}　{item['score']:.1f}）"]
    summary = []
    if item.get("trend_state"):
        summary.append(f"趋势 {item['trend_state']}")
    if item.get("last_close") is not None:
        summary.append(f"现价 {_fmt(item.get('last_close'))}")
    if item.get("stop_reference") is not None:
        summary.append(f"止损 {_fmt(item.get('stop_reference'))}")
    if item.get("target_reference") is not None:
        summary.append(f"目标 {_fmt(item.get('target_reference'))}")
    if summary:
        lines.append("  - 先看什么：" + "；".join(summary))
    lines.extend(_candidate_profile_lines(item))
    if item.get("reasons"):
        lines.append("  - 为什么：" + "；".join(item["reasons"]))
    flow = _trade_flow_summary(item.get("trade_flow"))
    if flow:
        # 单起一行而不是并进「为什么」：那一行讲的是选它的结构理由，
        # 逐笔是选完之后的旁证，不该读起来像是入选依据之一。
        lines.append(f"  - 昨日逐笔（旁证，不进打分）：{flow}")
    if item.get("warnings"):
        lines.append("  - 风险：" + "；".join(item["warnings"]))
    return lines


def _sector_candidates_section(candidates: list[dict]) -> list[str]:
    lines = ["## 板块里先看谁"]
    if not candidates:
        lines.append("入选板块中无标的通过筛选（ST/公告风险过滤后为空）。")
        return lines
    lines.append("| 标的 | 所属板块 | 评分 | 现价 | 趋势 | 止损参考 | 目标参考 |")
    lines.append("|---|---|---|---|---|---|---|")
    for item in candidates:
        lines.append(
            f"| {_symbol_label(item)} | {item['sector']} | {item['score']:.1f} | "
            f"{_fmt(item.get('last_close'))} | {item.get('trend_state') or '—'} | "
            f"{_fmt(item.get('stop_reference'))} | {_fmt(item.get('target_reference'))} |"
        )
    lines.append("")
    for item in candidates:
        lines.extend(_sector_candidate_card(item))
    lines.append("")
    return lines


def _position_section(market) -> list[str]:
    lines = ["## 仓位怎么收"]
    # MarketOutlook carries two distinct reason lists — one for the direction,
    # one for the position factor. Prefer the factor's, or this section repeats
    # the direction rationale under a heading that promises sizing.
    sentiment = getattr(market, "sentiment", None)
    reasons = getattr(sentiment, "reasons", None) or getattr(market, "reasons", [])
    lines.extend(f"- {reason}" for reason in reasons or [])
    lines.append("")
    return lines


def render_sector_brief(
    trade_date: date,
    outlooks: list,
    candidates: list[dict],
    market,
    position_factor: float,
    data_notes: list[str] | None = None,
    market_outlook=None,
    rotation: dict | None = None,
    rotation_price: dict | None = None,
    turnover: dict | None = None,
    earnings: dict | None = None,
    theme_radar: list[dict[str, Any]] | None = None,
) -> str:
    """Top-down pre-open card: market first, then sectors, then the names."""
    lines: list[str] = []
    lines.append(f"**交易日** {trade_date}　**仓位建议系数** {position_factor:.2f}")
    lines.append("")

    lines.append("## 先看结论")
    lines.append(_sector_lead(market_outlook, outlooks, position_factor))
    lines.append("")
    lines.append("这页只回答今天先看哪里，不回答必然涨跌；板块排序是优先级，不是预测。")
    lines.append("")

    lines.extend(_market_read_section(market_outlook))
    lines.extend(_rotation_memory_section(rotation))
    lines.extend(_rotation_price_section(rotation_price))
    lines.extend(_turnover_section(turnover))
    lines.extend(_earnings_section(earnings))

    theme_block = render_theme_radar(theme_radar)
    if theme_block:
        lines.append(theme_block)
        lines.append("")

    lines.extend(_sector_ranking_section(outlooks))
    lines.extend(_sector_candidates_section(candidates))
    lines.extend(_position_section(market))

    lines.extend(_render_notes_section(data_notes or []))
    if data_notes:
        lines.append("")

    lines.append(sector_mandatory_block(position_factor))
    return "\n".join(lines)


def sector_mandatory_block(position_factor: float) -> str:
    """Discipline and caveats that must reach the reader on every channel.

    Kept as its own function so the LLM path can append exactly what the
    template path renders, instead of relying on the model to carry it through
    a rewrite.
    """
    return "\n".join(
        [
            "## 纪律提醒",
            "- 板块排序解决的是「今天先看哪里」，不解决「买不买」；个股仍需逐一确认。",
            "- 集合竞价一旦要参与，委托必须在 **09:24:58 之前**发出；09:20-09:25 **不接受撤单**。",
            f"- 单日总仓位不超过仓位系数 {position_factor:.2f} 对应的上限。",
            "",
            SECTOR_CAVEAT,
            DISCLAIMER,
        ]
    )


def _pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.2%}" if signed else f"{value:.0%}"


def _advice_levels_table(advices: list) -> list[str]:
    lines = [
        "## 价位地图",
        "| 标的 | 名称 | 打法 | 现价 | 买入区间 | 止损 | 目标 | 盈亏比 | 趋势 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in advices:
        entry = (
            f"{item.entry_low:.2f}-{item.entry_high:.2f}"
            if item.entry_low is not None and item.entry_high is not None
            else "—"
        )
        targets = "/".join(f"{level:.2f}" for level in item.targets) if item.targets else "—"
        lines.append(
            f"| {item.symbol} | {item.name or '—'} | "
            f"{getattr(item, 'playbook', None) or '—'} | {_fmt(item.last_price)} | {entry} | "
            f"{_fmt(item.stop)} | {targets} | "
            f"{item.rrr if item.rrr is not None else '—'} | {item.trend_state or '—'} |"
        )
    lines.append("")
    return lines


def _advice_evidence_lines(item: Any) -> list[str]:
    """一只标的的**旁证**：量价判别、相对宽基、事件与风险证据。

    和「先看什么 / 为什么」分开：那些是入场判据本身，这些是判据之外的佐证。
    并成一段的话，读的人会把「相对宽基超额 +1.2%」记成入选理由之一。
    """
    lines: list[str] = []
    distribution = getattr(item, "distribution", None) or {}
    if distribution.get("verdict"):
        lines.append(
            f"- 量价判别：{distribution['verdict']}（派发 {distribution.get('distribution_points', 0):.1f} / "
            f"洗盘 {distribution.get('shakeout_points', 0):.1f}）"
        )
    relative = getattr(item, "relative_strength", None) or {}
    excess = relative.get("excess") if isinstance(relative, dict) else None
    excess_1d = excess.get("1d") if isinstance(excess, dict) else None
    benchmark = relative.get("benchmark_name") if isinstance(relative, dict) else None
    label = relative.get("label") if isinstance(relative, dict) else None
    if isinstance(excess_1d, (int, float)) and benchmark and label:
        lines.append(f"- 相对宽基：{label}，1 日相对 {benchmark} 超额 {excess_1d:+.2%}")
    event = getattr(item, "event_evidence", None) or {}
    event_types = event.get("event_types") if isinstance(event, dict) else None
    hits = event.get("hits") if isinstance(event, dict) else None
    risk_hits = event.get("risk_hits") if isinstance(event, dict) else None
    if event_types:
        suffix = f"（{'、'.join(hits[:5])}）" if hits else ""
        lines.append(f"- 事件催化证据：{'、'.join(event_types)}{suffix}")
    if risk_hits:
        lines.append(f"- 风险证据：{'、'.join(risk_hits[:5])}")
    return lines


def _no_playbook_line(item: Any) -> str | None:
    """没有打法成立时，把判据链的拒绝理由摊开。

    这句话本身就是「这只票今天为什么不该买」的答案，比一个低分有用得多。
    """
    setup = getattr(item, "setup", None) or {}
    if not setup or setup.get("matched"):
        return None
    reasons = [
        text
        for entry in (setup.get("rejected") or [])
        for text in (entry.get("reasons") or [])
    ]
    return "- 今日无可用打法：" + "；".join(reasons) if reasons else None


def _advice_detail_header(item: Any) -> str:
    header = f"### {item.symbol}"
    if item.name:
        header += f" {item.name}"
    if item.score is not None:
        header += f"（评分 {item.score:.1f}）"
    return header


def _advice_readings_lines(item: Any) -> list[str]:
    """判断与读数：先看什么、短线怎么判、数据长什么样、分是怎么来的。"""
    lines: list[str] = []
    lead = _advice_lead(item)
    if lead:
        lines.append("- 先看什么：" + lead)
    no_playbook = _no_playbook_line(item)
    if no_playbook:
        lines.append(no_playbook)
    shortline = getattr(item, "shortline", None)
    if shortline is not None and getattr(shortline, "summary", None):
        lines.append(f"- 短线判断：{shortline.summary}")
    trade_summary = _trade_context_summary(item)
    if trade_summary:
        lines.append("- 交易数据：" + trade_summary)
    attribution = _score_attribution(item)
    if attribution:
        lines.append("- 评分归因：" + attribution)
    return lines


def _advice_rationale_lines(item: Any) -> list[str]:
    """依据链：证据 → 短线理由 → 综合理由 → 台账口径 → 价位出处。"""
    lines = list(_advice_evidence_lines(item))
    shortline = getattr(item, "shortline", None)
    if shortline is not None and getattr(shortline, "reasons", None):
        lines.append("- 短线依据：" + "；".join(shortline.reasons))
    if item.reasons:
        lines.append("- 为什么：" + "；".join(item.reasons))
    lines.extend(f"- {line}" for line in getattr(item, "book_lines", []) or [])
    lines.extend(_levels_provenance(item))
    return lines


def _advice_outlook_lines(item: Any) -> list[str]:
    """情景分叉与风险。情景之间只有触发条件，没有概率——见 advice_mandatory_block。"""
    lines: list[str] = []
    if item.scenarios:
        lines.append("- 接下来怎么分：")
        lines.extend(
            f"  - **{scenario.name}**：{scenario.trigger} → {scenario.implication}"
            for scenario in item.scenarios
        )
    lines.extend(f"- 风险：{warning}" for warning in item.warnings)
    return lines


def _advice_detail(item: Any) -> list[str]:
    """逐只看法里的一只。"""
    return [
        _advice_detail_header(item),
        *_advice_readings_lines(item),
        *_advice_rationale_lines(item),
        *_advice_outlook_lines(item),
        "",
    ]


def render_advice_brief(trade_date, advices: list, data_notes: list[str] | None = None) -> str:
    """Per-symbol levels and forward scenarios."""
    lines: list[str] = []
    lines.append(f"**查询日** {trade_date}　**标的数** {len(advices)}")
    lines.append("")

    lines.append("## 先看结论")
    if not advices:
        lines.append("没有标的需要解释，直接传入代码即可。")
    else:
        for item in advices[:6]:
            name = f" {item.name}" if item.name else ""
            lines.append(f"- **{item.symbol}{name}**：{_advice_lead(item)}")
    lines.append("")
    lines.append("这页给的是结构位，不是预测；先看能不能做，再看怎么做。")
    lines.append("")

    stale = [item for item in advices if item.session_date and not item.is_current_session]
    if stale:
        lines.append(
            f"> ⚠️ 以下标的的分钟数据为 {stale[0].session_date}，**非当日盘中**："
            f"{'、'.join(item.symbol for item in stale)}。价位按该日收盘结构给出。"
        )
        lines.append("")

    lines.extend(_advice_levels_table(advices))

    lines.append("## 逐只看法")
    for item in advices:
        lines.extend(_advice_detail(item))

    lines.extend(_render_notes_section(data_notes or []))
    if data_notes:
        lines.append("")

    lines.append(advice_mandatory_block())
    return "\n".join(lines)


def _levels_provenance(item: Any) -> list[str]:
    """价位的**出处**，以及这组价是谁选的。

    L2 给每个候选都带了 `basis`，台账也存了，但读简报的人看不到台账——不印
    出来的话，「22.80 是从分时均价来的还是从买盘墙来的」在唯一会被真人读到的
    那份产物里仍然是缺的，而那正是把买卖点改成候选集的初衷。

    未选中的候选一并列出（最多三个）：它让「为什么不是那个价」变成可追问的，
    也是事后复盘时唯一能还原当时取舍空间的地方。
    """
    verdict = getattr(item, "adjudication", None)
    if verdict is None:
        return []

    lines: list[str] = []
    chosen = [
        (label, leg)
        for label, leg in (("买入", verdict.entry), ("止损", verdict.stop), ("目标", verdict.target))
        if leg is not None
    ]
    if chosen:
        parts = "　·　".join(f"{label} {leg.basis} {leg.price:.2f}" for label, leg in chosen)
        lines.append(f"- 价位出处：{parts}（{_decider_label(verdict)}）")

    candidates = getattr(item, "candidates", None)
    if candidates is not None:
        others = [entry for entry in candidates.entries if entry is not verdict.entry]
        if others:
            lines.append(
                "- 未选的买入候选："
                + "、".join(f"{entry.basis} {entry.price:.2f}" for entry in others[:3])
            )
    return lines


def _decider_label(verdict: Any) -> str:
    if verdict.abstained:
        return "模型弃权"
    if verdict.used_fallback:
        return "规则默认排序"
    return "模型推翻规则" if verdict.overrode_rule else "模型认可规则默认"


def advice_mandatory_block() -> str:
    """Discipline text that must reach the reader on every channel."""
    return f"## 纪律提醒\n- 价位是**结构位**，不是预测；触发前不动，触发后按纪律执行，不临场改止损。\n- 情景之间没有概率，本系统**没有拟合模型**，给不出「多大概率上涨」这类数字。\n- 单笔亏损控制在总资金可承受范围内；盈亏比不足 1.5 的机会直接放弃。\n\n{ADVICE_CAVEAT}\n{DISCLAIMER}"


def _pre_open_execution_section(
    rows: list[dict],
    snapshots: dict[str, DailyTechnicalSnapshot],
) -> list[str]:
    lines = ["## 今天怎么执行"]
    if not rows:
        lines.append("昨日无可执行候选，今日空仓观望。")
        return lines

    lines.append("| 标的 | 打法 | 现价 | 止损参考 | 目标参考 | 评分 |")
    lines.append("|---|---|---|---|---|---|")
    for row in rows:
        snapshot = snapshots.get(row["symbol"])
        # 快照在的话用快照（盘前重算过），否则退回 payload 里昨收口径的那份。
        stop = snapshot.stop_reference if snapshot else row.get("stop_reference")
        target = snapshot.target_reference if snapshot else row.get("target_reference")
        close = snapshot.last_close if snapshot else row.get("last_close")
        lines.append(
            f"| {row['symbol']} | {row.get('playbook') or '—'} | {_fmt(close)} | "
            f"{_fmt(stop)} | {_fmt(target)} | {row.get('score', 0.0):.1f} |"
        )
    lines.append("")
    for row in rows:
        name = f" {row['name']}" if row.get("name") else ""
        lines.append(f"- **{row['symbol']}{name}**（{row.get('playbook') or '—'}）")
        if row.get("reasons"):
            lines.append(f"  - 依据：{'；'.join(row['reasons'])}")
        if row.get("warnings"):
            lines.append(f"  - 风险：{'；'.join(row['warnings'])}")
    lines.append("")
    return lines


def _pre_open_skip_section(watch_only: list[dict]) -> list[str]:
    if not watch_only:
        return []
    lines = ["## 今天不碰"]
    for row in watch_only:
        reasons = _reject_reasons(row)
        name = f" {row['name']}" if row.get("name") else ""
        lines.append(f"- **{row['symbol']}{name}**：{reasons[0] if reasons else '无可用打法'}")
    lines.append("")
    return lines


def _pre_open_discipline_section(factor: float | None) -> list[str]:
    lines = [
        "## 纪律提醒",
        "- 集合竞价一旦要参与，委托必须在 **09:24:58 之前**发出；09:25 撮合完成后再下单只能算开盘单。",
        "- 09:20-09:25 期间**不接受撤单**，报价前先确认仓位与价格。",
    ]
    if factor is not None:
        lines.append(f"- 单日总仓位不超过仓位系数 {factor:.2f} 对应的上限。")
    lines.append("")
    return lines


def render_pre_open_brief(
    payload: dict,
    snapshots: dict[str, DailyTechnicalSnapshot],
    data_notes: list[str] | None = None,
) -> str:
    rows = list(payload.get("actionable") or [])
    watch_only = list(payload.get("watch_only") or [])

    lines: list[str] = [
        (
            f"**对应交易日** {payload.get('trade_date')}　"
            f"**策略版本** {payload.get('strategy_version') or '—'}"
        ),
        _regime_line(payload),
        "",
        "## 先看结论",
        f"今日先看 {rows[0]['symbol']} 等 {len(rows)} 只候选；这页是执行卡，不是预测卡。"
        if rows
        else "昨日无可执行候选，今日先按空仓观望。",
        "",
    ]

    lines.extend(_pre_open_execution_section(rows, snapshots))
    lines.extend(_pre_open_skip_section(watch_only))
    lines.extend(_pre_open_discipline_section(payload.get("position_factor")))

    lines.extend(_render_notes_section(data_notes or []))
    if data_notes:
        lines.append("")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


# ---- intraday alerts -----------------------------------------------------

LEVEL_BADGE = {"ACTION": "🔴", "WARN": "🟠", "INFO": "🔵"}


# 触发 → 一句话动作。查表而不是 if 阶梯：这里唯一的逻辑是「持仓表优先于通用表」，
# 其余全是文案。同一个 trigger 在两张表里含义不同（target_hit 对持仓是兑现，
# 对未持仓的票是「别把兑现信号当买点」），所以必须是两张表而不是一张带例外。
_HOLDING_ACTIONS: dict[str, str] = {
    "stop_break": "持仓风控：{headline}，先处理卖出/减仓纪律。",
    "structure_break": "持仓风控：{headline}，先处理卖出/减仓纪律。",
    "giveback": "持仓风控：{headline}，先处理卖出/减仓纪律。",
    "target_hit": "持仓兑现：{headline}，按计划分批落袋。",
    "time_stop": "持仓复核：{headline}，不要继续无条件耗着。",
}

_TRIGGER_ACTIONS: dict[str, str] = {
    "entry_confirmed": "买点确认：{headline}，只按区间和止损执行。",
    "entry_zone": "买点到位：{headline}，若盘口能成交，先小仓试错。",
    "entry_watch": "只观察：{headline}，进区间但没止跌，不追。",
    "entry_sealed": "不可成交：{headline}，封板买不进。",
    "reclaim_confirmed": "修复观察：{headline}，先确认站稳，不直接追。",
    "breakout_confirmed": "突破确认：{headline}，只按突破失败线执行。",
    "target_hit": "目标触及：{headline}，不要把兑现信号当买点。",
    "vwap_lost": "结构变化：{headline}，先看是否快速收回。",
    "scenario_shift": "结构变化：{headline}，先看是否快速收回。",
    "risk_notice": "风险否决：{headline}，先暂停新增交易。",
}

# 表里查不到时按级别兜底：级别总是有的，trigger 可能是新加的。
_LEVEL_ACTIONS: dict[str, str] = {
    "ACTION": "需要处理：{headline}。",
    "WARN": "观察提醒：{headline}。",
}


def _alert_action_line(alert) -> str:
    """Human action summary for the top of an intraday push."""
    trigger = str(getattr(alert, "trigger", "")).split(":", 1)[0]
    headline = getattr(alert, "headline", "")
    level = getattr(alert, "level", "")

    template = None
    if getattr(alert, "holding", False):
        template = _HOLDING_ACTIONS.get(trigger)
    if template is None:
        template = _TRIGGER_ACTIONS.get(trigger)
    if template is None:
        template = _LEVEL_ACTIONS.get(level, "状态更新：{headline}。")
    return template.format(headline=headline)


def _sort_alerts(alerts: list) -> list:
    level_rank = {"ACTION": 0, "WARN": 1, "INFO": 2}
    return sorted(
        alerts,
        key=lambda item: (
            not getattr(item, "holding", False),
            level_rank.get(getattr(item, "level", ""), 9),
            getattr(item, "symbol", ""),
            getattr(item, "trigger", ""),
        ),
    )


def alert_title(alerts: list) -> str:
    """告警推送的标题：有 ACTION 就把条数点出来，否则只说共几条。

    和 `render_alerts` 放在一起，是因为两条投递路径都要它——`jobs/monitor.py`
    的单次跑批与 `monitor/loop.py` 的轮询循环，此前各写了一份逐字相同的。

    标题是**用户在手机通知栏里唯一看得见的那一行**：正文要展开推送才读得到，
    而「3 条 ACTION」和「盘中观察提醒 3 条」决定他要不要现在就打开。两份实现
    各自演化的后果，是同一套系统按投递路径给出不同措辞的通知。
    """
    action = sum(1 for alert in alerts if getattr(alert, "level", "") == "ACTION")
    if action:
        return f"盘中买卖点提醒 {action} 条 ACTION / 共 {len(alerts)} 条"
    return f"盘中观察提醒 {len(alerts)} 条"


def render_alerts(alerts: list, health) -> str:
    """Compact alert card. Deliberately template-only — see docs/ARCHITECTURE.md §五.

    Every alert carries the as-of timestamp of the bar it was computed from.
    Without it a push is indistinguishable from one built on yesterday's close,
    which is the single most dangerous thing an automated channel can send.
    """
    lines: list[str] = []
    ordered = _sort_alerts(alerts)

    lines.append("## 先处理")
    for alert in ordered:
        badge = LEVEL_BADGE.get(alert.level, "•")
        lines.append(f"- {badge} {_alert_action_line(alert)}")
    lines.append("")
    lines.append("## 触发依据")
    # 持仓排在最前。同一批推送里，「你手上那只跌破止损了」和「你临时盯的那只进
    # 买区」不该按标的顺序混排——前者的漏读代价是真金白银。
    for alert in ordered:
        badge = LEVEL_BADGE.get(alert.level, "•")
        stamp = f"{alert.as_of:%H:%M}" if alert.as_of else "时间未知"
        origin = "【持仓】" if getattr(alert, "holding", False) else ""
        lines.append(f"{badge} **[{alert.level}] {origin}{alert.headline}**　as-of {stamp}")
        lines.extend(f"　{item}" for item in alert.detail)
        lines.append("")

    if getattr(health, "verdict", None) == "degraded":
        lines.append(f"> ⚠️ 数据源滞后 {health.lag_minutes} 分钟，价位请以实际盘口为准。")
    lines.append(DISCLAIMER)
    return "\n".join(lines).strip()


def render_feed_suspension(health) -> str:
    """The one message sent when the gate closes, and the one when it reopens."""
    if getattr(health, "usable", False):
        return "\n".join(
            [
                "✅ **分钟线数据源已恢复，盘中监控继续**",
                f"　{health.reason}",
                DISCLAIMER,
            ]
        )
    return "\n".join(
        [
            "⛔ **分钟线数据源停更，盘中监控已挂起**",
            f"　{health.reason}",
            "　挂起期间不产出任何买卖点信号。循环继续运行，数据恢复后会自动通知。",
            DISCLAIMER,
        ]
    )


# ---- 板块告警 --------------------------------------------------------------

# 与文件顶部的 `SECTOR_CAVEAT` 是两件事：那条讲的是**盘后板块打分**的权重未经
# 回测，这条讲的是**盘中截面**根本不打分。名字必须分开——同名会静默覆盖，
# 而覆盖之后盘前简报底部会印出一句和它无关的免责声明。
INTRADAY_SECTOR_CAVEAT = (
    "> 板块读数按**超额涨幅**（板块成分股涨幅中位数 − 全市场中位数）排序，"
    "只回答「此刻资金在哪」，**不打分、不进选股链路、不预测下一步**。"
    "成交额是双边的，占比高说明注意力集中，不等于净流入。"
)

# 每条板块触发器对应的动作口径。和 `_alert_action_line` 同一个用途：
# 「排名跃升」是读数，「先看这里，再从里面挑票」才是接下来要做的事。
_SECTOR_ACTION = {
    "session_open": "开盘定位：先确认今天的钱在哪几条线上。",
    "rank_surge": "资金正在往这里挪：先看这个方向，再从里面找票，不要直接追已经拉起来的那只。",
    "tone_strong": "整体转强：板块级别的强，不是个别票的强。",
    "limit_up_wave": "涨停潮：情绪最集中的位置，也是最容易买在顶上的位置。",
    "amount_surge": "注意力在集中：成交额占比放大，但这不是净流入。",
    "fade": "领涨板块退潮：手上属于这个方向的仓位需要一个决定，不必等个股跌到止损位。",
    "rank_slide": "排名下滑：这个方向今天已经不是主线。",
}


def sector_alert_title(alerts: list) -> str:
    """标题是通知栏里唯一看得见的那一行，所以点名**具体板块**而不是只报条数。

    「板块异动 3 条」和「有色金属 排名跃升 14→1 等 3 条」决定他要不要现在打开。
    同 `alert_title` 的理由。
    """
    if not alerts:
        return "盘中板块观察"
    action = sum(1 for item in alerts if getattr(item, "level", "") == "ACTION")
    lead = alerts[0]
    name = getattr(lead, "sector", "") or "板块"
    label = getattr(lead, "label", "")
    head = f"{name} {label}" if name != "板块" else label
    if len(alerts) == 1:
        return f"板块异动：{head}"
    suffix = f"（含 {action} 条 ACTION）" if action else ""
    return f"板块异动：{head} 等 {len(alerts)} 条{suffix}"


def render_sector_alerts(alerts: list, snapshot=None) -> str:
    """板块告警卡片。与 `render_alerts` 同为模板渲染，不碰 LLM——理由见
    `docs/ARCHITECTURE.md` §五：告警是时效敏感的，加两秒网络延迟买不到任何东西。
    """
    lines: list[str] = []
    ordered = list(alerts)

    lines.append("## 先看哪里")
    for alert in ordered:
        badge = LEVEL_BADGE.get(getattr(alert, "level", ""), "•")
        trigger = str(getattr(alert, "trigger", ""))
        action = _SECTOR_ACTION.get(trigger, "")
        lines.append(f"- {badge} **{alert.headline}**" + (f"　{action}" if action else ""))

    lines.append("")
    lines.append("## 触发依据")
    for alert in ordered:
        badge = LEVEL_BADGE.get(getattr(alert, "level", ""), "•")
        stamp = f"{alert.as_of:%H:%M}" if getattr(alert, "as_of", None) else "时间未知"
        # 命中你在盯的票的板块标出来。同一条「退潮」，发生在你有仓位的方向上
        # 和发生在一个你没碰过的方向上，紧急程度差一个量级。
        flag = "【命中你盯的票】" if getattr(alert, "touches_watched", False) else ""
        lines.append(f"{badge} **[{alert.level}] {flag}{alert.headline}**　as-of {stamp}")
        lines.extend(f"　{item}" for item in getattr(alert, "detail", []) or [])
        lines.append("")

    if snapshot is not None and getattr(snapshot, "market_median_pct", None) is not None:
        breadth = getattr(snapshot, "breadth_advance", None)
        lines.append(
            f"> 当前全市场：中位涨幅 {snapshot.market_median_pct:+.2f}%"
            + (f"，上涨占比 {breadth:.0%}" if breadth is not None else "")
            + f"，成交额 {snapshot.market_amount_yi:.0f} 亿。"
        )
    lines.append(INTRADAY_SECTOR_CAVEAT)
    lines.append(DISCLAIMER)
    return "\n".join(lines).strip()
