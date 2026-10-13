"""可视化：价位轴与速览表。**没有模型，没有依赖，全部由数字直接画出来。**

研究流程的最后一步是「生成可视化报告」。在终端里，可视化不是画布，是**让相对
位置一眼可见**——现价离止损三格、离买入带一格，这件事读一行数字要在脑子里算，
看一根轴不用。

轴上只画四样：止损、买入带、现价、目标。它们正好是决策卡上会改变行动的那四个数，
多画一个都会让「哪个位置重要」重新变模糊，而那正是要解决的问题。

刻度是线性的，且**只在这几个价之间**取范围——不是当日振幅，也不是全天区间。
理由：这根轴回答的是「离触发还有多远」，不是「今天走了多少」。
"""

from __future__ import annotations

from ashare_decision_support.ask.verdict import SIDE_SELL, Decision

WIDTH = 52

MARK_STOP = "×"
MARK_LAST = "●"
MARK_TARGET = "▲"
MARK_BAND = "▓"


def price_ladder(decision: Decision, width: int = WIDTH) -> list[str]:
    """一只票的价位轴。价位不足两个就不画——两点才有相对位置可言。"""
    points: list[tuple[float, str, str]] = []
    if decision.stop is not None:
        points.append((decision.stop, MARK_STOP, "止损"))
    if decision.last_price is not None:
        points.append((decision.last_price, MARK_LAST, "现价"))
    for index, target in enumerate(decision.targets[:2]):
        points.append((target, MARK_TARGET, "目标" if index == 0 else "目标2"))
    band = (
        (decision.entry_low, decision.entry_high)
        if decision.entry_low is not None and decision.entry_high is not None
        else None
    )
    if band:
        points.append((band[0], MARK_BAND, "买入带"))
        points.append((band[1], MARK_BAND, ""))

    prices = [price for price, _, _ in points]
    if len(set(prices)) < 2:
        return [f"  {decision.symbol}　价位不足两个，画不出价位轴"]

    low, high = min(prices), max(prices)
    span = high - low

    axis = ["─"] * width
    if band:
        start, end = _pos(band[0], low, span, width), _pos(band[1], low, span, width)
        for column in range(min(start, end), max(start, end) + 1):
            axis[column] = MARK_BAND
    for price, mark, _ in points:
        if mark != MARK_BAND:
            axis[_pos(price, low, span, width)] = mark

    labels = [" "] * width
    for price, _, text in points:
        if text:
            _place(labels, _pos(price, low, span, width), text)

    return [
        f"  {_fmt(low)} ├{''.join(axis)}┤ {_fmt(high)}",
        f"  {' ' * len(_fmt(low))}  {''.join(labels).rstrip()}",
    ]


def comparison_table(decisions: list[Decision]) -> str:
    """速览表。列的顺序就是看它们的顺序：先结论，再触发它需要什么。"""
    if not decisions:
        return "（没有可比较的标的）"
    header = "| # | 标的 | 结论 | 现价 | 买入带 | 止损（距离） | 目标 | 盈亏比 | 阻断 |"
    lines = [header, "|---|---|---|---|---|---|---|---|---|"]
    for index, item in enumerate(decisions, start=1):
        band = (
            f"{_fmt(item.entry_low)}–{_fmt(item.entry_high)}"
            if item.entry_low is not None and item.entry_high is not None
            else "—"
        )
        stop = _fmt(item.stop)
        if item.stop is not None and item.stop_distance_pct is not None:
            stop = f"{stop}（{item.stop_distance_pct:.1%}）"
        targets = "/".join(_fmt(level) for level in item.targets[:2]) if item.targets else "—"
        name = f"{item.symbol}<br>{item.name}" if item.name else item.symbol
        lines.append(
            f"| {index} | {name} | **{item.label}** | {_fmt(item.last_price)} | {band} | "
            f"{stop} | {targets} | {item.rrr if item.rrr is not None else '—'} | "
            f"{len(item.blockers) or '—'} |"
        )
    return "\n".join(lines)


def holding_line(decision: Decision) -> str | None:
    """持仓那一行。没有成本价就没有这一行——绝对价位回答不了「我该不该走」。"""
    if decision.side != SIDE_SELL or not decision.holding:
        return None
    holding = decision.holding
    parts = [f"成本 {_fmt(holding.get('成本价'))}"]
    if holding.get("浮动盈亏") is not None:
        parts.append(f"浮动盈亏 {holding['浮动盈亏']:+.2%}")
    if holding.get("持有天数") is not None:
        parts.append(f"持有 {holding['持有天数']} 天")
    if holding.get("生效止损") is not None:
        parts.append(f"生效止损 {_fmt(holding['生效止损'])}（{holding.get('止损来源')}）")
    return "　".join(parts)


def _pos(price: float, low: float, span: float, width: int) -> int:
    if span <= 0:
        return 0
    return max(0, min(width - 1, round((price - low) / span * (width - 1))))


def _place(row: list[str], start: int, text: str) -> None:
    """把标签写在轴位下方。位置被占了就左右找最近的空位。

    **两个标签之间必须留一个空格**：没有这道要求时「现价」和「买入带」会紧挨着
    排成「现价买入带」，读起来是一个词，而它本来是两个位置。宁可丢一个标签也不
    能让两个叠成读不出来的东西——所以找不到空位就直接不画。
    """
    width = len(row)
    for offset in range(width):
        for left in {start + offset, start - offset}:
            if left >= 0 and left + len(text) <= width and _free(row, left, len(text)):
                for index, char in enumerate(text):
                    row[left + index] = char
                return


def _free(row: list[str], left: int, length: int) -> bool:
    """左右各多要一格，那就是标签之间的分隔。"""
    start, end = max(0, left - 1), min(len(row), left + length + 1)
    return all(row[index] == " " for index in range(start, end))


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"
