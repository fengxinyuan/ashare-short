"""兑现核对任务：把台账里到期的建议对照实际走势结算，并出统计。

这是「持续学习」四步里的第 2、3 步。第 4 步（标定/上模型）的触发条件写在
`journal/stats.py:MODEL_READY_SAMPLES`——**200 条已兑现记录**，不是"攒了两周"。
在那之前这份统计只报告，不回写任何权重。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ashare_decision_support.journal import Journal, summarize, verify_pending
from ashare_decision_support.journal.stats import MIN_BUCKET, MODEL_READY_SAMPLES
from ashare_decision_support.notify import ConsoleNotifier
from ashare_decision_support.notify.base import Notifier
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
    stats = summarize(book.outcomes())
    content = render_review(today, result, stats)
    delivered = [c.capabilities.channel for c in channels if c.send(f"兑现核对 {today}", content)]

    return {
        "trade_date": str(today),
        "checked": result["checked"],
        "resolved": result["resolved"],
        "total_outcomes": stats["samples"],
        "model_ready": stats["model_ready"],
        "model_ready_gap": stats["model_ready_gap"],
        "delivered": delivered,
        "notes": result["notes"],
        "content": content,
    }


def render_review(today: date, result: dict, stats: dict) -> str:
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

    for title, key in (
        ("入选 vs 对照", "by_group"),
        ("按情绪周期（炸板率）", "by_regime"),
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
            "| 分组 | 样本 | 先到目标 | 先到止损 | MFE中位 | MAE中位 | 次日收盘中位 | 期末为正 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for row in sorted(rows, key=lambda item: -item["samples"])[:10]:
            flag = "" if row["trusted"] else " ⚠️"
            lines.append(
                f"| {row['bucket']}{flag} | {row['samples']} | {_pct(row['target_first_rate'])} | "
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
            f"- 暂时算不出：{edge.get('reason', '缺少对照组样本')}。"
            "对照组是每天紧邻落选的那批票，只记不推，随盘前链路自动积累。",
            "",
        ]

    def fmt(value: float | None) -> str:
        return "—" if value is None else f"{value:+.2%}"

    flag = "" if edge.get("trusted") else " ⚠️"
    return [
        f"## 筛选价值（入选 − 对照）{flag}",
        f"- 入选 {edge['picked_samples']} 条　对照 {edge['control_samples']} 条",
        f"- 隔夜跳空差 **{fmt(edge.get('next_open_gap'))}**　"
        f"次日收盘差 **{fmt(edge.get('next_close_gap'))}**",
        f"- 五日期末差 **{fmt(edge.get('close_gap'))}**　"
        f"MFE 差 **{fmt(edge.get('mfe_gap'))}**",
        "- 差值为负说明这套排序不如紧邻的落选样本——那是要改规则的信号，不是要加样本。",
        "" if edge.get("trusted") else f"> ⚠️ 两组样本各不足 {MIN_BUCKET} 条，差值仅供观察。",
        "",
    ]


def _stat_line(row: dict) -> str:
    return (
        f"- 样本 {row['samples']}　进场率 {_pct(row['entry_rate'])}　"
        f"先到目标 {_pct(row['target_first_rate'])}　先到止损 {_pct(row['stop_first_rate'])}　"
        f"MFE中位 {_pct(row['median_mfe_pct'])}　MAE中位 {_pct(row['median_mae_pct'])}"
    )


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1%}" if abs(value) < 1 and value < 0 else f"{value:.1%}"
