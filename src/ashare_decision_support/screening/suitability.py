"""超短线标的适配性筛选：五层漏斗，每层都能独立否掉一只票。

## 这个模块要挡住的那个错误

「找出昨日低点到次日高点差 2% 的票，就在低点买」——这条规则本身没错，错在
**这两个价位事后才知道**。收盘前你不知道今天的最低价在哪，更不知道明天的最高价
在哪。拿这个差价去筛票，筛出来的是「过去波动大的票」，不是「未来能赚钱的票」，
而且几乎所有日振幅 > 2% 的标的都会通过——A 股这样的票有几千只。

所以本模块**同时算三档口径**，并把它们并排放进同一张卡片：

| 口径 | 买 | 卖 | 能不能实盘做到 |
|---|---|---|---|
| `oracle` | T 日最低 | T+1 日最高 | **不能。** 上帝视角，只作理论上限 |
| `neutral` | T 日收盘 | T+1 日 (H+L+C)/3 | 能。不择时的基准 |
| `strict` | T 日收盘 | T+1 日开盘 | 能。完全机械，零判断 |

`oracle - strict` 这个差值就是**择时溢价**：它是你全年盯盘、精确抄底逃顶，理论上
最多能多赚的部分。把这个 gap 印出来，比印一张「稳赚名单」有用。

## 首轮实测：那 2% 全部是择时溢价

2026-08-01 取 25 只（白马 / 题材 / 金融 / 周期 / 科创各档）两年日线，成本按 0.202%
往返扣除：

| 读数 | 结果 |
|---|---|
| `oracle` 中位数的中位数 | **1.91%** |
| 满足「≥2%」的标的 | 11 / 25 |
| `neutral` 中位数的中位数 | **-0.20%** |
| 其中 `neutral` 为正的 | **2 / 25** |
| 择时溢价占 `oracle` 的比例 | **95% ~ 105%** |

`neutral` 的 -0.20% 几乎正好等于往返成本——**毛收益中位数是 0**。这不是筛选失败，
这是市场有效性的直接读数：日线级别「收盘买、次日卖」不存在系统性 edge，无论你挑
哪只票。「昨日低点到次日高点差 2%」筛出来的东西 100% 是波动率，0% 是可执行收益。

走到 L2 的那两只（300308.SZ / 601899.SH）也没能通过，形态一模一样：

    胜率 50.7%（抛硬币）  中位 +0.05%  p25 -1.0%~-1.8%  四段里只有 2 段为正

典型日赚 5 个基点、差日亏 100~180 个基点，且收益集中在后半段的一波行情。这正是
L2 存在的理由——全样本中位数是正的，分段一看就散了。

**这个结论不是「超短线不能做」。** 它是：那 2% 的空间**真实存在**（`oracle` 是实测
出来的），但它只能靠择时拿到，拿不到的部分不会因为换一只票而变得可拿。所以这个
模块的正确用法是**定天花板与基线**——`oracle` 告诉你这只票最多能给多少，`neutral`
告诉你零判断的基线在哪（约 -0.2%）。任何择时规则的实测收益，要显著高于 -0.2% 才
说明判断本身有价值，否则它只是在赚波动率的噪声。

## 为什么是五层而不是一个总分

总分会把「流动性枯竭」和「胜率略低」混成同一个数，而这两件事的性质完全不同：
前者是一票否决（买不进就没有后续），后者是程度问题。分层的另一个好处是**淘汰
原因可追溯**——每张卡片都记录它死在哪一层。

    L0 tradability  可交易性：流动性、一字板、停牌、价格下限   ← 一票否决
    L1 capture      波动可捕捉：三档口径的净收益分布
    L2 consistency  稳定可重复：胜率、下四分位、跨期一致性、最大连亏
    L3 behavior     行为规律：跳空离散度、高开低走率、接得住率
    L4 holdout      样本外：前 70% 筛选、后 30% 验证，报告衰减

## 反过拟合是内建的，不是选项

L4 的切分不能关。一个在全样本上调出来的阈值，在全样本上一定好看——这是恒真的，
没有信息量。`SuitabilityCard.decay` 报告「样本外指标 ÷ 样本内指标」，这个数比任何
单期指标都重要：**衰减超过一半的票，筛选期的漂亮数字是噪声**，卡片会直接标记
`unstable` 而不是让它进最终名单。

## 口径

日线**前复权**，和 `journal/verify.py` 一致，相对百分比因此可比。所有收益都是
百分比，绝对价不出现在任何输出里。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

# ---------------------------------------------------------------------------
# 成本
# ---------------------------------------------------------------------------

# A 股一次「买入 + 次日卖出」往返的真实成本。数字取 2026 年现行费率：
#
#   印花税   0.05%  仅卖出单边（2023-08-28 减半后至今）
#   佣金     0.025% 双边各收（万 2.5，忽略 5 元最低——超短线单笔不会那么小）
#   过户费   0.001% 双边各收（2022 年起沪深统一）
#   滑点     0.05%  双边各估
#
# 合计约 0.2%。这个数看着小，但超短线一年做 200 个来回就是 40% 的摩擦——**成本
# 必须在筛选阶段就扣掉**，否则筛出来的会是一批「毛收益能覆盖成本、净收益是负数」
# 的高换手标的，而这类票恰恰是最像「适合超短线」的。
STAMP_DUTY_SELL = 0.0005
COMMISSION_SIDE = 0.00025
TRANSFER_FEE_SIDE = 0.00001
DEFAULT_SLIPPAGE_SIDE = 0.0005


@dataclass(frozen=True)
class CostModel:
    """往返成本。滑点单独拎出来是因为它是唯一需要按标的调的那项。

    流动性差的票滑点远不止 0.05%，但日线看不出真实盘口深度，所以这里**不自作
    聪明地按成交额外推**——把滑点做成显式参数，让调用方为不同池子传不同的值，
    比在模块里埋一个拍脑袋的映射表诚实。
    """

    slippage_side: float = DEFAULT_SLIPPAGE_SIDE

    @property
    def round_trip(self) -> float:
        return (
            STAMP_DUTY_SELL
            + COMMISSION_SIDE * 2
            + TRANSFER_FEE_SIDE * 2
            + self.slippage_side * 2
        )


# ---------------------------------------------------------------------------
# 涨跌停
# ---------------------------------------------------------------------------

# 按板块的涨跌停幅度。判断「一字板」必须知道这个数，而一字板是可交易性的核心：
# 一字涨停买不进，一字跌停卖不掉，这两天的收益记进统计就是纯粹的幻觉。
_LIMIT_BY_PREFIX = (
    (("688",), 0.20),  # 科创板
    (("300", "301"), 0.20),  # 创业板
    (("8", "4", "9"), 0.30),  # 北交所
)
DEFAULT_LIMIT_PCT = 0.10  # 沪深主板

# 判定涨跌停时的容差。除权除息、复权因子、四舍五入都会让实际涨幅差那么一点点，
# 卡死在 10.00% 会漏掉大量真实涨停。
LIMIT_TOLERANCE = 0.005


def limit_pct(symbol: str) -> float:
    """这只票的单日涨跌停幅度。

    ST 股是 5%，但日线数据里没有 ST 标记，光看代码判断不出来。这里返回的是
    **上界**——把 ST 当成 10% 只会少判几个涨停，不会把不能交易的日子误判成能
    交易，方向是安全的。
    """
    code = "".join(ch for ch in str(symbol) if ch.isdigit())
    for prefixes, pct in _LIMIT_BY_PREFIX:
        if code.startswith(prefixes):
            return pct
    return DEFAULT_LIMIT_PCT


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenConfig:
    """筛选阈值。全部可调，但默认值有来历，改之前先读注释。"""

    # ---- L0 可交易性 ----
    min_avg_amount: float = 1e8
    """日均成交额下限（元）。1 亿是超短线的经验底线：低于这个数，一笔百万级的
    单子就能自己把价格打出去，回测里的成交价在实盘不存在。"""

    min_bars: int = 120
    """最少交易日。少于半年的样本切出 30% 样本外后只剩 36 天，任何统计都不成立。"""

    max_one_word_rate: float = 0.03
    """一字板占比上限。一字板是「想买买不到、想卖卖不掉」的日子，占比高说明这
    只票的价格由资金锁仓决定而不是由撮合决定——超短线的对立面。"""

    max_suspension_rate: float = 0.05
    """停牌（零成交）日占比上限。"""

    min_price: float = 2.0
    """最低价下限（元）。2 元的票一个最小变动单位就是 0.5%，价格网格本身把收益
    切成了粗颗粒，2% 的目标在它身上是三跳，噪声吃掉一切。"""

    # ---- L1 波动可捕捉 ----
    min_oracle_median: float = 0.02
    """`oracle` 口径净收益中位数下限。**这就是用户原始规则的 2%**，保留在这里
    作为第一道波动筛，但它只是必要条件，绝不是结论。"""

    min_executable_median: float = 0.0
    """可执行口径（`neutral`）净收益中位数下限。默认 0：先要求「机械执行不亏」，
    这一条就能砍掉绝大多数只有 oracle 好看的票。"""

    # ---- L2 稳定可重复 ----
    min_win_rate: float = 0.5
    """可执行口径的胜率下限。"""

    min_segment_pass: int = 3
    """四段里至少几段中位数为正。这一条挡的是「全年收益集中在一波行情」——那种
    票的历史统计漂亮，但那波行情不会因为你筛到它就重来一次。"""

    max_loss_streak: int = 8
    """最大连续亏损天数上限。超短线扛不住长连亏，不是因为亏损额，是因为人会在
    第五次之后改规则。"""

    # ---- L3 行为规律 ----
    max_gap_iqr: float = 0.05
    """隔夜跳空的四分位距上限。跳空越离散，「昨收买入」的实际成本越不可控。"""

    max_bad_open_rate: float = 0.45
    """高开低走率上限。这个形态是隔夜持仓的主要杀手：高开给你看到浮盈，收盘拿
    回去，而 T+1 让你无法在最高点走。"""

    # ---- L4 样本外 ----
    holdout_ratio: float = 0.3
    """样本外占比。"""

    min_decay: float = 0.5
    """样本外 ÷ 样本内的下限。低于此值判为 `unstable`：不是说这只票一定不行，
    是说**这份历史支撑不了这个结论**。"""

    # ---- 执行假设 ----
    exclude_limit_up_close: bool = True
    """T 日涨停收盘时排除该样本。涨停价挂单排队基本买不到，把这天算进去等于假设
    你能在封单里插队。关掉它统计会变好看，那份好看是假的。"""

    cost: CostModel = field(default_factory=CostModel)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundTrip:
    """一次「T 日买入 → T+1 日卖出」的模拟往返。"""

    index: int
    buy: float
    sell: float
    gross: float
    net: float


@dataclass(frozen=True)
class Tradability:
    """L0：这只票**能不能交易**。和赚不赚钱无关，是前置条件。"""

    bars: int = 0
    avg_amount: float | None = None
    min_amount: float | None = None
    one_word_rate: float | None = None
    suspension_rate: float | None = None
    min_price: float | None = None
    atr_pct: float | None = None
    passed: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Capture:
    """L1：三档口径各自的净收益分布，以及它们之间的差。"""

    oracle_median: float | None = None
    neutral_median: float | None = None
    strict_median: float | None = None
    timing_premium: float | None = None
    """`oracle - strict`：择时溢价。这个数越大，说明这只票的收益越依赖你精确
    抄底逃顶的能力，而不是标的本身的性质。**大不是好事，是脆弱。**"""
    executable_ratio: float | None = None
    """`neutral / oracle`：理论收益里机械执行能拿到的比例。"""
    samples: int = 0
    unfillable: int = 0
    """因一字板/停牌/涨停收盘被剔除的样本数。这个数本身就是可交易性读数。"""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Consistency:
    """L2：可执行口径的收益**分布形状**。

    全部用中位数和分位数，不用均值。超短线的收益分布是右偏的——少数几天的暴涨
    能把均值拉到正数，而你不可能每次都在场。**均值描述的是这只票，中位数描述的
    是你**。
    """

    n: int = 0
    win_rate: float | None = None
    median: float | None = None
    p25: float | None = None
    """下四分位：差的日子有多差。超短线最该看的一个数。"""
    worst: float | None = None
    max_loss_streak: int | None = None
    segments: list[float] = field(default_factory=list)
    segment_pass: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Behavior:
    """L3：价格行为的规律性。回答「凭什么它会重复」。"""

    gap_median: float | None = None
    gap_iqr: float | None = None
    bad_open_rate: float | None = None
    """高开低走率：高开且收盘低于开盘的日子占比。"""
    close_position: float | None = None
    """收盘在当日区间的位置 (C-L)/(H-L) 的中位数。偏高=尾盘常收在高位。"""
    dip_recovery_rate: float | None = None
    """跌超 1 个 ATR 之后，次日收阳的比例。这是「低吸接得住吗」的直接读数。"""
    limit_up_rate: float | None = None
    limit_down_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SuitabilityCard:
    """一只票的完整筛选卡片。

    `verdict` 只有三个值：

      `pass`      五层全过，且样本外没有明显衰减
      `unstable`  样本内过了但样本外衰减过半——**历史支撑不了结论**，不是「不行」
      `reject`    死在某一层，`rejected_at` 记录是哪一层
    """

    symbol: str
    verdict: str = "reject"
    rejected_at: str | None = None
    reasons: list[str] = field(default_factory=list)

    tradability: Tradability = field(default_factory=Tradability)
    capture: Capture = field(default_factory=Capture)
    consistency: Consistency = field(default_factory=Consistency)
    behavior: Behavior = field(default_factory=Behavior)

    holdout_capture: Capture | None = None
    holdout_consistency: Consistency | None = None
    decay: float | None = None
    """样本外中位数 ÷ 样本内中位数。None=样本外算不出来。"""

    rank_key: float | None = None
    """排序键：样本外的净收益中位数。用样本外而不是全样本，因为**排序也会过拟合**
    ——按样本内排出来的第一名，通常只是过拟合最深的那只。"""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def screen_symbol(
    symbol: str,
    bars: list[dict],
    config: ScreenConfig | None = None,
) -> SuitabilityCard:
    """跑完五层漏斗，返回一张卡片。

    任何情况下都不抛异常：批量筛几千只票时，一只票的脏数据不该让整轮停下来。
    数据不足时返回 `verdict="reject"` 且 `rejected_at="data"`，和「算出来不合格」
    能区分开。
    """
    cfg = config or ScreenConfig()
    rows = _normalise(bars)

    if len(rows) < cfg.min_bars:
        return SuitabilityCard(
            symbol=symbol,
            rejected_at="data",
            reasons=[f"日线仅 {len(rows)} 根，少于 {cfg.min_bars} 根"],
            tradability=Tradability(bars=len(rows)),
        )

    # L0 可交易性。放在最前面是因为它一票否决，后面几层再好看都没用。
    tradability = _tradability(symbol, rows, cfg)
    if not tradability.passed:
        return SuitabilityCard(
            symbol=symbol,
            rejected_at="tradability",
            reasons=list(tradability.reasons),
            tradability=tradability,
        )

    # 样本内 / 样本外切分。切分点按**时间**而不是随机——随机切分会让样本外包含
    # 样本内之后的信息，对时间序列是泄漏。
    split = int(len(rows) * (1.0 - cfg.holdout_ratio))
    in_sample, out_sample = rows[:split], rows[split:]

    capture = _capture(symbol, in_sample, cfg)
    consistency = _consistency(symbol, in_sample, cfg)
    behavior = _behavior(symbol, in_sample, cfg)

    holdout_capture = _capture(symbol, out_sample, cfg) if len(out_sample) >= 20 else None
    holdout_consistency = (
        _consistency(symbol, out_sample, cfg) if len(out_sample) >= 20 else None
    )

    reasons: list[str] = []

    # L1 波动可捕捉
    if not _at_least(capture.oracle_median, cfg.min_oracle_median):
        reasons.append(
            f"理论口径中位数 {_pct(capture.oracle_median)} < {_pct(cfg.min_oracle_median)}"
            "（波动本身就不够，后面几层不用看）"
        )
        return _reject(symbol, "capture", reasons, tradability, capture, consistency, behavior)
    if not _at_least(capture.neutral_median, cfg.min_executable_median):
        reasons.append(
            f"可执行口径中位数 {_pct(capture.neutral_median)} < "
            f"{_pct(cfg.min_executable_median)}——理论收益全在择时里，机械执行是负的"
        )
        return _reject(symbol, "capture", reasons, tradability, capture, consistency, behavior)

    # L2 稳定可重复
    consistency_reasons = _consistency_reasons(consistency, cfg)
    if consistency_reasons:
        return _reject(
            symbol, "consistency", consistency_reasons, tradability, capture, consistency, behavior
        )

    # L3 行为规律
    behavior_reasons = _behavior_reasons(behavior, cfg)
    if behavior_reasons:
        return _reject(
            symbol, "behavior", behavior_reasons, tradability, capture, consistency, behavior
        )

    # L4 样本外
    decay = _decay(consistency, holdout_consistency)
    verdict = "pass"
    if holdout_consistency is None:
        verdict = "unstable"
        reasons.append("样本外区间不足 20 根日线，无法验证")
    elif decay is None or decay < cfg.min_decay:
        verdict = "unstable"
        reasons.append(
            f"样本外衰减到 {_ratio(decay)}（阈值 {cfg.min_decay:.0%}）"
            "——筛选期的数字是噪声，不是这只票的性质"
        )

    return SuitabilityCard(
        symbol=symbol,
        verdict=verdict,
        rejected_at=None,
        reasons=reasons,
        tradability=tradability,
        capture=capture,
        consistency=consistency,
        behavior=behavior,
        holdout_capture=holdout_capture,
        holdout_consistency=holdout_consistency,
        decay=decay,
        rank_key=(holdout_consistency.median if holdout_consistency else None),
    )


def screen_many(
    bars_by_symbol: dict[str, list[dict]],
    config: ScreenConfig | None = None,
) -> list[SuitabilityCard]:
    """批量筛选，按 `rank_key` 降序返回。

    返回的是**全部**卡片，包括被淘汰的——淘汰原因的分布本身是有用的信息（比如
    「九成票死在 L2 跨期一致性」说明这套阈值可能太严，而不是市场没机会）。
    """
    cfg = config or ScreenConfig()
    cards = [screen_symbol(symbol, bars, cfg) for symbol, bars in bars_by_symbol.items()]
    cards.sort(key=lambda card: (card.rank_key if card.rank_key is not None else -9.99))
    cards.reverse()
    return cards


# ---------------------------------------------------------------------------
# L0 可交易性
# ---------------------------------------------------------------------------


def _tradability(symbol: str, rows: list[dict], cfg: ScreenConfig) -> Tradability:
    amounts = [row["amount"] for row in rows if row.get("amount")]
    avg_amount = sum(amounts) / len(amounts) if amounts else None
    min_amount = min(amounts) if amounts else None

    one_word = sum(1 for row in rows if _is_one_word(row, symbol))
    suspended = sum(1 for row in rows if not row.get("volume"))
    lowest = min(row["low"] for row in rows)
    atr = _atr_pct(rows)

    one_word_rate = one_word / len(rows)
    suspension_rate = suspended / len(rows)

    reasons: list[str] = []
    if avg_amount is None:
        reasons.append("无成交额字段，无法判断流动性")
    elif avg_amount < cfg.min_avg_amount:
        reasons.append(f"日均成交额 {avg_amount / 1e8:.2f} 亿 < {cfg.min_avg_amount / 1e8:.2f} 亿")
    if one_word_rate > cfg.max_one_word_rate:
        reasons.append(f"一字板占比 {one_word_rate:.1%} > {cfg.max_one_word_rate:.1%}（买不进卖不掉）")
    if suspension_rate > cfg.max_suspension_rate:
        reasons.append(f"停牌日占比 {suspension_rate:.1%} > {cfg.max_suspension_rate:.1%}")
    if lowest < cfg.min_price:
        reasons.append(f"区间最低价 {lowest:.2f} < {cfg.min_price:.2f}（价格网格太粗）")

    return Tradability(
        bars=len(rows),
        avg_amount=avg_amount,
        min_amount=min_amount,
        one_word_rate=round(one_word_rate, 4),
        suspension_rate=round(suspension_rate, 4),
        min_price=lowest,
        atr_pct=atr,
        passed=not reasons,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# L1 波动可捕捉
# ---------------------------------------------------------------------------


def _capture(symbol: str, rows: list[dict], cfg: ScreenConfig) -> Capture:
    oracle = _round_trips(symbol, rows, "oracle", cfg)
    neutral = _round_trips(symbol, rows, "neutral", cfg)
    strict = _round_trips(symbol, rows, "strict", cfg)

    oracle_median = _median([trip.net for trip in oracle])
    neutral_median = _median([trip.net for trip in neutral])
    strict_median = _median([trip.net for trip in strict])

    premium = None
    if oracle_median is not None and strict_median is not None:
        premium = round(oracle_median - strict_median, 6)

    ratio = None
    if oracle_median and neutral_median is not None and oracle_median > 0:
        ratio = round(neutral_median / oracle_median, 4)

    # 可配对的日子总数减去实际成交的，就是被执行约束吃掉的样本。
    pairable = max(len(rows) - 1, 0)
    return Capture(
        oracle_median=oracle_median,
        neutral_median=neutral_median,
        strict_median=strict_median,
        timing_premium=premium,
        executable_ratio=ratio,
        samples=len(neutral),
        unfillable=pairable - len(neutral),
    )


def _round_trips(
    symbol: str, rows: list[dict], mode: str, cfg: ScreenConfig
) -> list[RoundTrip]:
    """按某一档口径生成全部 T→T+1 往返。

    `_fillable()` 过滤掉的日子**直接不进样本**，而不是记成 0 收益。记 0 会稀释
    分布、把中位数拉向零；不进样本则如实反映「这些天你根本没有仓位」。
    """
    cost = cfg.cost.round_trip
    trips: list[RoundTrip] = []

    for index in range(len(rows) - 1):
        today, tomorrow = rows[index], rows[index + 1]
        if not _fillable(symbol, today, tomorrow, cfg):
            continue

        if mode == "oracle":
            buy, sell = today["low"], tomorrow["high"]
        elif mode == "neutral":
            buy = today["close"]
            sell = (tomorrow["high"] + tomorrow["low"] + tomorrow["close"]) / 3.0
        elif mode == "strict":
            buy, sell = today["close"], tomorrow["open"]
        else:  # pragma: no cover - 调用方传错就是 bug，不该静默兜底
            raise ValueError(f"unknown capture mode: {mode}")

        if buy <= 0 or sell <= 0:
            continue
        gross = sell / buy - 1.0
        trips.append(
            RoundTrip(
                index=index,
                buy=buy,
                sell=sell,
                gross=round(gross, 6),
                net=round(gross - cost, 6),
            )
        )

    return trips


def _fillable(symbol: str, today: dict, tomorrow: dict, cfg: ScreenConfig) -> bool:
    """这一对交易日，实盘能不能真的买进又卖出。

    四种情况下不能，全部剔除：

    1. T 日一字涨停——买不到，封单里排队排不上
    2. T 日涨停价收盘——同上，只是理论上盘中有过机会（`exclude_limit_up_close` 可关）
    3. T+1 日一字跌停——卖不掉
    4. 任一日零成交（停牌）——没有撮合

    这四条是这个模块和「拿 OHLC 直接算收益」的全部区别，也是回测虚高的主要来源。
    """
    if not today.get("volume") or not tomorrow.get("volume"):
        return False
    if _is_one_word(today, symbol) and _change(today) > 0:
        return False
    if _is_one_word(tomorrow, symbol) and _change(tomorrow) < 0:
        return False
    if cfg.exclude_limit_up_close and _at_limit_up(today, symbol):
        return False
    return True


# ---------------------------------------------------------------------------
# L2 稳定可重复
# ---------------------------------------------------------------------------


def _consistency(symbol: str, rows: list[dict], cfg: ScreenConfig) -> Consistency:
    trips = _round_trips(symbol, rows, "neutral", cfg)
    nets = [trip.net for trip in trips]
    if not nets:
        return Consistency()

    ordered = sorted(nets)
    segments = _segment_medians(nets, 4)

    return Consistency(
        n=len(nets),
        win_rate=round(sum(1 for net in nets if net > 0) / len(nets), 4),
        median=_median(nets),
        p25=round(_quantile(ordered, 0.25), 6),
        worst=round(ordered[0], 6),
        max_loss_streak=_max_loss_streak(nets),
        segments=segments,
        segment_pass=sum(1 for value in segments if value > 0),
    )


def _consistency_reasons(consistency: Consistency, cfg: ScreenConfig) -> list[str]:
    reasons: list[str] = []
    if consistency.n == 0:
        return ["无可成交样本"]
    if not _at_least(consistency.win_rate, cfg.min_win_rate):
        reasons.append(f"胜率 {_pct(consistency.win_rate)} < {_pct(cfg.min_win_rate)}")
    if consistency.segment_pass < cfg.min_segment_pass:
        reasons.append(
            f"四段里只有 {consistency.segment_pass} 段中位数为正"
            f"（要求 ≥{cfg.min_segment_pass}）——收益集中在个别区间，不是稳定性质"
        )
    if consistency.max_loss_streak is not None and consistency.max_loss_streak > cfg.max_loss_streak:
        reasons.append(f"最大连亏 {consistency.max_loss_streak} 天 > {cfg.max_loss_streak} 天")
    return reasons


def _segment_medians(values: list[float], parts: int) -> list[float]:
    """把序列均分成 `parts` 段，各算中位数。

    这是全模块最便宜也最有效的一个检查：一只票如果只在某一段赚钱，四段的中位数
    会一正三负，而全样本中位数照样可能是正的。
    """
    if len(values) < parts * 5:
        return []
    size = len(values) // parts
    out: list[float] = []
    for i in range(parts):
        chunk = values[i * size : (i + 1) * size] if i < parts - 1 else values[i * size :]
        value = _median(chunk)
        if value is not None:
            out.append(value)
    return out


def _max_loss_streak(values: list[float]) -> int:
    longest = current = 0
    for value in values:
        current = current + 1 if value <= 0 else 0
        longest = max(longest, current)
    return longest


# ---------------------------------------------------------------------------
# L3 行为规律
# ---------------------------------------------------------------------------


def _behavior(symbol: str, rows: list[dict], cfg: ScreenConfig) -> Behavior:
    gaps: list[float] = []
    bad_opens = 0
    high_opens = 0
    positions: list[float] = []
    limit_ups = 0
    limit_downs = 0

    for index in range(1, len(rows)):
        prev, row = rows[index - 1], rows[index]
        if prev["close"] > 0:
            gap = row["open"] / prev["close"] - 1.0
            gaps.append(gap)
            if gap > 0:
                high_opens += 1
                if row["close"] < row["open"]:
                    bad_opens += 1
        span = row["high"] - row["low"]
        if span > 0:
            positions.append((row["close"] - row["low"]) / span)
        if _at_limit_up(row, symbol):
            limit_ups += 1
        if _at_limit_down(row, symbol):
            limit_downs += 1

    total = max(len(rows) - 1, 1)
    ordered_gaps = sorted(gaps)

    return Behavior(
        gap_median=_median(gaps),
        gap_iqr=(
            round(_quantile(ordered_gaps, 0.75) - _quantile(ordered_gaps, 0.25), 6)
            if len(ordered_gaps) >= 4
            else None
        ),
        bad_open_rate=round(bad_opens / high_opens, 4) if high_opens else None,
        close_position=_median(positions),
        dip_recovery_rate=_dip_recovery(rows),
        limit_up_rate=round(limit_ups / total, 4),
        limit_down_rate=round(limit_downs / total, 4),
    )


def _behavior_reasons(behavior: Behavior, cfg: ScreenConfig) -> list[str]:
    reasons: list[str] = []
    if behavior.gap_iqr is not None and behavior.gap_iqr > cfg.max_gap_iqr:
        reasons.append(
            f"隔夜跳空四分位距 {_pct(behavior.gap_iqr)} > {_pct(cfg.max_gap_iqr)}"
            "——昨收买入的实际成本不可控"
        )
    if behavior.bad_open_rate is not None and behavior.bad_open_rate > cfg.max_bad_open_rate:
        reasons.append(
            f"高开低走率 {_pct(behavior.bad_open_rate)} > {_pct(cfg.max_bad_open_rate)}"
            "——T+1 下这个形态吃掉的正是你的卖出窗口"
        )
    return reasons


def _dip_recovery(rows: list[dict]) -> float | None:
    """跌超 1 个 ATR 之后次日收阳的比例——「低吸接得住吗」。

    用 ATR 而不是固定百分比，理由和 `signals/dipbuy.py` 一样：固定档位在低波动
    和高波动的票上含义完全不同。
    """
    atr = _atr_pct(rows)
    if not atr:
        return None
    hits = wins = 0
    for index in range(1, len(rows) - 1):
        prev, row, nxt = rows[index - 1], rows[index], rows[index + 1]
        if prev["close"] <= 0:
            continue
        if row["close"] / prev["close"] - 1.0 > -atr:
            continue
        hits += 1
        if nxt["close"] > row["close"]:
            wins += 1
    return round(wins / hits, 4) if hits >= 5 else None


# ---------------------------------------------------------------------------
# L4 样本外
# ---------------------------------------------------------------------------


def _decay(in_sample: Consistency, out_sample: Consistency | None) -> float | None:
    """样本外 ÷ 样本内。

    样本内中位数为负时返回 None 而不是一个负比值——那种情况下这只票早就在 L2 被
    砍掉了，硬算出来的比值没有含义，让它保持「算不出来」比编一个数诚实。
    """
    if out_sample is None or in_sample.median is None or out_sample.median is None:
        return None
    if in_sample.median <= 0:
        return None
    return round(out_sample.median / in_sample.median, 4)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _normalise(bars: list[dict]) -> list[dict]:
    """把原始日线洗成 OHLCV 齐全、价格自洽的行。

    `high < low` 或收盘价跑到区间外的行直接丢弃——这类脏数据在批量拉取里不罕见，
    留着会让 `oracle` 口径算出天文数字的收益。
    """
    rows: list[dict] = []
    for bar in bars or []:
        try:
            open_ = float(bar["open"])
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if min(open_, high, low, close) <= 0 or high < low:
            continue
        if not (low <= close <= high) or not (low <= open_ <= high):
            continue
        volume = _float(bar.get("volume")) or 0.0
        amount = _float(bar.get("amount"))
        if amount is None and volume:
            amount = volume * close
        rows.append(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "prev_close": _float(bar.get("prev_close")),
                "timestamp": bar.get("timestamp") or bar.get("date"),
            }
        )
    return rows


def _is_one_word(row: dict, symbol: str) -> bool:
    """一字板：全天只有一个价位。

    严格相等就够了——真正的一字板 OHLC 四个价完全一致。用容差反而会把振幅极小
    的正常交易日误判成一字板。
    """
    return row["high"] == row["low"] == row["open"] == row["close"]


def _change(row: dict) -> float:
    """当日涨跌幅。没有 `prev_close` 时用开盘价近似——一字板场景下开盘价就是全天
    唯一价位，这个近似只在判断一字板方向时用，够了。"""
    prev = row.get("prev_close")
    if prev and prev > 0:
        return row["close"] / prev - 1.0
    if row["open"] > 0:
        return row["close"] / row["open"] - 1.0
    return 0.0


def _at_limit_up(row: dict, symbol: str) -> bool:
    prev = row.get("prev_close")
    if not prev or prev <= 0:
        return False
    return row["close"] / prev - 1.0 >= limit_pct(symbol) - LIMIT_TOLERANCE


def _at_limit_down(row: dict, symbol: str) -> bool:
    prev = row.get("prev_close")
    if not prev or prev <= 0:
        return False
    return row["close"] / prev - 1.0 <= -(limit_pct(symbol) - LIMIT_TOLERANCE)


def _atr_pct(rows: list[dict]) -> float | None:
    """日均真实波幅占价格的比例。和 `signals/dipbuy.py` 同口径。"""
    ranges: list[float] = []
    previous_close: float | None = None
    for row in rows:
        true_range = row["high"] - row["low"]
        if previous_close is not None:
            true_range = max(
                true_range,
                abs(row["high"] - previous_close),
                abs(row["low"] - previous_close),
            )
        if row["close"] > 0:
            ranges.append(true_range / row["close"])
        previous_close = row["close"]
    if len(ranges) < 5:
        return None
    return round(sum(ranges) / len(ranges), 6)


def _quantile(ordered: list[float], q: float) -> float:
    """线性插值分位数。`ordered` 必须已排序。"""
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _median(values: list[float]) -> float | None:
    return round(median(values), 6) if values else None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _at_least(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.2%}"


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _reject(
    symbol: str,
    layer: str,
    reasons: list[str],
    tradability: Tradability,
    capture: Capture,
    consistency: Consistency,
    behavior: Behavior,
) -> SuitabilityCard:
    return SuitabilityCard(
        symbol=symbol,
        verdict="reject",
        rejected_at=layer,
        reasons=reasons,
        tradability=tradability,
        capture=capture,
        consistency=consistency,
        behavior=behavior,
    )
