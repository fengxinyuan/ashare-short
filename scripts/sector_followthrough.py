"""板块跟随度：「上周最强的板块，下周还强吗」——在这套数据上到底成不成立。

**为什么要这个脚本。** `sectors/rotation.py` 的 docstring 写着「不预测」，它的输出
全部是对已发生事实的陈述句。这条纪律是对的，但它留下一个空档：读简报的人拿到
「高低切换 -0.82」之后，**下一步一定会问「所以呢」**，而系统对这个问题一个字都
没有。于是那个数会被凭直觉外推，而直觉在这里是错的——

    -0.82 描述的是「刚刚发生了什么」（上周那次暴力切换），
    不是「接下来倾向于发生什么」。

拿它推出「所以该买跌得多的」，方向是反的：2026-08-07 实测，同一份数据上无条件
基准是**弱动量**（前 5 日强势的板块，未来 5 日相对全场中位 +0.30%、胜率 55%；
最弱的那一档是 -0.35%）。

**所以这个脚本不给预测，它给基准率。** 「历史上处在同样状态时，后来发生了什么」
是一个可以查的事实，而它正是把 `rotation` 那些陈述句接到决策上所缺的一环。

## 三个输出

1. **无条件基准**——追强 vs 抄底，在整段样本上各自的前向超额与胜率；
2. **按切换状态条件化**——今天落在哪一档，那一档历史上后来怎么走；
3. **最相似的历史交易日**——切换系数最接近今天的那几天，及其后 5 日的板块级明细。
   第 3 项是三项里最该看的：均值会把「扛住的有色」和「崩掉的电子链」平均成一个
   没有含义的数，而这两组的区别恰恰是决策要的。

## 口径（三条都影响结论，改之前先读）

- **前向收益取相对全场中位的超额**，不取绝对收益。否则大盘整体涨跌会被算成板块
  选择的功劳，一轮普涨里随便选都「胜率很高」。
- **前期窗口与近期窗口不重叠**（前期 = 20→5 个交易日前，近期 = 近 5 日），与
  `sectors/rotation.py:SectorTrend.ret_prior` 同一条理由：重叠会让本周暴涨的板块
  自动显得「前期已经涨高」，把相关系数机械地推向正号。
- **滚动取样，窗口互相重叠**。所以样本数不是独立事件数——相邻交易日属于同一轮
  行情。脚本会把独立事件的粗略个数一并印出来，胜率不能当概率读。

## 不做的事

- **不给建议、不给权重、不产出订单。** 输出是基准率，不是「所以买 X」。
- **不做参数搜索。** 窗口取 5/20 是因为 `rotation.py` 就是这么定义的，换一组数字
  再挑最好看的那组，得到的是过拟合，不是发现。

    python3 scripts/sector_followthrough.py
    python3 scripts/sector_followthrough.py --window 10 --analogues 8
"""

from __future__ import annotations

import argparse
import itertools
import statistics as st
from typing import Any

from ashare_decision_support.adapters.ths_sector_index import load_all_series
from ashare_decision_support.sectors import panel
from ashare_decision_support.sectors.rotation import LONG_WINDOW, SHORT_WINDOW, SWITCH_STRONG

# 分档边界直接用 `rotation.py` 的 SWITCH_STRONG，避免这里出现第二套「算强切换」
# 的判据——两处一旦漂开，简报说的「高低切换」和这里统计的就不是同一件事。
DECILE = 10
MIN_SECTORS = 50
DEFAULT_ANALOGUES = 6


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for position, index in enumerate(order):
        ranks[index] = float(position)
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 8:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = st.fmean(rx), st.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def _pct(series: list[float], end: int, window: int) -> float | None:
    """`end` 时点回看 `window` 根的涨跌幅。基期非正时返回 None，同 `panel.pct`。"""
    start = end - window
    if start < 0 or end >= len(series) or series[start] <= 0:
        return None
    return series[end] / series[start] - 1.0


def _between(series: list[float], end: int, older: int, newer: int) -> float | None:
    """`older` 根前 → `newer` 根前的涨跌幅。与近期窗口不重叠的「前期涨幅」。"""
    a, b = end - older, end - newer
    if a < 0 or b >= len(series) or series[a] <= 0:
        return None
    return series[b] / series[a] - 1.0


def _load() -> tuple[list[str], list[list[float]], list[str]]:
    aligned = panel.align(load_all_series(), LONG_WINDOW + SHORT_WINDOW + 1, keys=("close",))
    if not aligned:
        raise SystemExit(
            "没有可用的板块指数序列。先跑一次 `ashare-ds morning`（或任何会调用 "
            "ThsSectorIndexAdapter.fetch 的命令）把日线落盘。"
        )
    names = [name for _, name, _ in aligned.rows]
    closes = [panel.closes(bars) for _, _, bars in aligned.rows]
    dates = [str(bar["date"]) for bar in aligned.rows[0][2]]
    depth = min(len(series) for series in closes)
    return names, [series[-depth:] for series in closes], dates[-depth:]


def _samples(
    closes: list[list[float]], dates: list[str], short: int, long: int
) -> list[dict[str, Any]]:
    """每个交易日一条：当天的切换系数，以及强/弱两档的前向超额。"""
    out: list[dict[str, Any]] = []
    for i in range(long, len(dates) - short):
        rows = []
        for series in closes:
            recent = _pct(series, i, short)
            prior = _between(series, i, long, short)
            forward = _pct(series, i + short, short)
            if None not in (recent, prior, forward):
                rows.append((prior, recent, forward))
        if len(rows) < MIN_SECTORS:
            continue
        switch = _spearman([r[0] for r in rows], [r[1] for r in rows])
        if switch is None:
            continue
        median = st.median([r[2] for r in rows])
        ranked = sorted(rows, key=lambda r: -r[1])
        k = max(1, len(rows) // DECILE)
        out.append(
            {
                "index": i,
                "date": dates[i],
                "switch": switch,
                "top": st.fmean([r[2] for r in ranked[:k]]) - median,
                "bottom": st.fmean([r[2] for r in ranked[-k:]]) - median,
                "median": median,
            }
        )
    return out


def _episodes(indices: list[int]) -> int:
    """把相邻交易日并成一轮行情，给出独立事件的粗略个数。

    不并的话「n=10」读起来像 10 次独立验证，而它可能只是两轮行情各持续五天。

    间隔在**交易日序号**上算，不在日期上算：`20260731` 到 `20260803` 是相邻的两个
    交易日，按 `YYYYMMDD` 相减却是 72，会把同一轮行情拆成两轮。
    """
    if not indices:
        return 0
    ordered = sorted(indices)
    return 1 + sum(1 for a, b in itertools.pairwise(ordered) if b - a > 3)


def _describe(label: str, group: list[dict[str, Any]], episodes: bool = True) -> str:
    """一档的前向表现。

    `episodes=False` 用于全样本：那是一段连续交易日，按「间隔 >3 天算换一轮」去数
    必然得 1，印出来会被误读成「整段数据只有一次独立验证」。
    """
    if not group:
        return f"{label:<24} 无样本"
    top = [g["top"] for g in group]
    bottom = [g["bottom"] for g in group]
    hit = sum(1 for value in top if value > 0) / len(top)
    rounds = f" (约 {_episodes([g['index'] for g in group])} 轮)" if episodes else ""
    return (
        f"{label:<24} n={len(group):>3}{rounds}"
        f"  强势档 {st.fmean(top) * 100:+6.2f}% 胜率 {hit:>4.0%}"
        f"  |  弱势档 {st.fmean(bottom) * 100:+6.2f}%"
        f"  |  全场中位 {st.fmean([g['median'] for g in group]) * 100:+6.2f}%"
    )


def _analogue_detail(
    names: list[str],
    closes: list[list[float]],
    dates: list[str],
    index: int,
    short: int,
    top_n: int = 6,
) -> list[str]:
    """某一天的领涨板块，以及它们之后 `short` 日各自走成了什么样。

    均值会把「扛住的」和「崩掉的」平均掉，而这两组的区别正是决策要的，所以这里
    逐个板块印出来。
    """
    rows = []
    for name, series in zip(names, closes, strict=True):
        past = _pct(series, index, short)
        forward = _pct(series, index + short, short)
        if past is not None and forward is not None:
            rows.append((name, past, forward))
    rows.sort(key=lambda r: -r[1])
    lines = []
    for name, past, forward in rows[:top_n]:
        mark = "✅" if forward > 0 else "❌"
        lines.append(f"      {name:<12} 前 {short} 日 {past:+7.2%}  →  后 {short} 日 {forward:+7.2%} {mark}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="板块跟随度基准率，不给预测")
    parser.add_argument("--window", type=int, default=SHORT_WINDOW, help="近期/前向窗口，默认 5")
    parser.add_argument("--long", type=int, default=LONG_WINDOW, help="前期窗口末端，默认 20")
    parser.add_argument(
        "--analogues", type=int, default=DEFAULT_ANALOGUES, help="打印几个最相似的历史交易日"
    )
    args = parser.parse_args()

    names, closes, dates = _load()
    samples = _samples(closes, dates, args.window, args.long)
    if not samples:
        raise SystemExit("序列太短，攒不出一个样本。")

    print(f"{len(names)} 个板块 × {len(dates)} 根日线　{dates[0]} ~ {dates[-1]}")
    print(f"窗口：近期/前向 {args.window} 日，前期 {args.long}→{args.window} 日前（不重叠）")
    # 全样本是一段连续交易日，按「间隔 >3 天算换一轮」去数必然得 1，那个数没有含义。
    # 连续样本上能说的是**有效独立观测数**：窗口每 `window` 天才换一批不重叠的前向
    # 收益，所以 n/window 才是「这段数据到底提供了几次互不重复的验证」。
    print(
        f"样本 {len(samples)} 个交易日（滚动，窗口重叠）"
        f"，不重叠的独立观测约 {len(samples) // args.window} 个"
    )
    print()

    print("== 无条件基准（前向收益均为相对全场中位的超额）==")
    print(_describe("全样本", samples, episodes=False))
    correlations = [
        _spearman(
            [s["switch"] for s in samples],
            [s["top"] for s in samples],
        )
    ]
    print(f"{'切换系数与强势档前向超额的秩相关':<24} {correlations[0]:+.3f}"
          if correlations[0] is not None else "")
    print()

    print("== 按上周的切换状态条件化 ==")
    print(_describe(f"强高低切换 (≤-{SWITCH_STRONG * 2:.1f})",
                    [s for s in samples if s["switch"] <= -SWITCH_STRONG * 2]))
    print(_describe("无明显方向",
                    [s for s in samples if abs(s["switch"]) < SWITCH_STRONG * 2]))
    print(_describe(f"强者恒强 (≥{SWITCH_STRONG * 2:.1f})",
                    [s for s in samples if s["switch"] >= SWITCH_STRONG * 2]))
    print()

    latest = samples[-1] if samples[-1]["date"] == dates[-1 - args.window] else None
    current = _current_switch(closes, args.window, args.long)
    if current is not None:
        print(f"== 当前状态（{dates[-1]}）：切换系数 {current:+.2f} ==")
        print(f"最相似的 {args.analogues} 个历史交易日，及其后 {args.window} 日：")
        ranked = sorted(samples, key=lambda s: abs(s["switch"] - current))[: args.analogues]
        for sample in ranked:
            print(
                f"  {sample['date']}  切换 {sample['switch']:+.2f}"
                f"  强势档 {sample['top'] * 100:+6.2f}%"
                f"  弱势档 {sample['bottom'] * 100:+6.2f}%"
                f"  全场中位 {sample['median'] * 100:+6.2f}%"
            )
        closest = ranked[0]
        print()
        print(f"  最接近的一天 {closest['date']} 的领涨板块，逐个看后 {args.window} 日：")
        for line in _analogue_detail(names, closes, dates, closest["index"], args.window):
            print(line)
    if latest is None:
        print()
        print("注：最后一个样本的前向窗口尚未走完，当前状态只报切换系数，不报前向结果。")

    print()
    print("胜率不是概率：样本按交易日滚动、窗口互相重叠，相邻日期属同一轮行情。")
    print("本脚本只给基准率，不给建议，也不产出订单。")


def _current_switch(closes: list[list[float]], short: int, long: int) -> float | None:
    """最新一个交易日的切换系数，口径与 `rotation.read_rotation` 一致。"""
    end = len(closes[0]) - 1
    rows = []
    for series in closes:
        recent = _pct(series, end, short)
        prior = _between(series, end, long, short)
        if recent is not None and prior is not None:
            rows.append((prior, recent))
    if len(rows) < MIN_SECTORS:
        return None
    return _spearman([r[0] for r in rows], [r[1] for r in rows])


if __name__ == "__main__":
    main()
