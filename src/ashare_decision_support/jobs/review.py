"""兑现核对任务：把台账里到期的建议对照实际走势结算，并出统计。

这是「持续学习」四步里的第 2、3 步。第 4 步（标定/上模型）的触发条件写在
`journal/stats.py:MODEL_READY_SAMPLES`——**200 条已兑现记录**，不是"攒了两周"。
在那之前这份统计只报告，不回写任何权重。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ashare_decision_support.journal import Journal, rolling_validation, summarize, verify_pending
from ashare_decision_support.journal.stats import MIN_BUCKET, MODEL_READY_SAMPLES
from ashare_decision_support.notify import ConsoleNotifier
from ashare_decision_support.notify.base import Notifier, publish
from ashare_decision_support.notify.render import DISCLAIMER


def run_review(
    adapter: Any,
    horizon: int = 5,
    journal: Journal | None = None,
    notifiers: list[Notifier] | None = None,
    trade_date: date | None = None,
) -> dict[str, Any]:
    today = trade_date or date.today()
    book = journal or Journal()
    channels = notifiers if notifiers is not None else [ConsoleNotifier()]

    result = verify_pending(book, adapter, today=today, horizon=horizon)
    outcomes = book.outcomes()
    stats = summarize(outcomes)
    rolling = rolling_validation(outcomes)
    content = render_review(today, result, stats, rolling)
    delivered = publish(channels, f"兑现核对 {today}", content)

    return {
        "trade_date": str(today),
        "checked": result["checked"],
        "resolved": result["resolved"],
        "total_outcomes": stats["samples"],
        "model_ready": stats["model_ready"],
        "model_ready_gap": stats["model_ready_gap"],
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
    if not stats["model_ready"]:
        lines.append(
            f"- 距上模型门槛还差 **{stats['model_ready_gap']}** 条"
            f"（门槛 {MODEL_READY_SAMPLES} 条）。在此之前统计只报告、不回写权重——"
            "样本不足时按统计调参，调的是噪声。"
        )
    lines.append("")

    overall = stats["overall"]
    if overall["samples"]:
        lines.append("## 总体")
        lines.append(_stat_line(overall))
        lines.append("")

    lines.extend(_selection_block(stats.get("selection_edge") or {}))
    lines.extend(_rolling_block(rolling or {}))

    for title, key in (
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
    return "\n".join(lines)


def _selection_block(edge: dict) -> list[str]:
    """筛选价值：入选组减对照组。

    这是整份报告里唯一直接回答「这套规则有没有用」的一段。单看入选组的绝对
    收益说明不了任何事——普涨的日子里随便选都赚，差值才把市场本身消掉了。
    """
    if not edge.get("available"):
        return [
            "## 筛选价值",
            (f"- 暂时算不出：{edge.get('reason', '缺少对照组样本')}。"
            "对照组是每天紧邻落选的那批票，只记不推，随盘前链路自动积累。"),
            "",
        ]

    def fmt(value: float | None) -> str:
        return "—" if value is None else f"{value:+.2%}"

    flag = "" if edge.get("trusted") else " ⚠️"
    return [
        f"## 筛选价值（入选 − 对照）{flag}",
        f"- 入选 {edge['picked_samples']} 条　对照 {edge['control_samples']} 条",
        (f"- 隔夜跳空差 **{fmt(edge.get('next_open_gap'))}**　"
        f"次日收盘差 **{fmt(edge.get('next_close_gap'))}**"),
        (f"- 五日期末差 **{fmt(edge.get('close_gap'))}**　"
        f"MFE 差 **{fmt(edge.get('mfe_gap'))}**"),
        "- 差值为负说明这套排序不如紧邻的落选样本——那是要改规则的信号，不是要加样本。",
        "" if edge.get("trusted") else f"> ⚠️ 两组样本各不足 {MIN_BUCKET} 条，差值仅供观察。",
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
                f"入选−对照次日收盘差 {gap:+.2%}"
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
        f"- 样本 {row['samples']}　交易日 {row.get('sessions', 0)}　进场率 {_pct(row['entry_rate'])}　"
        f"先到目标 {_pct(row['target_first_rate'])}　先到止损 {_pct(row['stop_first_rate'])}　"
        f"MFE中位 {_pct(row['median_mfe_pct'])}　MAE中位 {_pct(row['median_mae_pct'])}"
    )


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1%}" if abs(value) < 1 and value < 0 else f"{value:.1%}"
