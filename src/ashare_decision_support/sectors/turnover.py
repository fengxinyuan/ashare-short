"""板块成交额迁移：钱在往哪个板块挪，以及**它挪过去是在买还是在跑**。

`rotation.py` 用**价格**回答「谁在涨」，这一层用**成交额**回答「谁在被交易」。
两者经常不一致，而不一致的地方通常就是要点：一个板块可以一边跌一边成交额暴涨
（有人在夺路而逃），也可以一边涨一边成交额萎缩（没人愿意卖，但也没有新钱进来）。

## 为什么不叫「资金流」

因为它不是。`signals/volume_price.py` 开头那条纪律在这里同样成立，而且更该强调：
**成交额是双边的**，一笔成交同时是一次买入和一次卖出，它本身没有方向。市面上
「板块资金净流入」那类数字要么来自逐笔的主动买卖推断（`adapters/tencent_detail.py`
那一路，只有当日、且方向是厂商推断的），要么来自东财 push2（本机被出口代理挡死，
见 `docs/DATA_SOURCES.md` §三）。这一层两样都不是，所以命名、注释、简报文案三处
都不许把它写成「资金净流入」「主力流入」——它只回答**成交额的注意力挪到了哪里**。

## 为什么用占比而不是绝对额

因为全市场的成交总额本身就在剧烈波动。实测 2026-07-14→08-03 的 90 个板块合计
成交额：单日在 19445 亿到 29742 亿之间，近 5 日均值比前 10 日均值低 9.8%。在这个
背景下看绝对额，几乎每个板块都会显得「资金在流出」——那测的是全市场的水位，
不是板块之间的迁移。占比把水位除掉，剩下的才是「钱从哪个板块挪到了哪个板块」。

代价是占比会**抹掉总量信息**，而总量恰恰是超短最该知道的一件事（存量博弈还是
增量入场，同样的高低切换含义完全不同）。所以总量单独出一个读数，见
`TurnoverMigration.total_change`——被占比除掉的那个数，必须原样报回来。

## 占比上升不等于资金进场

这是这一层唯一容易用错的地方，所以判据里强制配价格方向：

```text
占比↑ + 价↑   放量上涨   有人在买
占比↑ + 价↓   放量下跌   有人在跑     ← 只看占比会把这一格读成「资金流入」
占比↓ + 价↑   缩量上涨   惜售，但没有新钱
占比↓ + 价↓   缩量阴跌   没人看
```

实测 2026-08-03：电子化学品成交额占比几乎没动（+0.7%）而近 5 日跌 14.4%，
互联网电商占比涨 41.7% 但价格是跌的——后者若只报占比，会被读成资金在进场。

## 不做的事

- **不给权重、不进打分。** 与五档、同花顺指数、量价形态、板块轮动同一处置：
  先进简报与台账，等 `review` 攒够样本能回答「占比抬升的板块里选出来的票兑现率
  高多少」，再谈要不要给分。
- **不预测。** 输出全部是对已发生事实的陈述句。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.numeric import mean
from ashare_decision_support.sectors import panel
from ashare_decision_support.sectors.panel import MIN_SECTORS

# 近端与前段窗口。5 + 15 = 20 个交易日 ≈ 一个月，与 `rotation.py` 的提法对齐。
# 两段**不重叠**，理由和 `rotation.py:SectorTrend.ret_prior` 完全一样：前段若
# 包含近端，近端的暴涨会自动抬高前段，把迁移幅度机械地压小。
RECENT_WINDOW = 5
PRIOR_WINDOW = 15

# 占比相对变化小于这个数就归入「平」。不留中性带的话，占比在 0 附近的小板块
# 每天都会在「进场」和「撤离」之间翻面。
SHARE_MOVE = 0.05

# 近端窗口内部的量能走向要多大才值得单独说一句。合计成交额是 90 个板块的和，
# 日间 ±5% 是常态噪声；低于这个幅度时「窗口内在放量」和「窗口内基本没动」
# 讲的是同一件事，多印一行只会稀释上面那行的信息。见 `TurnoverMigration.total_trend`。
TREND_MOVE = 0.05

# 榜单取前几名。
TOP_COUNT = 10

QUADRANT_INFLOW = "放量上涨"
QUADRANT_DISTRIBUTION = "放量下跌"
QUADRANT_DRY_RALLY = "缩量上涨"
QUADRANT_NEGLECT = "缩量阴跌"
QUADRANT_FLAT = "占比基本没动"

_QUADRANT_GLOSS = {
    QUADRANT_INFLOW: "成交额占比抬升且价格在涨，有人在买",
    QUADRANT_DISTRIBUTION: "成交额占比抬升但价格在跌，放量下跌是有人在跑，不是资金进场",
    QUADRANT_DRY_RALLY: "价格在涨但成交额占比回落，惜售，没有新钱跟进",
    QUADRANT_NEGLECT: "占比与价格同时回落，没人看",
    QUADRANT_FLAT: "占比变化在中性带内，说不出迁移方向",
}


@dataclass(frozen=True)
class SectorTurnover:
    """一个板块的成交额迁移读数。"""

    code: str
    name: str
    share_recent: float | None = None
    share_prior: float | None = None
    # 占比的**相对**变化。用相对而不是绝对差：绝对差永远由大板块支配（占比 3%
    # 的半导体动 0.3 个点是小事，占比 0.06% 的教育翻一倍才动 0.07 个点），
    # 而「翻了一倍」正是小板块上最该被看到的信号。
    share_change: float | None = None
    amount_recent: float | None = None
    amount_prior: float | None = None
    ret_recent: float | None = None
    quadrant: str = QUADRANT_FLAT

    @property
    def amount_delta(self) -> float | None:
        """日均成交额的绝对增减。占比回答「份额变了多少」，这个回答「几个亿」。"""
        if self.amount_recent is None or self.amount_prior is None:
            return None
        return self.amount_recent - self.amount_prior

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "share_recent": self.share_recent,
            "share_prior": self.share_prior,
            "share_change": self.share_change,
            "amount_recent": self.amount_recent,
            "amount_prior": self.amount_prior,
            "amount_delta": self.amount_delta,
            "ret_recent": self.ret_recent,
            "quadrant": self.quadrant,
        }


@dataclass
class TurnoverMigration:
    """一次成交额迁移读数。`trusted=False` 时所有结论都不该被引用。"""

    trade_date: str = ""
    sectors: int = 0
    trusted: bool = False
    # 全市场（这里指全体板块合计）日均成交额，以及它的相对变化。**占比刻意除掉的
    # 那个量**，必须单独报回来：存量博弈下的高低切换和增量入场下的高低切换，
    # 对仓位的含义相反。
    total_recent: float | None = None
    total_prior: float | None = None
    total_change: float | None = None
    # 近端窗口**内部**的量能走向，以及最新一日的水位。
    #
    # `total_change` 比的是两个块的**均值**，它答的是「这一周的水位比上个月高
    # 还是低」。它答不了「水位正在涨还是在退」——而在 V 形拐点那一周，这两个
    # 答案会相反。2026-08-07 实测：近 5 日均 24301 亿对前 15 日均 24737 亿是
    # -1.8%，措辞落在「萎缩」；而这 5 天自己从 20111 亿一路放到 26834 亿，是
    # +33%，最新一日还比前段常态高 8.5%。
    #
    # 只报块均值，读简报的人会按「存量腾挪」处理一个量能刚触底回升的市场——正是
    # 本模块开头那句「存量博弈还是增量入场，同样的高低切换含义完全不同」要防的
    # 错，只不过错在拐点上而不是错在水位上。
    total_latest: float | None = None
    total_trend: float | None = None
    entries: list[SectorTurnover] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.trusted and bool(self.entries)

    def rising(self, n: int = TOP_COUNT) -> list[SectorTurnover]:
        scored = [item for item in self.entries if item.share_change is not None]
        return sorted(scored, key=lambda item: item.share_change or 0.0, reverse=True)[:n]

    def falling(self, n: int = TOP_COUNT) -> list[SectorTurnover]:
        scored = [item for item in self.entries if item.share_change is not None]
        return sorted(scored, key=lambda item: item.share_change or 0.0)[:n]

    def by_quadrant(self, quadrant: str) -> list[SectorTurnover]:
        picked = [item for item in self.entries if item.quadrant == quadrant]
        return sorted(picked, key=lambda item: abs(item.share_change or 0.0), reverse=True)

    def by_amount_delta(self, n: int = 5, rising: bool = True) -> list[SectorTurnover]:
        """按日均成交额的**绝对**增减排序。

        占比榜会漏掉一整类情况：总量整体收缩时，一个成交额腰斩的板块，占比可能
        几乎没动——因为所有人都在同步失血。实测 2026-08-03 的半导体就是这样，
        日均少了 887 亿（全场最大），占比只动 -4.8%，落在中性带里，在占比榜上
        一行都不出现。份额答「钱挪去哪了」，绝对额答「哪里的钱最多地消失了」，
        总量收缩的市场里后者才是主要事实。
        """
        scored = [item for item in self.entries if item.amount_delta is not None]
        scored.sort(key=lambda item: item.amount_delta or 0.0, reverse=rising)
        return scored[:n]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "sectors": self.sectors,
            "trusted": self.trusted,
            "total_recent": self.total_recent,
            "total_prior": self.total_prior,
            "total_change": self.total_change,
            "total_latest": self.total_latest,
            "total_trend": self.total_trend,
            "rising": [item.to_dict() for item in self.rising()],
            "falling": [item.to_dict() for item in self.falling()],
            "distribution": [item.to_dict() for item in self.by_quadrant(QUADRANT_DISTRIBUTION)],
            "amount_gained": [item.to_dict() for item in self.by_amount_delta(rising=True)],
            "amount_lost": [item.to_dict() for item in self.by_amount_delta(rising=False)],
            "observations": list(self.observations),
        }


def read_turnover(
    series: dict[str, tuple[str, list[dict]]],
    recent_window: int = RECENT_WINDOW,
    prior_window: int = PRIOR_WINDOW,
) -> TurnoverMigration:
    """板块指数序列 → 成交额迁移读数。纯计算，不碰网络。

    `series` 形如 `{code: (板块名, 日线)}`，即
    `adapters/ths_sector_index.load_all_series()` 的产物。日线里要有 `amount`。
    """
    read = TurnoverMigration()
    need = recent_window + prior_window
    aligned, notes = _align(series, need)
    read.notes.extend(notes)
    if not aligned:
        read.observations.append("没有可用的板块成交额序列，本次无迁移读数。")
        return read

    read.trade_date = str(aligned[0][2][-1]["date"])
    read.sectors = len(aligned)
    if len(aligned) < MIN_SECTORS:
        read.notes.append(
            f"仅 {len(aligned)} 个板块有足够长的成交额序列（需 {MIN_SECTORS} 个），"
            "占比是横截面量，这个样本量上不可比，本次不给迁移结论"
        )
        return read

    # 逐日总额 → 逐日占比 → 两段窗口各自取均值。
    #
    # 先按日归一再取均值，而不是「先求和再相除」：后者会让成交额最大的那一天
    # 支配整段窗口的占比，等于用一天代表一周。
    span = [bars[-need:] for _code, _name, bars in aligned]
    totals = [sum(day[i]["amount"] for day in span) for i in range(need)]
    if any(total <= 0 for total in totals):
        read.notes.append("板块成交额存在非正的日合计，本次不给迁移结论")
        return read

    # 四段窗口都取自 `span`，而 `span` 每条的长度就是 `need = recent + prior`，
    # 所以这几个切片非空是**由上面的长度闸门保证**的，`mean` 不会在这里交出 None。
    entries: list[SectorTurnover] = []
    for index, (code, name, bars) in enumerate(aligned):
        window = span[index]
        shares = [window[i]["amount"] / totals[i] for i in range(need)]
        share_recent = mean(shares[-recent_window:])
        share_prior = mean(shares[:prior_window])
        amount_recent = mean(row["amount"] for row in window[-recent_window:])
        amount_prior = mean(row["amount"] for row in window[:prior_window])
        change = (share_recent / share_prior - 1.0) if share_prior else None
        ret = panel.pct(panel.closes(bars), recent_window)
        entries.append(
            SectorTurnover(
                code=code,
                name=name,
                share_recent=panel.rounded(share_recent),
                share_prior=panel.rounded(share_prior),
                share_change=panel.rounded(change),
                amount_recent=panel.rounded(amount_recent, 2),
                amount_prior=panel.rounded(amount_prior, 2),
                ret_recent=panel.rounded(ret),
                quadrant=_quadrant(change, ret),
            )
        )

    read.entries = entries
    read.total_recent = panel.rounded(mean(totals[-recent_window:]), 2)
    read.total_prior = panel.rounded(mean(totals[:prior_window]), 2)
    if read.total_prior:
        read.total_change = panel.rounded(read.total_recent / read.total_prior - 1.0)
    # 近端窗口内部的走向。取首末两根而不是拟合斜率：这一层要答的是「这一周结束时
    # 的水位比开始时高还是低」，首末差就是它，斜率还要再解释一遍单位。
    read.total_latest = panel.rounded(totals[-1], 2)
    window_open = totals[-recent_window]
    if window_open > 0:
        read.total_trend = panel.rounded(totals[-1] / window_open - 1.0)
    read.trusted = True
    read.observations = _observations(read, recent_window, prior_window)
    return read


# ---- internals -----------------------------------------------------------


def _align(
    series: dict[str, tuple[str, list[dict]]], need: int
) -> tuple[list[tuple[str, str, list[dict]]], list[str]]:
    """对齐 + 把丢弃的原因写成本层的话。判据在 `sectors/panel.py:align`。

    对齐的理由与 `rotation.py:_align` 相同，但这里更硬：占比的分母是**当日**
    全体板块的合计额，一个停更的板块会让它缺席的那几天的分母整体偏小，从而把
    其余每一个板块的占比都抬高一点。错的不只是它自己那一行。

    也因此这里要求 `amount` 与 `close` **两格都在**：占比来自 amount、象限判定
    来自 close，缺任一格的那一根都会让四象限只剩半边。
    """
    notes: list[str] = []
    result = panel.align(series, need, keys=("amount", "close"))
    if not result:
        return [], notes
    if result.dropped:
        notes.append(
            f"成交额迁移：{result.dropped} 个板块最新交易日不是 {result.latest}，已排除"
            "（缺席会让当天的占比分母偏小，抬高其余所有板块）"
        )
    return result.rows, notes


def _quadrant(change: float | None, ret: float | None) -> str:
    """占比变化 × 价格方向 → 四象限。缺任何一边都不给象限。"""
    if change is None or ret is None or abs(change) < SHARE_MOVE:
        return QUADRANT_FLAT
    if change > 0:
        return QUADRANT_INFLOW if ret > 0 else QUADRANT_DISTRIBUTION
    return QUADRANT_DRY_RALLY if ret > 0 else QUADRANT_NEGLECT


def _rebounding(read: TurnoverMigration) -> bool:
    """近端窗口内部在放量。只用来压住「存量腾挪」那句断言，不单独出读数。"""
    return read.total_trend is not None and read.total_trend >= TREND_MOVE


def _trend_line(read: TurnoverMigration, recent_window: int) -> str:
    """近端窗口内部的量能走向。块均值对比看不见拐点，这一行专门补它。

    只在幅度过 `TREND_MOVE` 时出声；两个读数反号时额外点明该按哪个读，因为那正是
    「萎缩 1.8%」与「放量 33%」同时成立、而只看前者会得出相反结论的那种日子。
    """
    if read.total_trend is None or read.total_latest is None:
        return ""
    if abs(read.total_trend) < TREND_MOVE:
        return ""
    direction = "放量" if read.total_trend > 0 else "缩量"
    line = (
        f"近 {recent_window} 日**内部**{direction} {abs(read.total_trend):.1%}"
        f"（窗口首日 → 末日），最新一日 {read.total_latest / 1e8:.0f} 亿"
    )
    if read.total_change is not None and read.total_trend * read.total_change < 0:
        line += (
            "——与上一行的块均值对比**指向相反**，拐点落在窗口内部，"
            "量能水位按最新一日读，不按窗口均值读。"
        )
    else:
        line += "。"
    return line


def _observations(
    read: TurnoverMigration, recent_window: int, prior_window: int
) -> list[str]:
    """把读数写成陈述句。**全部是对已发生事实的描述，不含任何对明天的推断。**"""
    lines: list[str] = []

    # 总量先说。占比的所有结论都是在这个水位之上的相对变化，水位本身变了的话，
    # 「资金从 A 挪到 B」和「A、B 一起在失血、只是 A 更快」读起来完全一样。
    if read.total_change is not None:
        direction = "放大" if read.total_change > 0 else "萎缩"
        line = (
            f"{read.sectors} 个板块合计日均成交额 {read.total_recent / 1e8:.0f} 亿"
            f"（近 {recent_window} 日）对 {read.total_prior / 1e8:.0f} 亿"
            f"（其前 {prior_window} 日），{direction} {abs(read.total_change):.1%}"
        )
        # 「存量腾挪」是一句**断言**，不是读数，所以它要同时过块均值和窗口内走向
        # 两道闸：块均值在下、但窗口内在放量，说明这一周的水位是从窗口头几天的
        # 低位爬上来的——那不是存量市场，那是刚回来的增量。
        if read.total_change <= -SHARE_MOVE and not _rebounding(read):
            line += "——总量在收缩，下面的迁移是存量腾挪，不是增量入场。"
        else:
            line += "。"
        lines.append(line)

    trend_line = _trend_line(read, recent_window)
    if trend_line:
        lines.append(trend_line)

    rising = read.rising(5)
    if rising:
        lines.append(
            f"成交额占比抬升最快（近 {recent_window} 日对其前 {prior_window} 日）："
            + "、".join(
                f"{item.name}（{item.share_change:+.0%}，"
                f"日均 {item.amount_delta / 1e8:+.0f} 亿，近 {recent_window} 日 "
                f"{item.ret_recent:+.1%}）"
                for item in rising
                if item.share_change is not None
                and item.amount_delta is not None
                and item.ret_recent is not None
            )
        )
    falling = read.falling(5)
    if falling:
        lines.append(
            "占比回落最快："
            + "、".join(
                f"{item.name}（{item.share_change:+.0%}，"
                f"日均 {item.amount_delta / 1e8:+.0f} 亿，近 {recent_window} 日 "
                f"{item.ret_recent:+.1%}）"
                for item in falling
                if item.share_change is not None
                and item.amount_delta is not None
                and item.ret_recent is not None
            )
        )

    # 绝对额单独出一行。总量收缩时占比榜会整体失真，见 `by_amount_delta`。
    lost = [item for item in read.by_amount_delta(3, rising=False) if (item.amount_delta or 0) < 0]
    if lost:
        lines.append(
            "日均成交额减少最多（绝对额，占比榜看不见这一类）："
            + "、".join(
                f"{item.name}（{item.amount_delta / 1e8:+.0f} 亿，占比 "
                f"{item.share_change:+.0%}，近 {recent_window} 日 {item.ret_recent:+.1%}）"
                for item in lost
                if item.amount_delta is not None
                and item.share_change is not None
                and item.ret_recent is not None
            )
        )

    # 放量下跌单独点名。这一格是整层里唯一会被读反的：占比在涨，但那是有人在跑。
    distribution = read.by_quadrant(QUADRANT_DISTRIBUTION)
    if distribution:
        lines.append(
            f"⚠️ 占比抬升但价格在跌（{_QUADRANT_GLOSS[QUADRANT_DISTRIBUTION]}）："
            + "、".join(
                f"{item.name}（占比 {item.share_change:+.0%}，近 {recent_window} 日 "
                f"{item.ret_recent:+.1%}）"
                for item in distribution[:5]
                if item.share_change is not None and item.ret_recent is not None
            )
        )

    neglected = read.by_quadrant(QUADRANT_NEGLECT)
    if neglected:
        lines.append(
            "占比与价格同时回落（没人看）："
            + "、".join(item.name for item in neglected[:6])
        )
    return lines
