"""兑现核对任务：把台账里到期的建议对照实际走势结算，并出统计。

样本覆盖需同时满足 200 条记录、60 个交易日和 3 类已知周期，阈值定义在
`journal/stats.py`。统计只提出人工复查项，覆盖达标也不会自动回写权重。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ashare_decision_support.journal import Journal, rolling_validation, summarize, verify_pending
from ashare_decision_support.journal.records import KIND_OUTCOME, VERIFIABLE_KINDS
from ashare_decision_support.journal.stats import (
    MIN_BUCKET,
    MIN_COMPARISON_SESSIONS,
    MODEL_READY_REGIMES,
    MODEL_READY_SAMPLES,
    MODEL_READY_SESSIONS,
)
from ashare_decision_support.notify import ConsoleNotifier
from ashare_decision_support.notify.base import Notifier, publish
from ashare_decision_support.notify.render import DISCLAIMER
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.signals.series import parse_bar_date


def run_review(
    adapter: Any,
    horizon: int = 5,
    journal: Journal | None = None,
    notifiers: list[Notifier] | None = None,
    trade_date: date | None = None,
) -> dict[str, Any]:
    today = trade_date or beijing_today()
    book = journal or Journal()
    channels = notifiers if notifiers is not None else [ConsoleNotifier()]

    result = verify_pending(book, adapter, today=today, horizon=horizon)
    # 结算后只读一份台账快照，让建议分项与兑现使用同一次读取，减少重复 JSONL 解析。
    recorded = book.rows(until=today)
    outcomes = [
        row
        for row in recorded
        if row.get("kind") == KIND_OUTCOME
        and ((resolved := parse_bar_date(row.get("resolved_on"))) is None or resolved <= today)
    ]
    # 建议记录一并传入：评分分项只存在建议上，按判断编号接回兑现记录才能逐项拆解。
    recommendations = [row for row in recorded if row.get("kind") in VERIFIABLE_KINDS]
    stats = summarize(outcomes, recommendations=recommendations)
    rolling = rolling_validation(outcomes)
    content = render_review(today, result, stats, rolling)
    delivered = publish(channels, f"兑现核对 {today}", content)

    return {
        "trade_date": str(today),
        "checked": result["checked"],
        "resolved": result["resolved"],
        "deferred": result.get("deferred", []),
        "total_outcomes": stats["samples"],
        "model_ready": stats["model_ready"],
        "model_ready_gap": stats["model_ready_gap"],
        "readiness": stats["readiness"],
        "rule_feedback": stats["rule_feedback"],
        "rolling_validation": rolling,
        "delivered": delivered,
        "notes": result["notes"],
        "content": content,
    }


def render_review(
    today: date,
    result: dict,
    stats: dict,
    rolling: dict | None = None,
) -> str:
    lines = [
        f"**核对日** {today}　本次结算 {result['resolved']}/{result['checked']} 条",
        "",
        "## 累计样本",
        f"- 已兑现记录 **{stats['samples']}** 条",
    ]
    lines.extend(_readiness_block(stats))
    if stats.get("revision_policy"):
        lines.extend([f"- {stats['revision_policy']}", ""])
    lines.extend(_feedback_block(stats.get("rule_feedback") or []))

    overall = stats["overall"]
    if overall["samples"]:
        lines.append("## 总体")
        lines.append(_stat_line(overall))
        lines.extend(_execution_lines(overall.get("execution") or {}))
        lines.append("")

    lines.extend(_selection_block(stats.get("selection_edge") or {}))
    lines.extend(_veto_block(stats.get("veto_cost") or {}))
    lines.extend(_execution_block(stats))
    lines.extend(_equity_block(stats.get("equity") or {}))
    lines.extend(_component_block(stats.get("by_component") or []))
    lines.extend(_rolling_block(rolling or {}))

    for title, key in (
        ("按策略版本（各版本每日首次判断）", "by_strategy_version"),
        ("入选 vs 对照", "by_group"),
        ("按情绪周期（炸板率）", "by_regime"),
        ("按宏观主题", "by_macro_topic"),
        ("按宏观方向信号", "by_macro_signal"),
        ("按预期差", "by_macro_expectation"),
        # 下面三组此前**算了但从没渲染出来**。分组函数在 `journal/stats.py` 里，
        # 产物一直在 payload 里躺着，只是没人看得到——算而不显示，等于没算。
        ("按打法", "by_playbook"),
        ("按买入位依据", "by_entry_basis"),
        ("按裁决方（规则 / 模型）", "by_decider"),
        # 下面三组是「只出读数、不进打分」那几层的验收位。若某一组的各档之间
        # 没有区分度，那一层就该撤掉而不是留着当装饰。
        ("按周度轮动方向", "by_rotation"),
        ("按全场成交额（存量 / 增量）", "by_turnover"),
        ("按等权与加权是否背离", "by_index_divergence"),
        ("按昨日大单净额方向", "by_large_net"),
        ("按回调深度", "by_pullback"),
        ("按分数分桶", "by_score"),
        # 盈亏比与止损距离是计划发出时就定下的两个数。这两组是「1.5 的执行门槛
        # 该不该收紧」「3 个日波幅的止损上限该不该压低」的实证位，此前完全没有。
        ("按盈亏比分桶", "by_rrr"),
        ("按止损距离（日波幅倍数）", "by_stop_distance"),
        ("按趋势状态", "by_trend"),
        ("按板块", "by_sector"),
    ):
        rows = [row for row in stats.get(key) or [] if row["samples"]]
        if not rows:
            continue
        lines.append(f"## {title}")
        lines.append(
            "| 分组 | 样本 | 交易日 | 先到目标 | 先到止损 | MFE中位 | MAE中位 | 次日收盘中位 | 期末为正 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for row in sorted(rows, key=lambda item: -item["samples"])[:10]:
            flag = "" if row["trusted"] else " ⚠️"
            lines.append(
                f"| {row['bucket']}{flag} | {row['samples']} | {row.get('sessions', 0)} | {_pct(row['target_first_rate'])} | "
                f"{_pct(row['stop_first_rate'])} | {_pct(row['median_mfe_pct'])} | "
                f"{_pct(row['median_mae_pct'])} | {_pct(row.get('median_next_close_pct'))} | "
                f"{_pct(row['positive_close_rate'])} |"
            )
        lines.append("")
        # 小样本照列但标注——把它藏起来只会让人误读那个数字。
        if any(not row["trusted"] for row in rows):
            lines.append(f"> ⚠️ 标记的分组样本数不足 {MIN_BUCKET}，数字仅供观察，不构成判断依据。")
            lines.append("")

    if result["notes"]:
        lines.append("## 说明")
        lines.extend(f"- {note}" for note in result["notes"])
        lines.append("")

    lines.append(DISCLAIMER)
    return "\n".join(line + "\n" if line.startswith("## ") else line for line in lines)


def _readiness_block(stats: dict) -> list[str]:
    detail = (stats.get("readiness") or {}).get("detail") or {}
    lines = []
    for key, label, required in (
        ("samples", "记录数", MODEL_READY_SAMPLES),
        ("sessions", "交易日覆盖", MODEL_READY_SESSIONS),
        ("regimes", "已知情绪周期", MODEL_READY_REGIMES),
    ):
        reading = detail.get(key) or {}
        actual = reading.get("actual", stats.get("samples", 0) if key == "samples" else 0)
        need = reading.get("required", required)
        lines.append(
            f"- {label}：{actual}/{need}，{'已达标' if actual >= need else f'还差 {need - actual}'}"
        )
    lines.extend(
        [
            "- 三项覆盖均达标后仍需独立验证；本报告只提出人工复查项，不自动改写规则。",
            "",
        ]
    )
    return lines


def _feedback_block(feedback: list[dict]) -> list[str]:
    if not feedback:
        return []
    lines = ["## 规则观察与复查"]
    for item in feedback:
        lines.append(f"- **{item['title']}：{item['status']}**。{item['action']}")
        evidence = item.get("evidence") or {}
        gap = item.get("gap", item.get("next_close_gap"))
        if gap is None:
            continue
        label = item.get("gap_label") or "次日差值"
        detail = evidence.get("text") or (
            f"有效样本 {evidence.get('left_samples', 0)}/{evidence.get('right_samples', 0)} 条，"
            f"覆盖 {evidence.get('sessions', 0)} 个共同交易日。"
        )
        lines.append(f"  {label} {_gap_pct(gap)}；{detail}")
    return [*lines, ""]


def _comparison_coverage(reading: dict) -> str:
    evidence = (reading.get("metric_coverage") or {}).get("next_close_gap") or {}
    return (
        f"- 次日比较有效样本 {evidence.get('left_samples', 0)}/{evidence.get('right_samples', 0)} 条，"
        f"共同交易日 {evidence.get('sessions', 0)} 个；来源与策略版本相同，按日等权，期末指标另匹配兑现窗口。"
    )


def _selection_block(edge: dict) -> list[str]:
    """筛选价值：入选组减对照组。

    这是整份报告里唯一直接回答「这套规则有没有用」的一段。单看入选组的绝对
    收益说明不了任何事——普涨的日子里随便选都赚，差值才把市场本身消掉了。
    """
    if not edge.get("available"):
        return [
            "## 筛选价值",
            (
                f"- 暂时算不出：{edge.get('reason', '缺少对照组样本')}。"
                "对照组是每天紧邻落选的那批票，只记不推，随盘前链路自动积累。"
            ),
            "",
        ]

    flag = "" if edge.get("trusted") else " ⚠️"
    execution_gap = (
        [
            (
                f"- 执行口径已实现收益差 **{_gap_pct(edge.get('exec_return_gap'))}**"
                "（同日、同窗口、同模拟版本的已平仓样本；毛收益）"
            )
        ]
        if edge.get("exec_return_gap") is not None
        else []
    )
    return [
        f"## 筛选价值（入选 − 对照）{flag}",
        f"- 入选 {edge['picked_samples']} 条　对照 {edge['control_samples']} 条",
        _comparison_coverage(edge),
        (
            f"- 隔夜跳空差 **{_gap_pct(edge.get('next_open_gap'))}**　"
            f"次日收盘差 **{_gap_pct(edge.get('next_close_gap'))}**"
        ),
        (
            f"- 期末差 **{_gap_pct(edge.get('close_gap'))}**　"
            f"MFE 差 **{_gap_pct(edge.get('mfe_gap'))}**"
        ),
        *execution_gap,
        "- 差值为负表示同日入选组落后于对照组，需结合覆盖范围复查排序。",
        ""
        if edge.get("trusted")
        else (
            f"> ⚠️ 需两组各 {MIN_BUCKET} 条有效次日数据、至少 {MIN_COMPARISON_SESSIONS} 个共同交易日；"
            "当前仅供观察。"
        ),
        "",
    ]


def _veto_block(cost: dict) -> list[str]:
    if not cost.get("available"):
        return ["## 否决代价", f"- 暂时算不出：{cost.get('reason', '缺少同日否决组数据')}。", ""]
    return [
        "## 否决代价（否决 − 入选）",
        _comparison_coverage(cost),
        f"- 次日收盘差 **{_gap_pct(cost.get('next_close_gap'))}**；期末差 **{_gap_pct(cost.get('close_gap'))}**。",
        "- 正值提示被否决组表现更强，仍需核对风险暴露与具体否决原因。",
        "",
    ]


def _rolling_block(rolling: dict) -> list[str]:
    windows = rolling.get("windows") or {}
    available = [item for item in windows.values() if item.get("samples")]
    if not available:
        return []

    lines = ["## 近期验证"]
    for label, window in windows.items():
        if not window.get("samples"):
            continue
        edge = window.get("selection_edge") or {}
        edge_text = "暂无入选/对照差值"
        if edge.get("available"):
            gap = edge.get("next_close_gap")
            edge_text = (
                f"入选−对照次日收盘差 {gap:+.2%}（共同交易日 {edge.get('matched_sessions', 0)} 个）"
                if gap is not None
                else "入选/对照差值暂无可用次日收盘"
            )
        lines.append(
            f"- {label}：{window.get('from')} 至 {window.get('to')}，"
            f"{window.get('samples')} 条 / {window.get('sessions')} 个交易日；{edge_text}"
        )
    lines.append("")
    return lines


def _stat_line(row: dict) -> str:
    return (
        f"- 信号口径：样本 {row['samples']}　交易日 {row.get('sessions', 0)}　"
        f"触及入场价 {_pct(row['entry_rate'])}　"
        f"先到目标 {_pct(row['target_first_rate'])}　先到止损 {_pct(row['stop_first_rate'])}　"
        f"MFE中位 {_pct(row['median_mfe_pct'])}　MAE中位 {_pct(row['median_mae_pct'])}"
    )


def _execution_lines(execution: dict) -> list[str]:
    """总体那一节的第二行：执行口径。

    上一行是从参考收盘起算、不管有没有买到的信号口径。这一行先成交、成交当日
    不可卖、跳空按开盘价——两行并列，读的人才看得见「方向对了但买不到 / 买到了
    但被跳空打掉」这两类差异，它们在任何一行单独看都是不可见的。
    """
    if not execution.get("samples"):
        return []
    flag = "" if execution.get("trusted") else " ⚠️"
    lines = [
        (
            f"- 执行口径{flag}（日线价格模拟，T+1 起可卖）：核对 {execution['samples']} 条　"
            f"可模拟 {execution.get('replayable', execution['samples'])} 条　"
            f"成交 {execution.get('filled', 0)} 条（{_pct(execution.get('fill_rate'))}）　"
            f"已平仓 {execution.get('closed', 0)} 条　"
            f"先到目标 {_pct(execution.get('target_rate'))}　先到止损 {_pct(execution.get('stop_rate'))}　"
            f"到期离场 {_pct(execution.get('time_exit_rate'))}　"
            f"模拟已实现收益中位 {_gap_pct(execution.get('median_return_pct'))}"
            f"（毛收益；固定成本情景扣费后 {_gap_pct(execution.get('median_net_return_pct'))}）　"
            f"为正 {_pct(execution.get('positive_rate'))}"
            f"（扣费后 {_pct(execution.get('net_positive_rate'))}）　"
            f"MAE中位 {_gap_pct(execution.get('median_mae_pct'))}"
        )
    ]
    notes = []
    skipped = execution.get("skipped") or {}
    if skipped:
        notes.append(
            "未成交：" + "、".join(f"{reason} {count} 条" for reason, count in skipped.items())
        )
    if execution.get("locked_exits"):
        notes.append(f"单一跌价阻碍离场 {execution['locked_exits']} 条，按可能封板保守处理")
    if execution.get("open_positions"):
        notes.append(
            f"未平仓估值 {execution.get('marked_positions', 0)} 条，"
            f"浮动盈亏中位 {_gap_pct(execution.get('median_unrealized_return_pct'))}；"
            f"成交后待补数据 {execution.get('unresolved_positions', 0)} 条，均不计入已实现收益"
        )
    if notes:
        lines.append("  " + "；".join(notes))
    return lines


def _execution_block(stats: dict) -> list[str]:
    """执行口径分组表。

    入选/对照/否决回答「排序有没有用」，打法回答「哪条能落地」，情绪周期回答
    「什么行情下别下单」，分数、盈亏比、止损距离三组回答「计划本身的参数该不该动」。
    其余维度看信号口径的分组表就够，再抄一遍只会更长。
    """
    sections = []
    for title, key in (
        ("入选 vs 对照", "by_group"),
        ("按打法", "by_playbook"),
        ("按情绪周期", "by_regime"),
        ("按分数", "by_score"),
        ("按盈亏比", "by_rrr"),
        ("按止损距离", "by_stop_distance"),
        ("按模拟版本", "by_execution_model"),
    ):
        rows = [row for row in stats.get(key) or [] if (row.get("execution") or {}).get("samples")]
        if rows:
            sections.append((title, rows))
    if not sections:
        return []

    lines = [
        "## 执行口径（先成交，T+1 起可卖）",
        (
            "- 回踩按买入区间限价模拟，开盘跌穿下沿则取消；突破须触及触发价且不追过上限。"
            "仅有有效止损和目标的盘前、盘后日线候选可使用开盘价研究情景；"
            "弃权、否决和无效计划不建仓。"
        ),
        (
            "- 成交当日不可卖；跳空按开盘价；单一涨跌价按可能封板保守处理。"
            "未平仓估值与待补数据均不计入已实现收益、胜率和研究曲线。"
        ),
        (
            "- 这是日线价格模拟，未核验盘中止跌、放量确认和成交排队；未计滑点、最低佣金。"
            "旧分时记录缺少参考开盘价时，仍保留收盘近似口径；历史记录不改写。"
        ),
        "",
        "| 分组 | 可模拟 | 成交率 | 平仓 / 未确认平仓 | 先到目标 | 先到止损 | 已实现收益中位 | 扣费后中位 | 为正 | MAE估计中位 | 持有(日) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    flagged = False
    for title, rows in sections:
        for row in sorted(rows, key=lambda item: -item["execution"]["samples"])[:8]:
            execution = row["execution"]
            flag = "" if execution.get("trusted") else " ⚠️"
            flagged = flagged or not execution.get("trusted")
            hold = execution.get("median_hold_sessions")
            lines.append(
                f"| {title}：{row['bucket']}{flag} | {execution.get('replayable', execution['samples'])} | "
                f"{_pct(execution.get('fill_rate'))} | "
                f"{execution.get('closed', 0)} / {execution.get('open_positions', 0)} | "
                f"{_pct(execution.get('target_rate'))} | "
                f"{_pct(execution.get('stop_rate'))} | {_gap_pct(execution.get('median_return_pct'))} | "
                f"{_gap_pct(execution.get('median_net_return_pct'))} | "
                f"{_pct(execution.get('positive_rate'))} | {_gap_pct(execution.get('median_mae_pct'))} | "
                f"{'—' if hold is None else hold} |"
            )
    lines.append("")
    if flagged:
        lines.append(
            f"> ⚠️ 标记的分组有效已平仓样本不足 {MIN_BUCKET}，数字仅供观察，不构成判断依据。"
        )
        lines.append("")
    return lines


def _equity_block(equity: dict) -> list[str]:
    """按成交日归集多日收益的合成研究曲线，不代表逐日资金净值。"""
    picked = equity.get("picked") or {}
    control = equity.get("control") or {}
    if not picked.get("sessions") and not control.get("sessions"):
        return []

    def summary(label: str, curve: dict) -> str:
        if not curve.get("sessions"):
            return f"- {label}：暂无已平仓模拟记录"
        return (
            f"- {label}：{curve['sessions']} 个成交日 {curve.get('trades', 0)} 笔，"
            f"合成累计 **{_gap_pct(curve.get('cumulative_pct'))}**，"
            f"合成曲线回撤 {_gap_pct(curve.get('max_drawdown_pct'))}，"
            f"持有期均值为正的批次 {curve.get('positive_sessions', 0)}/{curve['sessions']}"
        )

    lines = [
        f"## 成交日归集研究曲线（每笔等权，固定成本情景 {equity.get('cost_pct', 0):.2%}）",
        summary("入选", picked),
        summary("对照", control),
    ]
    by_day: dict[str, dict[str, dict]] = {}
    for label, curve in (("picked", picked), ("control", control)):
        for item in curve.get("daily") or []:
            by_day.setdefault(item["date"], {})[label] = item
    recent = sorted(by_day)[-10:]
    if recent:
        lines.append("")
        lines.append("| 成交日 | 入选笔数 | 入选持有期均值 | 对照笔数 | 对照持有期均值 |")
        lines.append("|---|---|---|---|---|")
        for day in recent:
            left = by_day[day].get("picked") or {}
            right = by_day[day].get("control") or {}
            lines.append(
                f"| {day} | {left.get('trades', '—')} | {_gap_pct(left.get('mean_net_return_pct'))} | "
                f"{right.get('trades', '—')} | {_gap_pct(right.get('mean_net_return_pct'))} |"
            )
    lines.append(f"- {equity.get('basis', '')}")
    lines.append("")
    return lines


def _component_block(rows: list[dict]) -> list[str]:
    """分项比较分别展示各收益口径的覆盖，避免少量执行样本被总记录数掩盖。"""
    if not rows:
        return []
    lines = [
        "## 评分分项拆解（高值组 − 低值组，按日配对）",
        (
            "- 按同日、同来源、同策略版本的中位数分组，同分留在一组；中位数等于最高值时，"
            "较低值为低值组。切点显示各可比组的范围，同组取值全相同的记录不参与比较。"
        ),
        (
            "- 每项差值括号内为高/低组有效样本及共同交易日；⚠️ 表示该口径覆盖不足。"
            "判读只使用达标口径。反向是分组关联，可能受其他因子或行业暴露影响，不证明单项因果，不自动改权重。"
        ),
        "",
        "| 分项 | 样本 | 切点范围 | 次日收盘差 | 期末差 | 执行口径已实现差 | 判读 | 判读依据 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    flagged = False
    for row in rows:
        flag = "" if row.get("trusted") else " ⚠️"
        flagged = flagged or not row.get("trusted")
        lines.append(
            f"| {row['component']}{flag} | {row['samples']} | {_component_cut(row)} | "
            f"{_component_metric(row, 'next_close_gap')} | {_component_metric(row, 'close_gap')} | "
            f"{_component_metric(row, 'exec_return_gap')} | {row.get('direction', '')} | "
            f"{row.get('direction_basis', '')} |"
        )
    lines.append("")
    if flagged:
        lines.append(
            f"> ⚠️ 标记的分项两组有效样本不足 {MIN_BUCKET} 条或共同交易日不足 {MIN_COMPARISON_SESSIONS} 个，"
            "数字仅供观察。"
        )
        lines.append("")
    return lines


def _component_cut(row: dict) -> str:
    low, high = row.get("cut_min"), row.get("cut_max")
    if low is None or high is None:
        return "—"
    return f"{low:+.2f}" if low == high else f"{low:+.2f}～{high:+.2f}"


def _component_metric(row: dict, key: str) -> str:
    evidence = (row.get("metric_coverage") or {}).get(key) or {}
    value = row.get(key)
    if value is None:
        return "—"
    flag = "" if evidence.get("trusted") else " ⚠️"
    return (
        f"{_gap_pct(value)}{flag}（{evidence.get('left_samples', 0)}/{evidence.get('right_samples', 0)} 条"
        f" · {evidence.get('sessions', 0)} 日）"
    )


def _pct(value: float | None) -> str:
    """百分比，缺失显示破折号。

    原来这里有一个三分支：`abs(value) < 1 and value < 0` 时走 `:+.1%`，否则走
    `:.1%`。**两条分支的输出逐字相同**——`+` 只给正数补符号，而这个条件只在
    负数上成立，负数本来就带 `-`。看得出想做的是「MFE/MAE 这类收益要带符号，
    进场率这类比率不带」，但那个判据落在了取值范围上，从来没生效过。

    这里收成实际在跑的那一条。要区分收益与比率，得由调用方说明是哪一种，
    不能靠猜数值大小——`entry_rate` 和 `median_mfe_pct` 的取值范围是重叠的。
    """
    return "—" if value is None else f"{value:.1%}"


def _gap_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2%}"
