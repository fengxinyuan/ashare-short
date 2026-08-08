"""板块轮动：从「今天谁强」到「资金正在从哪里挪到哪里」。

`sectors/momentum.py` + `sectors/predict.py` 给的是**当日截面**——今天哪个板块强。
`sectors/history.py` 给的是**系统自己排名的记忆**——我给出的前五名稳不稳。
这一层给的是第三样东西，前两样都答不了：**用板块指数的真实价格序列，回答过去
一周到一个月里资金的移动方向**。

三者的区别值得说清楚，否则很容易重复实现：

| | 输入 | 从哪天开始有答案 |
|---|---|---|
| `momentum.py` | 今天的成分股日线 | 当天 |
| `history.py` | 系统每天自己的排名 | **系统连续跑 N 天之后** |
| 本模块 | 同花顺板块指数序列 | **第一次跑就有**（`last.js` 一次回当年 140 根） |

最后一列是本模块存在的主要理由：轮动规律是一个关于**过去一个月**的问题，而一个
今天才上线的记忆层要到下个月才能回答它。价格序列是现成的，落盘代码也已经在跑
（`adapters/ths_sector_index.py:_persist_history`），只是从来没有人读回来算过。

## 三个读数

**1. 相对强弱排名与排名变化。** 板块相对全体板块中位数的超额收益 → 排名。
**排名变化比排名本身更有用**：排在第 3 名说明它已经强了，从第 40 名升到第 12 名
说明**资金正在往这里挪**。后者才是「轮动到哪了」。

**2. 高低切换度量。** 这是那句「资金从高估值板块流向低估值板块」的可测版本。
做法：算「前期涨幅」与「近期涨幅」在**横截面上**的秩相关。

```text
相关系数明显为负  →  前期涨得多的最近在跌、前期跌的在涨  =  高低切换
相关系数明显为正  →  前期强的继续强                      =  强者恒强
接近 0            →  两者没关系，说不出轮动方向
```

**这是测量，不是预设。** 「该高低切换了」是一个每天都有人说的判断，本模块不假设
它成立——它把这个月实际发生的是切换还是延续算出来，包括算出「与流行说法相反」
这个结论。同一条纪律写在 `docs/ROADMAP.md` §5.2 的跷跷板那一段里：不预设关系，
由数据说。

**3. 主线持续度。** 近 N 个交易日每天涨幅前 5，相邻两天的重合度取平均。
接近 1 = 主线明确，跟随有意义；接近 0 = 无主线的快速轮动，超短最容易两头挨打。

## 不做的事

- **不给权重、不进打分。** 和五档、同花顺指数、量价形态完全一样的处置：先进简报
  与台账，等 `review` 能回答「排名上升最快的板块里选出来的票兑现率高多少」，
  再谈要不要给它分。
- **不预测。** 「明天该轮到低位板块了」不写。本模块的全部输出都是对**已经发生的
  事**的陈述句。
- **不跨源映射。** 同花顺 90 个板块 ≈ 申万二级粒度，不往申万一级 31 个上硬映
  （`ths_sector_index.py` 开头已论证过）。这一层的板块名就是同花顺的板块名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from ashare_decision_support.sectors import panel

# 窗口。5 日 = 一周，20 日 = 一个月，与「过去一周到一个月」这个提法对齐。
SHORT_WINDOW = 5
LONG_WINDOW = 20

# 排名变化回看多少个交易日。用 5 日：比这更短，排名变化基本是噪声；更长就不再
# 是「正在挪」而是「已经挪完了」。
RANK_LOOKBACK = 5

# 参与排序所需的最少序列长度。要同时算 20 日涨幅和 5 日前的 20 日涨幅，
# 所以至少 LONG_WINDOW + RANK_LOOKBACK + 1 根。
MIN_BARS = LONG_WINDOW + RANK_LOOKBACK + 1

# 低于这个板块数就不给结论。横截面统计在小样本上毫无意义——20 个板块算出的
# 秩相关，随便两个板块换位就能翻号。
MIN_SECTORS = 20

# 高低切换的判定分界。秩相关落在 [-0.2, 0.2] 之间时**不给方向**：那个区间里
# 的相关系数说明不了任何事，而给一个标签会让「测不出关系」读起来像「没有轮动」。
SWITCH_STRONG = 0.2

# 领涨榜取前几名，用于主线持续度。
LEADER_COUNT = 5

# 主线持续度的分档，只用于措辞。
PERSISTENCE_HIGH = 0.6
PERSISTENCE_LOW = 0.25

LABEL_SWITCH = "高低切换"
LABEL_MOMENTUM = "强者恒强"
LABEL_UNCLEAR = "无明显轮动方向"
LABEL_INSUFFICIENT = "样本不足"


@dataclass(frozen=True)
class SectorTrend:
    """一个板块在轮动视角下的读数。"""

    code: str
    name: str
    ret_5d: float | None = None
    ret_20d: float | None = None
    # 「前期」涨幅：20 个交易日前到 5 个交易日前，**不含最近 5 日**。
    #
    # 这个字段存在的全部理由是它与 `ret_5d` 不重叠。用 `ret_20d` 当前期涨幅是
    # 一个不会报错的错误：20 日窗口**包含**最近 5 日，于是本周暴涨的板块会自动
    # 显得「前期已经涨高了」，给高低切换的相关系数注入一个纯机械的正偏——
    # 实测同一份无规律数据，用重叠窗口算出 +0.27（误判为强者恒强），
    # 用不重叠窗口算出 -0.02（正确地判为无方向）。
    ret_prior: float | None = None
    # 相对全体板块中位数的超额收益。绝对涨幅答不了「跑赢没有」——普涨日里
    # +2% 是落后，阴跌日里 -0.5% 是强势。
    excess_5d: float | None = None
    excess_20d: float | None = None
    excess_prior: float | None = None
    rank_now: int | None = None
    rank_prev: int | None = None

    @property
    def rank_change(self) -> int | None:
        """排名上升为正。第 40 名升到第 12 名 = +28。"""
        if self.rank_now is None or self.rank_prev is None:
            return None
        return self.rank_prev - self.rank_now

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "ret_5d": self.ret_5d,
            "ret_20d": self.ret_20d,
            "ret_prior": self.ret_prior,
            "excess_5d": self.excess_5d,
            "excess_20d": self.excess_20d,
            "excess_prior": self.excess_prior,
            "rank_now": self.rank_now,
            "rank_prev": self.rank_prev,
            "rank_change": self.rank_change,
        }


@dataclass
class RotationRead:
    """一次轮动读数。`trusted=False` 时所有结论都不该被引用。"""

    trade_date: str = ""
    sectors: int = 0
    trusted: bool = False
    switch_correlation: float | None = None
    switch_label: str = LABEL_INSUFFICIENT
    # 相邻等长窗口的秩相关：上一个 short_window 与这一个 short_window。
    #
    # 与 `switch_correlation` 是两个问题，不是同一个问题的两种算法：
    #
    # - `switch_correlation` 比的是**前 15 个交易日**与**近 5 日**，长窗口对短窗口，
    #   回答「一个月级别的强弱有没有反转」；
    # - 这个比的是**相邻两个 5 日**，等长对等长，回答「上周的强弱这周有没有掉个头」。
    #
    # 两者经常反号，而那不是矛盾：月线还在跌的板块，完全可以在最近一周变成最强。
    # 对超短来说后者更贴近决策周期，所以它必须单独出一个数，不能被长窗口那个盖住。
    # 它也是唯一与 `rank_change` 同口径的相关系数——排名变化本来就用这两个窗口算，
    # 此前却从没算过它们的相关性，于是「谁在涨」有数、「是不是在切换」没数。
    adjacent_correlation: float | None = None
    adjacent_label: str = LABEL_INSUFFICIENT
    persistence: float | None = None
    persistence_days: int = 0
    trends: list[SectorTrend] = field(default_factory=list)
    leaders: tuple[str, ...] = ()
    recurring_leaders: tuple[str, ...] = ()
    observations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.trusted and bool(self.trends)

    def rising(self, n: int = 5) -> list[SectorTrend]:
        """排名上升最快的板块——「资金正在往哪里挪」。"""
        scored = [item for item in self.trends if item.rank_change is not None]
        return sorted(scored, key=lambda item: item.rank_change or 0, reverse=True)[:n]

    def falling(self, n: int = 5) -> list[SectorTrend]:
        scored = [item for item in self.trends if item.rank_change is not None]
        return sorted(scored, key=lambda item: item.rank_change or 0)[:n]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "sectors": self.sectors,
            "trusted": self.trusted,
            "switch_correlation": self.switch_correlation,
            "switch_label": self.switch_label,
            "adjacent_correlation": self.adjacent_correlation,
            "adjacent_label": self.adjacent_label,
            "persistence": self.persistence,
            "persistence_days": self.persistence_days,
            "leaders": list(self.leaders),
            "recurring_leaders": list(self.recurring_leaders),
            "rising": [item.to_dict() for item in self.rising()],
            "falling": [item.to_dict() for item in self.falling()],
            "observations": list(self.observations),
        }


def read_rotation(
    series: dict[str, tuple[str, list[dict]]],
    short_window: int = SHORT_WINDOW,
    long_window: int = LONG_WINDOW,
    rank_lookback: int = RANK_LOOKBACK,
) -> RotationRead:
    """板块指数序列 → 轮动读数。纯计算，不碰网络。

    `series` 形如 `{code: (板块名, 日线)}`，即
    `adapters/ths_sector_index.load_all_series()` 的产物。
    """
    read = RotationRead()
    aligned, notes = _align(series, min_bars=long_window + rank_lookback + 1)
    read.notes.extend(notes)
    if not aligned:
        read.observations.append("没有可用的板块指数序列，本次无轮动读数。")
        return read

    read.trade_date = str(aligned[0][2][-1]["date"])
    read.sectors = len(aligned)
    if len(aligned) < MIN_SECTORS:
        read.notes.append(
            f"仅 {len(aligned)} 个板块有足够长的序列（需 {MIN_SECTORS} 个），"
            "横截面统计在这个样本量上不可信，本次不给轮动结论"
        )
        read.observations.append(
            f"板块样本仅 {len(aligned)} 个，不足以支撑轮动判断，本次只按当天截面看。"
        )
        return read

    # ---- 涨幅与超额 ------------------------------------------------------
    rows: list[tuple[str, str, float | None, float | None, float | None, float | None]] = []
    for code, name, bars in aligned:
        closes = panel.closes(bars)
        rows.append(
            (
                code,
                name,
                panel.pct(closes, short_window),
                panel.pct(closes, long_window),
                # 前期区间：long_window 前 → short_window 前。与近期不重叠。
                _pct_between(closes, long_window, short_window),
                # 5 个交易日前站在那天回看的 5 日涨幅，用于算排名变化。
                panel.pct(closes[: len(closes) - rank_lookback], short_window),
            )
        )

    median_5d = _median_of([item[2] for item in rows])
    median_20d = _median_of([item[3] for item in rows])
    median_prior = _median_of([item[4] for item in rows])
    median_prev = _median_of([item[5] for item in rows])

    now_pairs = [(item[0], _excess(item[2], median_5d)) for item in rows]
    prev_pairs = [(item[0], _excess(item[5], median_prev)) for item in rows]
    rank_now = _rank_map(now_pairs)
    rank_prev = _rank_map(prev_pairs)

    read.trends = [
        SectorTrend(
            code=code,
            name=name,
            ret_5d=panel.rounded(ret_5d),
            ret_20d=panel.rounded(ret_20d),
            ret_prior=panel.rounded(ret_prior),
            excess_5d=panel.rounded(_excess(ret_5d, median_5d)),
            excess_20d=panel.rounded(_excess(ret_20d, median_20d)),
            excess_prior=panel.rounded(_excess(ret_prior, median_prior)),
            rank_now=rank_now.get(code),
            rank_prev=rank_prev.get(code),
        )
        for code, name, ret_5d, ret_20d, ret_prior, _prev in rows
    ]
    read.trusted = True

    # ---- 高低切换 --------------------------------------------------------
    # 前期用 `excess_prior`（不含最近 5 日）而不是 `excess_20d`：两个窗口重叠
    # 会让「本周涨得猛」同时抬高「前期涨得高」，把相关系数机械地推向正号，
    # 也就是**系统性地把高低切换误判成强者恒强**。见 `SectorTrend.ret_prior`。
    paired = [
        (item.excess_prior, item.excess_5d)
        for item in read.trends
        if item.excess_prior is not None and item.excess_5d is not None
    ]
    correlation = _spearman([a for a, _ in paired], [b for _, b in paired])
    read.switch_correlation = panel.rounded(correlation, 4)
    read.switch_label = _switch_label(correlation)

    # ---- 周度切换：相邻等长窗口 -------------------------------------------
    # 复用算排名用的那两份超额收益，所以这个相关系数与 `rank_change` 严格同口径：
    # 排名变化说「谁从第 90 名冲到第 7 名」，它说「这种对调是整体现象还是个例」。
    # 少了它，简报里就只有一堆个别板块的名次剧变，没有一个数能回答「这周到底是
    # 普遍性的高低切换，还是几个板块自己在动」。
    adjacent_paired = [
        (prev, now)
        for (_, prev), (_, now) in zip(prev_pairs, now_pairs, strict=True)
        if prev is not None and now is not None
    ]
    adjacent = _spearman(
        [a for a, _ in adjacent_paired], [b for _, b in adjacent_paired]
    )
    read.adjacent_correlation = panel.rounded(adjacent, 4)
    read.adjacent_label = _switch_label(adjacent)

    # ---- 主线持续度 ------------------------------------------------------
    persistence, days, leaders, recurring = _leadership(aligned, rank_lookback)
    read.persistence = panel.rounded(persistence, 4)
    read.persistence_days = days
    read.leaders = leaders
    read.recurring_leaders = recurring

    read.observations = _observations(read, long_window, short_window, rank_lookback)
    return read


# ---- internals -----------------------------------------------------------


def _align(
    series: dict[str, tuple[str, list[dict]]], min_bars: int
) -> tuple[list[tuple[str, str, list[dict]]], list[str]]:
    """对齐 + 把丢弃的原因写成本层的话。判据在 `sectors/panel.py:align`。

    对齐是必须的：一个停更了三天的板块，它的「5 日涨幅」跨的是别人 8 天的区间，
    排进同一张榜就是在拿不同区间的读数比名次。同 `ths_sector_index.fetch` 里
    对当日读数做的那道闸门。
    """
    notes: list[str] = []
    result = panel.align(series, min_bars)
    if not result:
        return [], notes
    if result.dropped:
        notes.append(
            f"板块轮动：{result.dropped} 个板块最新交易日不是 {result.latest}，已排除"
            "（不同区间的涨幅不可比）"
        )
    if result.total > len(result.rows):
        notes.append(
            f"板块轮动：{len(result.rows)}/{result.total} 个板块序列足够长（需 {min_bars} 根）"
        )
    return result.rows, notes


def _leadership(
    aligned: list[tuple[str, str, list[dict]]], days: int
) -> tuple[float | None, int, tuple[str, ...], tuple[str, ...]]:
    """主线持续度：近 `days` 天，相邻两天涨幅前 5 的重合度均值。

    用相邻两天的重合度取平均，而不是全部天数的**交集**：交集在真实数据上几乎
    永远是 0（5 天里天天都在前五的板块极少），而一个恒为 0 的指标区分不出
    「主线明确」和「完全无序」——两种盘面它都报 0。
    """
    if days < 2:
        return None, 0, (), ()

    tops: list[list[str]] = []
    for offset in range(days - 1, -1, -1):
        index = -1 - offset
        scored: list[tuple[str, float]] = []
        for _code, name, bars in aligned:
            if len(bars) < abs(index) + 1:
                continue
            previous = panel.bar_value(bars[index - 1], "close")
            current = panel.bar_value(bars[index], "close")
            if previous is None or current is None or previous <= 0:
                continue
            scored.append((name, current / previous - 1.0))
        if not scored:
            continue
        scored.sort(key=lambda pair: pair[1], reverse=True)
        tops.append([name for name, _ in scored[:LEADER_COUNT]])

    if len(tops) < 2:
        return None, len(tops), tuple(tops[-1]) if tops else (), ()

    overlaps = [
        len(set(tops[i]) & set(tops[i - 1])) / float(LEADER_COUNT)
        for i in range(1, len(tops))
    ]
    counts: dict[str, int] = {}
    for names in tops:
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    threshold = max(2, (len(tops) + 1) // 2)
    recurring = tuple(
        name
        for name, count in sorted(counts.items(), key=lambda pair: pair[1], reverse=True)
        if count >= threshold
    )
    return sum(overlaps) / len(overlaps), len(tops), tuple(tops[-1]), recurring


def _correlation_line(detail: str, label: str, gloss: dict[str, str]) -> str:
    """把一个相关系数写成一句话。两个窗口共用，措辞由 `gloss` 给。"""
    return f"{detail}——{gloss.get(label, gloss[LABEL_UNCLEAR])}"


_LONG_GLOSS = {
    LABEL_SWITCH: "前期涨得多的近期在回落，属于**高低切换**。",
    LABEL_MOMENTUM: "前期强的近期继续强，属于**强者恒强**，尚未切换。",
    LABEL_UNCLEAR: "两者没有稳定关系，说不出轮动方向。",
}

_ADJACENT_GLOSS = {
    LABEL_SWITCH: "上周强的这周在回落、上周弱的这周在涨，**周度层面正在高低切换**。",
    LABEL_MOMENTUM: "上周强的这周继续强，周度层面尚未切换。",
    LABEL_UNCLEAR: "上下两周没有稳定关系，周度说不出方向。",
}


def _persistence_verdict(persistence: float) -> str:
    if persistence >= PERSISTENCE_HIGH:
        return "主线明确"
    if persistence <= PERSISTENCE_LOW:
        return "无主线的快速轮动，追涨最容易两头挨打"
    return "主线一般"


def _rank_line(prefix: str, items: list[SectorTrend]) -> str:
    return prefix + "、".join(
        f"{item.name}（{item.rank_prev}→{item.rank_now}，{item.rank_change:+d}）"
        for item in items
    )


def _observations(
    read: RotationRead, long_window: int, short_window: int, rank_lookback: int
) -> list[str]:
    """把读数写成陈述句。**全部是对已发生事实的描述，不含任何对明天的推断。**"""
    lines: list[str] = []

    if read.switch_correlation is not None:
        lines.append(
            _correlation_line(
                f"前期涨幅（{long_window}→{short_window} 个交易日前）与近 {short_window} 日"
                f"涨幅的横截面秩相关 {read.switch_correlation:+.2f}（{read.sectors} 个板块）",
                read.switch_label,
                _LONG_GLOSS,
            )
        )

    if read.adjacent_correlation is not None:
        lines.append(
            _correlation_line(
                f"上一个 {short_window} 日与这一个 {short_window} 日的横截面秩相关 "
                f"{read.adjacent_correlation:+.2f}（等长窗口，与排名变化同口径）",
                read.adjacent_label,
                _ADJACENT_GLOSS,
            )
        )

    if read.persistence is not None:
        lines.append(
            f"近 {read.persistence_days} 个交易日领涨前 {LEADER_COUNT} 的相邻重合度 "
            f"{read.persistence:.0%}，{_persistence_verdict(read.persistence)}。"
        )
    if read.recurring_leaders:
        lines.append(
            f"反复进入领涨前 {LEADER_COUNT} 的方向："
            + "、".join(read.recurring_leaders[:6])
        )

    rising = read.rising(5)
    if rising:
        lines.append(
            _rank_line(
                f"相对强弱排名（近 {short_window} 日 vs 其前 {rank_lookback} 日）上升最快：",
                rising,
            )
        )
    falling = read.falling(5)
    if falling:
        lines.append(_rank_line("排名下滑最快：", falling))

    # 两个读数用的是**不同的对比窗口**，所以它们可以同时成立而看起来矛盾：
    # 切换标签比的是「前 15 个交易日 vs 近 5 日」，排名变化比的是「前 5 日 vs
    # 近 5 日」。实测 2026-08-03：半导体前期 -22.8% 之后再跌 -12.8%（弱者恒弱，
    # 支持 +0.45 的相关），但它在 -10→-5 那段反弹到第 6 名，于是排名读数是 6→89。
    #
    # 不加这一行的话，简报里「强者恒强」会紧挨着「半导体 6→90」出现，读的人只能
    # 二选一地相信其中一个——而正确的读法是两个都对，它们回答的不是同一个问题。
    #
    # 有了 `adjacent_correlation` 之后这句话才真正有支撑：两个窗口各自的相关系数
    # 都算出来了，谁正谁负是量出来的，不再需要读的人自己去猜。实测同一天长窗口
    # +0.45（月度弱者恒弱）、短窗口 -0.47（周度高低切换），一正一负。
    if read.switch_label != read.adjacent_label and read.adjacent_correlation is not None:
        lines.append(
            f"两个窗口给出的方向不同——长窗口（前 {long_window - short_window} 个交易日"
            f"对近 {short_window} 日）是「{read.switch_label}」，"
            f"等长窗口（相邻两个 {short_window} 日）是「{read.adjacent_label}」。"
            "长窗口说这一个月的趋势有没有反转，等长窗口说这一周资金有没有掉头，"
            "**超短按后者，仓位按前者**。"
        )
    elif rising and read.switch_label in (LABEL_MOMENTUM, LABEL_SWITCH):
        lines.append(
            f"注意两行的对比窗口不同：切换标签比的是前 {long_window - short_window} 个"
            f"交易日与近 {short_window} 日，排名变化比的是相邻两个 {short_window} 日窗口。"
            "长窗口说趋势有没有反转，短窗口说这周谁在领涨，两者不一致是常态而非冲突。"
        )
    return lines


def _switch_label(correlation: float | None) -> str:
    if correlation is None:
        return LABEL_INSUFFICIENT
    if correlation <= -SWITCH_STRONG:
        return LABEL_SWITCH
    if correlation >= SWITCH_STRONG:
        return LABEL_MOMENTUM
    return LABEL_UNCLEAR


def _spearman(left: list[float], right: list[float]) -> float | None:
    """秩相关。用秩而不是原值，因为板块涨幅的尾部很重——2026 实测单板块
    20 日涨幅极值可以是中位数的十几倍，皮尔逊相关会被那一两个板块支配。"""
    if len(left) != len(right) or len(left) < MIN_SECTORS:
        return None
    ranks_left = _ranks(left)
    ranks_right = _ranks(right)
    n = float(len(left))
    mean_left = sum(ranks_left) / n
    mean_right = sum(ranks_right) / n
    cov = sum((a - mean_left) * (b - mean_right) for a, b in zip(ranks_left, ranks_right, strict=True))
    var_left = sum((a - mean_left) ** 2 for a in ranks_left)
    var_right = sum((b - mean_right) ** 2 for b in ranks_right)
    if var_left <= 0 or var_right <= 0:
        return None
    return cov / (var_left**0.5 * var_right**0.5)


def _ranks(values: list[float]) -> list[float]:
    """平均秩，并列取均值。并列不处理的话，一堆同值会按输入顺序拿到不同的秩，
    让相关系数带上一点与数据无关的信息。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        average = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[order[position]] = average
        index = end + 1
    return ranks


def _rank_map(pairs: list[tuple[str, float | None]]) -> dict[str, int]:
    """超额收益 → 名次，1 是最强。取不到读数的不给名次。"""
    scored = [(code, value) for code, value in pairs if value is not None]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return {code: index for index, (code, _value) in enumerate(scored, start=1)}


def _pct_between(closes: list[float], start: int, end: int) -> float | None:
    """`start` 个交易日前 → `end` 个交易日前的涨幅。要求 start > end ≥ 1。"""
    if start <= end or len(closes) < start + 1:
        return None
    previous = closes[-(start + 1)]
    current = closes[-(end + 1)]
    if previous <= 0:
        return None
    return current / previous - 1.0


def _excess(value: float | None, benchmark: float | None) -> float | None:
    if value is None or benchmark is None:
        return None
    return value - benchmark


def _median_of(values: list[float | None]) -> float | None:
    usable = [value for value in values if value is not None]
    return median(usable) if usable else None
