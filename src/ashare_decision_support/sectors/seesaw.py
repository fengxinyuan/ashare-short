"""板块跷跷板：按共同交易日测量历史负相关，不预设任何板块关系。

两个半窗的相关系数均为负才进入候选；整窗阈值通过 Bonferroni 和 Fisher z
按本轮检验对数收紧。输出包含样本、窗口、半窗系数及阈值，集中在同一板块的关系单独说明。

explain_today 使用同一批序列解释当日背离，不预测未来，也不进入筛选或打分。
统计门槛不能保证样本外稳定；未入榜表示证据不足。研究边界见 docs/ARCHITECTURE.md 的“能力边界”。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations, pairwise
from statistics import NormalDist
from typing import Any

from ashare_decision_support.sectors import panel
from ashare_decision_support.sectors.panel import MIN_SECTORS

# 相关系数的窗口（交易日）。60 日 ≈ 三个月，短到能反映当前这轮行情的结构，
# 长到不被一两周的行情主导。
WINDOW = 60

# 报出去的**下限**。定得比常见的 -0.2 严，但它只是地板——真正生效的阈值由
# `critical_correlation` 按检验了多少对算出来，见那个函数。
NEGATIVE_THRESHOLD = -0.35

# 多重比较的族错误率。0.05 是 Bonferroni 的惯例值：**整轮 4005 对里出现哪怕一个
# 假阳性的概率**压到 5%，而不是「每一对各自 5%」——后者在 4005 对上等于保证会有
# 两百个假阳性。
FAMILY_ERROR_RATE = 0.05

# 半窗最少要有多少个收益点才算得出一个系数。半窗是 30 天，低于 20 说明这个板块
# 在这半段里停更过，那一半的系数不可比。
MIN_HALF_POINTS = 20

# 榜上最多列出的关系数。
TOP_PAIRS = 12

LABEL_CONSISTENT = "与历史关系一致"
LABEL_UNKNOWN = "两者历史上没有稳定关系"


@dataclass(frozen=True)
class SeesawPair:
    """一对实测反向的板块。**两个半窗的系数一起带出来**，这是它可信度的全部依据。"""

    left: str
    right: str
    correlation: float
    first_half: float
    second_half: float
    points: int

    def describe(self) -> str:
        return (
            f"{self.left} ↔ {self.right}：近 {self.points} 日相关 {self.correlation:+.2f}"
            f"（前半段 {self.first_half:+.2f} / 后半段 {self.second_half:+.2f}）"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "correlation": self.correlation,
            "first_half": self.first_half,
            "second_half": self.second_half,
            "points": self.points,
        }


@dataclass
class SeesawRead:
    """跷跷板读数。

    `trusted` 为假时 `pairs` 一定是空的，但 `notes` 会说明为什么——同
    `RotationRead` / `TurnoverMigration`：**「没有跷跷板」和「算不出来」是两件事**，
    前者是结论，后者是数据问题，而它们在简报里必须长得不一样。
    """

    trade_date: str = ""
    window: int = WINDOW
    sectors: int = 0
    pairs_tested: int = 0
    # 本轮真正生效的阈值。**必须跟着读数一起印出去**：它随检验对数变，
    # 不写出来的话，同一份榜在不同天的严格程度是看不出来的。
    threshold: float = NEGATIVE_THRESHOLD
    trusted: bool = False
    pairs: list[SeesawPair] = field(default_factory=list)
    # 最新一个交易日各板块的日收益。**留在读数里而不是让调用方另取一份**：
    # 「今天涨跌多少」与「近 60 日相关多少」必须来自同一批序列、同一个交易日，
    # 否则 `explain_today` 那句话的两半可能落在不同的日子上。
    latest_returns: dict[str, float] = field(default_factory=dict)
    observations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "window": self.window,
            "sectors": self.sectors,
            "pairs_tested": self.pairs_tested,
            "threshold": self.threshold,
            "trusted": self.trusted,
            "pairs": [item.to_dict() for item in self.pairs],
            # 只带榜上那几个板块的今日收益。全部 81 个进 facts 会把上下文挤爆，
            # 而榜外板块的今日涨跌在别处（板块排序）已经有了。
            "latest_returns": {
                name: value
                for name, value in sorted(self.latest_returns.items())
                if any(name in {item.left, item.right} for item in self.pairs)
            },
            "observations": list(self.observations),
            "notes": list(self.notes),
        }

    def correlation_of(self, left: str, right: str) -> SeesawPair | None:
        """查一对具体板块。**只在榜上找**，榜外返回 None。

        「不在榜上」的含义是「没有通过那两道闸门」，而不是「相关系数为 0」——
        `explain_divergence` 因此把它说成「历史上没有稳定关系」，不说成「无关」。
        """
        wanted = {left, right}
        return next((item for item in self.pairs if {item.left, item.right} == wanted), None)


def critical_correlation(pairs_tested: int, points: int) -> float:
    """检验这么多对、每对这么多个点时，负相关要多强才不像是碰巧。

    **这是这一层最重要的一个数**，也是它与「随便算个相关系数矩阵」的全部区别。

    一个固定阈值在这里是无效的：`|r| ≥ 0.35`、60 个点、单对的双尾 p ≈ 0.006，
    看着挺显著；乘上 4005 对，期望假阳性是 **24 个**。而这一层报出去的榜通常
    只有十几行——也就是说固定阈值下，整张榜都可能是噪声，且每一行都带着漂亮的
    两个半窗系数。**实测验证过**：231 对纯高斯噪声（互不相关的构造序列）里，
    按 -0.35 筛出了一对 r=-0.357、两个半窗同为负的「跷跷板」。

    做法是 Bonferroni：把族错误率摊到每一对上（`α / n`），再把这个 p 换算成
    r 的临界值。换算走 Fisher z 变换——`arctanh(r) * sqrt(points-3)` 近似标准
    正态，这是相关系数上的标准做法，且只用得到 `statistics.NormalDist`，不引
    scipy。

    两条边界：

    - 取 `min(地板, 临界值)`，所以对数很少时不会反而放宽到 -0.1；
    - `points ≤ 3` 时 z 变换没有定义，直接退回地板。

    **它压不掉的东西要说清楚**：Bonferroni 控的是「有没有假阳性」，不是「报出来
    的关系明年还成不成立」。后者要样本外验证，那要等台账攒够，不是一个算子能给的。
    """
    if pairs_tested <= 0 or points <= 3:
        return NEGATIVE_THRESHOLD
    # 双尾里只用负的那一侧：这一层只报反向对。正相关对在 A 股上多到没有信息量
    # ——同一个大盘 beta 会让绝大多数板块两两正相关，出榜只是把 beta 重报一遍。
    alpha = FAMILY_ERROR_RATE / (2.0 * pairs_tested)
    z = NormalDist().inv_cdf(max(alpha, 1e-12))
    return min(NEGATIVE_THRESHOLD, math.tanh(z / math.sqrt(points - 3)))


def read_seesaw(
    series: dict[str, tuple[str, list[dict]]],
    window: int = WINDOW,
    top_pairs: int = TOP_PAIRS,
) -> SeesawRead:
    """板块指数序列 → 跷跷板读数。纯计算，不碰网络。

    `series` 与 `rotation.read_rotation` / `turnover.read_turnover` 同一个入参
    （`ths_sector_index.load_all_series()` 的产物），对齐也走同一个
    `panel.align`——三层看的必须是同一张面板，否则「轮动说 A 强、跷跷板说 A 和 B
    反着走」这两句话可能落在不同的交易日上。
    """
    read = SeesawRead(window=window)
    # 收益点比 bar 少一个，所以要多要一根。
    result = panel.align(series, min_bars=window + 1)
    if not result:
        read.observations.append("没有可用的板块指数序列，本次无跷跷板读数。")
        return read

    read.trade_date = result.latest
    read.sectors = len(result.rows)
    if result.dropped:
        read.notes.append(
            f"跷跷板：{result.dropped} 个板块最新交易日不是 {result.latest}，已排除"
            "（收益序列错位会让相关系数变成两段不同区间的比较）"
        )
    if result.total > len(result.rows):
        read.notes.append(
            f"跷跷板：{len(result.rows)}/{result.total} 个板块序列足够长（需 {window + 1} 根）"
        )

    if len(result.rows) < MIN_SECTORS:
        read.notes.append(
            f"仅 {len(result.rows)} 个板块有足够长的序列（需 {MIN_SECTORS} 个），"
            "相关系数在这个样本量上不可信，本次不给跷跷板结论"
        )
        read.observations.append(f"板块样本仅 {len(result.rows)} 个，不足以支撑相关性判断。")
        return read

    returns = {
        name: rets
        for _code, name, bars in result.rows
        if len(rets := _returns(bars, window)) >= MIN_HALF_POINTS * 2
    }
    if len(returns) < MIN_SECTORS:
        read.notes.append(f"只有 {len(returns)} 个板块能算出足够长的收益序列，本次不给跷跷板结论")
        return read

    # 检验对数包含提前淘汰的组合，避免阈值随筛选结果改变。
    measured: list[SeesawPair] = []
    tested = 0
    for left, right in combinations(sorted(returns), 2):
        tested += 1
        pair = _measure(left, right, returns[left], returns[right])
        if pair is not None:
            measured.append(pair)

    read.pairs_tested = tested
    read.trusted = True
    points = min((item.points for item in measured), default=window)
    threshold = critical_correlation(tested, points)
    read.threshold = round(threshold, 4)
    found = [item for item in measured if item.correlation <= threshold]
    # 最负的排前面。并列时按板块名，保证两次运行的输出可 diff。
    found.sort(key=lambda item: (item.correlation, item.left, item.right))
    read.pairs = found[: max(top_pairs, 0)]
    read.latest_returns = {
        name: round(rets[read.trade_date], 6)
        for name, rets in returns.items()
        if read.trade_date in rets
    }
    read.notes.append(
        f"跷跷板：{tested} 对全部检验过，阈值按 Bonferroni 收到 {threshold:+.2f}"
        f"（族错误率 {FAMILY_ERROR_RATE:.0%}，{points} 个收益点），{len(found)} 对通过"
        f"「两个半窗同为负 + 整窗 ≤ 阈值」。固定阈值在这个对数上无效："
        f"按 {NEGATIVE_THRESHOLD:+.2f} 筛，光是噪声就该出约 "
        f"{_expected_false_positives(tested, points):.0f} 个假阳性。"
    )
    read.observations.extend(_observations(read))
    return read


def _expected_false_positives(pairs_tested: int, points: int) -> float:
    """全是随机序列时，按地板阈值筛会筛出多少对。用来给上面那句话一个具体的数。"""
    if points <= 3:
        return 0.0
    z = math.atanh(NEGATIVE_THRESHOLD) * math.sqrt(points - 3)
    return pairs_tested * NormalDist().cdf(z)


def explain_today(read: SeesawRead) -> list[str]:
    """榜上哪几对**今天真的背着走了**，以及那符不符合它们的历史关系。

    这是 `explain_divergence` 在主链路上的入口。只解释**榜上**的对：一份把 3240
    对今天的分歧全列一遍的简报没人会读，而榜上那几对已经过了两道闸门。

    收益取自这一层自己算的那批日收益（`SeesawRead.latest_returns`），不从别处
    再取一份——「今天涨跌多少」和「近 60 日相关多少」必须来自同一批序列、同一个
    交易日，否则这句话的两半可能落在不同的日子上。
    """
    lines: list[str] = []
    for pair in read.pairs:
        line = explain_divergence(
            read,
            pair.left,
            pair.right,
            read.latest_returns.get(pair.left),
            read.latest_returns.get(pair.right),
        )
        if line is not None:
            lines.append(line)
    return lines


def explain_divergence(
    read: SeesawRead,
    left: str,
    right: str,
    left_return: float | None,
    right_return: float | None,
) -> str | None:
    """今天这两个板块背着走，和它们的历史关系对不对得上。

    **这是这一层唯一的用途。** 它不说「所以明天会反过来」——一句「与历史关系
    一致」的价值在于告诉你今天的分歧是结构性的还是偶然的，仅此而已。

    两个都涨或都跌时返回 None：没有分歧要解释。收益缺失同理。

    **只分得开两种情况**，因为 `read` 里只有通过闸门的反向对：这一对历史上反着走
    （一致），或者没有稳定的反向关系（未知）。「今天背着走、历史上却是同向」那一类
    落在后者里——要把它单独认出来，得让 `read_seesaw` 同时保留正相关对，那是另一个
    读数，不是这个函数少写了一个分支。
    """
    if left_return is None or right_return is None:
        return None
    if left_return * right_return >= 0.0:
        return None

    pair = read.correlation_of(left, right)
    if pair is None:
        verdict = LABEL_UNKNOWN
        detail = f"近 {read.window} 日未通过跷跷板闸门"
    else:
        verdict = LABEL_CONSISTENT
        detail = (
            f"近 {pair.points} 日相关 {pair.correlation:+.2f}"
            f"（前半段 {pair.first_half:+.2f} / 后半段 {pair.second_half:+.2f}）"
        )
    return (
        f"{left} {left_return:+.2%}、{right} {right_return:+.2%}——{verdict}"
        f"（{detail}）。这是对今天的陈述，不含对明天的判断。"
    )


def _returns(bars: list[dict], window: int) -> dict[str, float]:
    """近 `window` 根日收益，**按交易日建索引**。

    索引不是装饰，是这一层唯一区别于其余两层的对齐要求。`panel.align` 保证的是
    「最后一根是同一天」以及「序列足够长」——它**不保证中间那些天对得齐**：一个
    停牌三天的板块，清洗掉那三根之后第 60 根往前跨的是 63 个自然交易日。

    这对 `rotation.py` 无所谓，它只回看第 N 根算一个涨幅；对相关系数是致命的，
    因为相关系数吃的是**逐日配对**的两条序列。按下标 zip 会把 A 的周三配到 B 的
    周一，而算出来的数看不出任何异常——它只是一个偏向 0 的、错的系数。

    收益的日期取**后一天**（`close[t]/close[t-1]-1` 记在 t 上）。两个板块只有在
    同一对相邻交易日上都有报价，这个收益才可比，所以基期不连续时那一天直接没有
    收益，而不是拿更早的收盘价顶上去。
    """
    tail = bars[-(window + 1) :]
    rets: dict[str, float] = {}
    for previous, current in pairwise(tail):
        # `align` 已经把取不到 close 的整根丢掉，所以这里的值必定有限且为正。
        rets[str(current["date"])] = float(current["close"]) / float(previous["close"]) - 1.0
    return rets


def _measure(left: str, right: str, a: dict[str, float], b: dict[str, float]) -> SeesawPair | None:
    """按共同交易日对齐，仅两个半窗均为负时计算整窗相关。"""
    days = sorted(a.keys() & b.keys())
    size = len(days)
    if size < MIN_HALF_POINTS * 2:
        return None
    left_series = [a[day] for day in days]
    right_series = [b[day] for day in days]
    half = size // 2
    first = panel.pearson(left_series[:half], right_series[:half])
    if first is None or first >= 0.0:
        return None
    second = panel.pearson(left_series[half:], right_series[half:])
    if second is None or second >= 0.0:
        return None
    whole = panel.pearson(left_series, right_series)
    if whole is None:
        return None
    return SeesawPair(
        left=left,
        right=right,
        correlation=round(whole, 4),
        first_half=round(first, 4),
        second_half=round(second, 4),
        points=size,
    )


def _observations(read: SeesawRead) -> list[str]:
    """给简报的人话。**没有跷跷板本身就是一条结论**，所以空榜也要说话。"""
    if not read.pairs:
        return [
            (
                f"近 {read.window} 日 {read.pairs_tested} 对板块里，没有一对通过跷跷板闸门"
                "（两个半窗都为负）——这段行情里板块之间是同涨同跌的 beta 关系，"
                "指望靠板块对冲抹平方向风险不成立。"
            )
        ]
    lines = [
        f"近 {read.window} 日实测反向的板块 {len(read.pairs)} 对（共检验 "
        f"{read.pairs_tested} 对）：" + "；".join(item.describe() for item in read.pairs[:3])
    ]
    hub, count = _hub(read.pairs)
    if hub is not None:
        lines.append(
            f"**这 {len(read.pairs)} 对不是 {len(read.pairs)} 个独立发现**："
            f"其中 {count} 对都含「{hub}」，说明这段行情是「{hub} 对其余板块」的"
            "一对多结构，不是多组各自成立的跷跷板。当成多个独立信号读会把同一件事"
            "数很多遍。"
        )
    lines.append(
        "**只用来解释今天的分歧，不用来预测。** 相关系数会随窗口漂移，"
        "两个半窗的系数已随读数一起列出，不一致的对已经被挡掉。"
    )
    return lines


# 一个板块出现在这么多比例的对里，就按「一对多」而不是「多对多」报。
# 取 1/2 而不是更高：4 对里占 2 对已经足以让人把同一件事读成两件。
HUB_SHARE = 0.5


def _hub(pairs: list[SeesawPair]) -> tuple[str | None, int]:
    """榜上有没有一个板块占了多数对。

    这条不是统计检验，是**读法提示**。2026-08-10 实测 12 对里 11 对含「银行」——
    也就是「银行 vs 整个电子链」一件事，被列成了 11 行。11 行读起来像 11 条证据，
    而它们全部来自同一个价格序列，一起对一起错。
    """
    if not pairs:
        return None, 0
    counts: dict[str, int] = {}
    for item in pairs:
        for name in (item.left, item.right):
            counts[name] = counts.get(name, 0) + 1
    # 并列时取名字靠前的，保证输出可 diff。
    name, count = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return (name, count) if count >= len(pairs) * HUB_SHARE else (None, 0)
