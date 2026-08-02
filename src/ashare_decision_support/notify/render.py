"""Markdown rendering for the daily briefs.

Kept separate from the notifiers so every channel delivers identical content and
the rendering stays unit-testable without any IO.
"""

from __future__ import annotations

from datetime import date

from ashare_decision_support.models.schemas import (
    DailyTechnicalSnapshot,
    DecisionMatrix,
)
from ashare_decision_support.scoring import ScoreResult

DISCLAIMER = "> 本报告由规则引擎自动生成，仅供研究参考，不构成投资建议；系统不下单、不接管账户。"

SECTOR_CAVEAT = (
    "> 板块打分融合量价动量、关键词舆情与龙虎榜资金三项读数，权重为人工设定、**未经回测验证**，"
    "只用于决定「先看哪里」，不构成方向预测。"
)

ADVICE_CAVEAT = (
    "> 买卖点由日线结构与分时结构规则推导，**未经回测验证**；分钟线为免费源，"
    "盘中时效不保证，收盘后取到的是最后一个交易日的完整数据。"
)


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def render_post_close_brief(
    matrix: DecisionMatrix,
    results: list[ScoreResult],
    snapshots: dict[str, DailyTechnicalSnapshot],
    data_notes: list[str] | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"**交易日** {matrix.trade_date}　**策略版本** {matrix.strategy_version}")
    lines.append(
        f"**市场状态** {matrix.market_regime}　"
        f"**仓位系数** {matrix.position_band_factor:.2f}"
    )
    if matrix.sector_whitelist:
        lines.append(f"**主攻板块** {'、'.join(matrix.sector_whitelist)}")
    if matrix.sector_hedge:
        lines.append(f"**对冲板块** {'、'.join(matrix.sector_hedge)}")
    lines.append("")

    lines.append("## 次日候选")
    if not results:
        lines.append("今日无标的通过筛选。")
    else:
        lines.append("| # | 标的 | 评分 | 现价 | 趋势 | 止损参考 | 目标参考 |")
        lines.append("|---|---|---|---|---|---|---|")
        for index, result in enumerate(results, start=1):
            snapshot = snapshots.get(result.symbol)
            lines.append(
                f"| {index} | {result.symbol} | {result.score:.1f} | "
                f"{_fmt(snapshot.last_close) if snapshot else '—'} | "
                f"{snapshot.trend_state.value if snapshot else '—'} | "
                f"{_fmt(snapshot.stop_reference) if snapshot else '—'} | "
                f"{_fmt(snapshot.target_reference) if snapshot else '—'} |"
            )
        lines.append("")
        for result in results:
            lines.append(f"### {result.symbol}（{result.score:.1f}）")
            if result.reasons:
                lines.append("- 依据：" + "；".join(result.reasons))
            if result.warnings:
                lines.append("- ⚠️ 风险：" + "；".join(result.warnings))
            snapshot = snapshots.get(result.symbol)
            if snapshot:
                lines.append(f"- 结构：{snapshot.explanation}")
            lines.append("")

    if data_notes:
        lines.append("## 数据边界")
        lines.extend(f"- {note}" for note in data_notes)
        lines.append("")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


def render_sector_brief(
    trade_date: date,
    outlooks: list,
    candidates: list[dict],
    market,
    position_factor: float,
    data_notes: list[str] | None = None,
    market_outlook=None,
) -> str:
    """Top-down pre-open card: market first, then sectors, then the names."""
    lines: list[str] = []
    lines.append(f"**交易日** {trade_date}　**仓位建议系数** {position_factor:.2f}")
    lines.append("")

    if market_outlook is not None:
        lines.append("## 大盘趋势")
        if not getattr(market_outlook, "trusted", True):
            # "没测出来" 和 "测出来是中性" 必须长得不一样。
            lines.append(f"**{market_outlook.direction}** — 覆盖率不足，本次不给方向判断。")
        else:
            lines.append(
                f"**{market_outlook.direction}**（综合读数 {market_outlook.score:.1f}／100，"
                f"{market_outlook.sectors_trusted} 个达标板块 "
                f"{market_outlook.constituents_covered} 只成分股）"
            )
        for reason in getattr(market_outlook, "reasons", []):
            lines.append(f"- {reason}")
        for warning in getattr(market_outlook, "warnings", []):
            lines.append(f"- ⚠️ {warning}")
        lines.append("")

    lines.append("## 板块排序")
    if not outlooks:
        lines.append("无可用板块读数。")
    else:
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
                lines.append("- 依据：" + "；".join(item.reasons))
            if item.warnings:
                lines.append("- ⚠️ " + "；".join(item.warnings))
            if item.headlines:
                lines.append("- 相关消息：" + "　/　".join(item.headlines))
            lines.append("")

    lines.append("## 板块内候选")
    if not candidates:
        lines.append("入选板块中无标的通过筛选（ST/公告风险过滤后为空）。")
    else:
        lines.append("| 标的 | 所属板块 | 评分 | 现价 | 趋势 | 止损参考 | 目标参考 |")
        lines.append("|---|---|---|---|---|---|---|")
        for item in candidates:
            lines.append(
                f"| {item['symbol']} | {item['sector']} | {item['score']:.1f} | "
                f"{_fmt(item.get('last_close'))} | {item.get('trend_state') or '—'} | "
                f"{_fmt(item.get('stop_reference'))} | {_fmt(item.get('target_reference'))} |"
            )
        lines.append("")
        for item in candidates:
            lines.append(f"- **{item['symbol']}**（{item['sector']}　{item['score']:.1f}）")
            if item.get("reasons"):
                lines.append("  - 依据：" + "；".join(item["reasons"]))
            if item.get("warnings"):
                lines.append("  - ⚠️ " + "；".join(item["warnings"]))
        lines.append("")

    lines.append("## 仓位建议")
    # MarketOutlook carries two distinct reason lists — one for the direction,
    # one for the position factor. Prefer the factor's, or this section repeats
    # the direction rationale under a heading that promises sizing.
    sentiment = getattr(market, "sentiment", None)
    reasons = getattr(sentiment, "reasons", None) or getattr(market, "reasons", [])
    for reason in reasons or []:
        lines.append(f"- {reason}")
    lines.append("")

    if data_notes:
        lines.append("## 数据边界")
        lines.extend(f"- {note}" for note in data_notes)
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


def render_advice_brief(trade_date, advices: list, data_notes: list[str] | None = None) -> str:
    """Per-symbol levels and forward scenarios."""
    lines: list[str] = []
    lines.append(f"**查询日** {trade_date}　**标的数** {len(advices)}")
    lines.append("")

    stale = [item for item in advices if item.session_date and not item.is_current_session]
    if stale:
        lines.append(
            f"> ⚠️ 以下标的的分钟数据为 {stale[0].session_date}，**非当日盘中**："
            f"{'、'.join(item.symbol for item in stale)}。价位按该日收盘结构给出。"
        )
        lines.append("")

    lines.append("## 价位一览")
    lines.append("| 标的 | 名称 | 现价 | 买入区间 | 止损 | 目标 | 盈亏比 | 趋势 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for item in advices:
        entry = (
            f"{item.entry_low:.2f}-{item.entry_high:.2f}"
            if item.entry_low is not None and item.entry_high is not None
            else "—"
        )
        targets = "/".join(f"{level:.2f}" for level in item.targets) if item.targets else "—"
        lines.append(
            f"| {item.symbol} | {item.name or '—'} | {_fmt(item.last_price)} | {entry} | "
            f"{_fmt(item.stop)} | {targets} | "
            f"{item.rrr if item.rrr is not None else '—'} | {item.trend_state or '—'} |"
        )
    lines.append("")

    for item in advices:
        header = f"### {item.symbol}"
        if item.name:
            header += f" {item.name}"
        if item.score is not None:
            header += f"（评分 {item.score:.1f}）"
        lines.append(header)

        if item.reasons:
            lines.append("- 依据：" + "；".join(item.reasons))
        if item.scenarios:
            lines.append("- 后续情景：")
            for scenario in item.scenarios:
                lines.append(f"  - **{scenario.name}**：{scenario.trigger} → {scenario.implication}")
        if item.warnings:
            for warning in item.warnings:
                lines.append(f"- ⚠️ {warning}")
        lines.append("")

    if data_notes:
        lines.append("## 数据边界")
        lines.extend(f"- {note}" for note in data_notes)
        lines.append("")

    lines.append(advice_mandatory_block())
    return "\n".join(lines)


def advice_mandatory_block() -> str:
    """Discipline text that must reach the reader on every channel."""
    return "\n".join(
        [
            "## 纪律提醒",
            "- 价位是**结构位**，不是预测；触发前不动，触发后按纪律执行，不临场改止损。",
            "- 情景之间没有概率，本系统**没有拟合模型**，给不出「多大概率上涨」这类数字。",
            "- 单笔亏损控制在总资金可承受范围内；盈亏比不足 1.5 的机会直接放弃。",
            "",
            ADVICE_CAVEAT,
            DISCLAIMER,
        ]
    )


def render_pre_open_brief(
    matrix: DecisionMatrix,
    snapshots: dict[str, DailyTechnicalSnapshot],
    data_notes: list[str] | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"**对应交易日** {matrix.trade_date}　**策略版本** {matrix.strategy_version}")
    lines.append(
        f"**市场状态** {matrix.market_regime}　"
        f"**仓位系数** {matrix.position_band_factor:.2f}"
    )
    lines.append("")

    lines.append("## 执行提示")
    if not matrix.candidates:
        lines.append("昨日无候选，今日空仓观望。")
    else:
        lines.append("| 标的 | 路由 | 仓位带 | 止损(bps) | 最低盈亏比 | 止损价参考 | 目标价参考 |")
        lines.append("|---|---|---|---|---|---|---|")
        for candidate in matrix.candidates:
            snapshot = snapshots.get(candidate.symbol)
            band = f"{candidate.min_position_pct * 100:.1f}%-{candidate.max_position_pct * 100:.1f}%"
            lines.append(
                f"| {candidate.symbol} | {candidate.route.value} | {band} | "
                f"{candidate.stop_bps} | {candidate.rrr_min:.1f} | "
                f"{_fmt(snapshot.stop_reference) if snapshot else '—'} | "
                f"{_fmt(snapshot.target_reference) if snapshot else '—'} |"
            )
        lines.append("")
        for candidate in matrix.candidates:
            score = f"（评分 {candidate.research_score:.1f}）" if candidate.research_score is not None else ""
            lines.append(f"- **{candidate.symbol}**{score}：{candidate.thesis}")
            if candidate.warnings:
                lines.append(f"  - ⚠️ {'；'.join(candidate.warnings)}")
        lines.append("")

    lines.append("## 纪律提醒")
    lines.append("- 集合竞价一旦要参与，委托必须在 **09:24:58 之前**发出；09:25 撮合完成后再下单只能算开盘单。")
    lines.append("- 09:20-09:25 期间**不接受撤单**，报价前先确认仓位与价格。")
    lines.append(f"- 单日总仓位不超过市场状态给定的仓位系数 {matrix.position_band_factor:.2f}。")
    lines.append("")

    if data_notes:
        lines.append("## 数据边界")
        lines.extend(f"- {note}" for note in data_notes)
        lines.append("")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


# ---- intraday alerts -----------------------------------------------------

LEVEL_BADGE = {"ACTION": "🔴", "WARN": "🟠", "INFO": "🔵"}


def render_alerts(alerts: list, health) -> str:
    """Compact alert card. Deliberately template-only — see docs/AUTOMATION.md §六.

    Every alert carries the as-of timestamp of the bar it was computed from.
    Without it a push is indistinguishable from one built on yesterday's close,
    which is the single most dangerous thing an automated channel can send.
    """
    lines: list[str] = []
    for alert in alerts:
        badge = LEVEL_BADGE.get(alert.level, "•")
        stamp = f"{alert.as_of:%H:%M}" if alert.as_of else "时间未知"
        origin = "" if alert.watchlist else "（候选池）"
        lines.append(f"{badge} **[{alert.level}] {alert.headline}**{origin}　as-of {stamp}")
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
