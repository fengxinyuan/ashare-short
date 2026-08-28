"""量价关系：把「放量上涨 / 缩量下跌」这套话变成可记录、可统计的读数。

改造前整套系统对量能只有**两个**数：`volume_ratio_20d`（当日量 / 前 20 日均量）
和 `dip.volume_contraction`（回调段的缩量程度）。它们都是**单日快照**，答不了超短
最常问的那几个问题：

```text
连续三天放量，还是今天突然放一根？          → vol_trend_5d
这只票的量能对它自己来说算大吗？            → turnover_rate（量比跨票不可比，换手率可比）
最近这段是买盘推的还是卖盘砸的？            → up_down_vol_ratio
价还在创新高，量跟上了吗？                  → divergence
```

**这一层只出读数，不出结论。** 和 `signals/orderflow.py` 同一条口径：「量比 3.2」
是事实，「放量突破」是判断——后者取决于这只票现在处在回调段还是启动段，同一个量比
在两种位置下含义相反（`scoring/rule_based.py:_score_volume` 已经踩过这个坑）。
含义由 `strategy/playbook.py` 的判据链决定，不在这里。

## `up_down_vol_ratio` 不是主动买卖单

它是**近 20 日上涨日成交量之和 ÷ 下跌日成交量之和**，日线粒度的方向性成交量代理。
本机拿不到逐笔（`push2.eastmoney.com` 被出口代理拒绝，QMT 未开通），这是能拿到的
最接近的东西——但它和 L2 的「主动买入额」是两个东西：

- 它按**整根日线的涨跌**给当天的量定方向，一天之内的买卖结构完全看不见；
- 真正的主动买卖单按**每一笔成交撮合在买一还是卖一**分，粒度差了四五个数量级。

所以命名、注释、简报文案三处都不许把它写成「主力资金」「主动买入」「明盘暗盘」。
有分钟线时可以升级到「收在分时均价上方的分钟成交量占比」，那更接近，但仍然不是逐笔。

## 阈值都是先验设定

下面每个常量都是超短交易里通用的经验刻度，**不是从这套系统的台账里标定出来的**，
也没有回测支持。它们的作用是让「放量上涨」这句话第一次有一个确定的判据，而不是
精确。等台账攒够样本（`journal/stats.py:MODEL_READY_SAMPLES`）再谈调参——在那之前
按统计调这些数，调的是噪声。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ashare_decision_support.numeric import finite, mean
from ashare_decision_support.signals.candles import close_position
from ashare_decision_support.signals.dipbuy import normalised_volume
from ashare_decision_support.signals.series import change, series

# 放/缩量的切点。1.2 与 0.8 之间是「平量」——留一条中性带，否则量比在 1.0 附近
# 来回穿越时，四象限标签会每天翻面。
EXPANSION_RATIO = 1.2
CONTRACTION_RATIO = 0.8

# 倍量 / 三倍量。**这是日环比**（当日量 ÷ 前一日量），和下面 `vol_ratio_20d` 那套
# 「对 20 日均量」的口径不是一回事，两者常年不同号：连续温和放量的第 5 天可以是
# `vol_ratio_20d = 2.5` 而环比只有 1.1——后者根本不是倍量。
#
# 分成两个字段而不是共用一个数，因为它们回答的是两个问题：均量比问「这只票今天
# 相对自己平时活跃多少」，环比问「今天相对昨天突然放大了多少」。倍量阴、三倍量
# 这类形态问的全是后者。
DOUBLE_VOLUME = 2.0
TRIPLE_VOLUME = 3.0

VOLUME_BASELINE_WINDOW = 20
TREND_WINDOW = 5
# 「持续放量」的判据：**今天之前**那几天的均量相对 20 日均量。取 1.05 而不是 1.0
# 是要求前几天确实在放，而不只是刚好达到自己的平均值。见 `_expanding`。
EXPANSION_SUSTAIN = 1.05
FLOW_WINDOW = 20
DIVERGENCE_WINDOW = 20
DRY_UP_WINDOW = 3

# 天量：量比到这个倍数就不再是「放量确认」而是情绪透支。与
# `strategy/playbook.py:BREAKOUT_VOLUME_MAX`（4.0）刻意不同——那是**买入侧**的上限
# （超过就不算突破确认），这是**卖出侧**的触发（超过且滞涨就该减）。两个问题，
# 两个数，不该共用一个常量。
CLIMAX_RATIO = 3.0
# 天量还要在高位才算见顶。低位的天量是启动，不是滞涨。
CLIMAX_POSITION = 0.85
# 「滞涨」：收在当日区间的下半段。放了巨量却收不上去，说明买盘被卖盘吃完了。
CLIMAX_CLOSE_POSITION = 0.5

# 地量：连续 DRY_UP_WINDOW 日量比都在这个数以下。
DRY_UP_RATIO = 0.6

QUADRANT_UP_EXPAND = "放量上涨"
QUADRANT_UP_CONTRACT = "缩量上涨"
QUADRANT_UP_FLAT = "平量上涨"
QUADRANT_DOWN_EXPAND = "放量下跌"
QUADRANT_DOWN_CONTRACT = "缩量下跌"
QUADRANT_DOWN_FLAT = "平量下跌"
QUADRANT_FLAT = "横盘"

DIVERGENCE_TOP = "顶背离"
DIVERGENCE_BOTTOM = "底背离"

# 判据的**名字**收在上面，这两条还各有一句**定义**——同一个信号会由三层各自说出
# 来：量价读数（本模块 `describe`）、派发判定（`signals/distribution.py`）、卖点
# 判据（`strategy/exits.py`）。三处此前都 `== DIVERGENCE_TOP` 地共用名字，却把
# 后半句各写各的。
#
# 于是改名字三处一起变，改句子只有一处变——**同一份简报里同一个信号出现两种
# 说法**，而两种都不报错。名字和它的定义必须在同一处。
CLIMAX_STALL_TEXT = "天量滞涨：放巨量而收在日内下半段"
TOP_DIVERGENCE_TEXT = f"{DIVERGENCE_TOP}：价创新高而量没跟上"


@dataclass(frozen=True)
class VolumePriceFeatures:
    """一只票在某个交易日收盘后的量价画像。

    每个字段都可能是 None——数据不足时返回 None 而不是 0。「没算出来」和「算出来
    是零」在事后统计里必须能区分开，这是 `signals/dipbuy.py` 立的规矩。
    """

    ret_1d: float | None = None
    vol_ratio_20d: float | None = None

    # 日环比量能：当日量 ÷ 前一日量。倍量 / 三倍量看的是这个，不是上面那个。
    vol_ratio_1d: float | None = None

    # 换手率。量比是自己跟自己比，跨票不可比；换手率才可比。
    turnover_rate: float | None = None
    turnover_rate_20d: float | None = None

    # 四象限标签。**只说事实**：涨了还是跌了、放量还是缩量。
    quadrant: str | None = None

    # 量能趋势：近 5 日量比序列的斜率。正=持续放量，负=持续缩量。
    # **判「是不是单日脉冲」不要用它**，用 `expanding`——理由见 `_expanding`。
    vol_trend_5d: float | None = None
    expanding: bool | None = None

    # 方向性成交量代理。**不是主动买卖单**，见模块 docstring。
    up_down_vol_ratio: float | None = None

    divergence: str | None = None

    climax: bool | None = None
    # 天量滞涨：放巨量且收在日内下半段。这是卖出侧触发，不是买入侧。
    climax_stall: bool | None = None
    dry_up: bool | None = None
    # 放量跌破 MA20——和「缩量回踩」正好相反的那一面。
    breakdown_on_volume: bool | None = None

    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> list[str]:
        """能直接贴进简报的读数行。只陈述，不判断。"""
        lines: list[str] = []
        if self.quadrant and self.vol_ratio_20d is not None:
            text = f"{self.quadrant}（量比 {self.vol_ratio_20d:.2f}"
            if self.turnover_rate is not None:
                text += f"、换手 {self.turnover_rate:.2%}"
            lines.append(text + "）")
        if self.divergence:
            lines.append(f"{self.divergence}：价与量不同步")
        if self.vol_ratio_1d is not None and self.vol_ratio_1d >= DOUBLE_VOLUME:
            label = "三倍量" if self.vol_ratio_1d >= TRIPLE_VOLUME else "倍量"
            lines.append(f"{label}：较前一日放大 {self.vol_ratio_1d:.2f} 倍")
        if self.climax_stall:
            lines.append(CLIMAX_STALL_TEXT)
        elif self.climax:
            lines.append("天量：量比已达情绪透支区间")
        if self.dry_up:
            lines.append(f"地量：连续 {DRY_UP_WINDOW} 日量比低于 {DRY_UP_RATIO}")
        if self.up_down_vol_ratio is not None:
            lines.append(
                f"近 {FLOW_WINDOW} 日涨跌量比 {self.up_down_vol_ratio:.2f}"
                "（日线口径的方向性成交量，非逐笔主动买卖）"
            )
        return lines


def measure_volume_price(
    bars: list[dict],
    snapshot: Any = None,
    float_shares: float | None = None,
) -> VolumePriceFeatures:
    """从前复权日线算量价画像。

    `snapshot` 可选（`DailyTechnicalSnapshot`）——用它已经算好的 `volume_ratio_20d`
    与 `range_position_60d`，不重复造轮子；给 None 时本地算一份等价的。

    `float_shares` 可选（流通股本，单位股）——只有它在时才给换手率。取不到就是
    None，不拿总股本或别的东西顶替。

    任何情况下都不抛异常：一只票算不出量价特征，不该让整条链路停下来。
    """
    notes: list[str] = []
    closes = series(bars, "close")
    if len(closes) < 2:
        return VolumePriceFeatures(notes=["日线不足 2 根，量价读数全部跳过"])

    volumes = _normalised_volumes(bars)
    if volumes is None:
        # 单位反推不出来时全部弃权。用错量纲的换手率比没有换手率危险得多。
        notes.append("成交量单位无法从 amount/(volume×close) 反推，本次不给量价读数")
        return VolumePriceFeatures(ret_1d=change(closes, 1), notes=notes)

    ret_1d = change(closes, 1)
    ratio = _volume_ratio(volumes, snapshot)
    turnover, turnover_20d = _turnover(volumes, float_shares, notes)
    # 天量与天量滞涨看的是同一个 60 日分位，算一次就够——`_range_position` 在没有
    # snapshot 时要扫 60 根 K 线的高低点。
    position = _range_position(bars, snapshot)

    features = VolumePriceFeatures(
        ret_1d=ret_1d,
        vol_ratio_20d=ratio,
        vol_ratio_1d=_volume_ratio_1d(volumes),
        turnover_rate=turnover,
        turnover_rate_20d=turnover_20d,
        quadrant=_quadrant(ret_1d, ratio),
        vol_trend_5d=_volume_trend(volumes),
        expanding=_expanding(volumes),
        up_down_vol_ratio=_up_down_volume(closes, volumes),
        divergence=_divergence(closes, volumes),
        climax=_climax(ratio, position),
        climax_stall=_climax_stall(bars, ratio, position),
        dry_up=_dry_up(volumes),
        breakdown_on_volume=_breakdown(closes, ret_1d, ratio, snapshot),
        notes=notes,
    )
    return features


# ---- 各读数 ---------------------------------------------------------------


def _quadrant(ret_1d: float | None, ratio: float | None) -> str | None:
    """量价四象限。**标签只说事实，含义交给判据链。**

    同一个「放量上涨」，在 60 日分位 0.2 处是启动，在 0.95 处是派发——这里不区分，
    因为区分它需要位置信息，而位置属于另一个读数。混进来会让这个标签变成一个
    半成品的结论。
    """
    if ret_1d is None or ratio is None:
        return None
    if abs(ret_1d) < 0.002:
        return QUADRANT_FLAT
    if ret_1d > 0:
        if ratio >= EXPANSION_RATIO:
            return QUADRANT_UP_EXPAND
        if ratio <= CONTRACTION_RATIO:
            return QUADRANT_UP_CONTRACT
        return QUADRANT_UP_FLAT
    if ratio >= EXPANSION_RATIO:
        return QUADRANT_DOWN_EXPAND
    if ratio <= CONTRACTION_RATIO:
        return QUADRANT_DOWN_CONTRACT
    return QUADRANT_DOWN_FLAT


def _volume_trend(volumes: list[float]) -> float | None:
    """近 5 日量能的最小二乘斜率，除以均量做归一。

    归一是必须的：同一条「每天多放 10 万手」的斜率，对日均 20 万手的票是暴增，
    对日均 2000 万手的票是噪声。除掉均量之后不同票之间才可比。
    """
    if len(volumes) < VOLUME_BASELINE_WINDOW + TREND_WINDOW:
        return None
    baseline = mean(volumes[-VOLUME_BASELINE_WINDOW:])
    if not baseline:
        return None

    window = volumes[-TREND_WINDOW:]
    n = len(window)
    mean_x = (n - 1) / 2.0
    mean_y = sum(window) / n
    denominator = sum((index - mean_x) ** 2 for index in range(n))
    if denominator <= 0:
        return None
    slope = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(window))
    return round(slope / denominator / baseline, 4)


def _expanding(volumes: list[float]) -> bool | None:
    """量能是不是**持续**在放，而不是今天单放一根。

    **不能用 `vol_trend_5d > 0` 判这件事**，虽然那是最自然的写法。最小二乘斜率在
    5 根这么短的窗口上会被末端的一个离群值整个拽正：量能序列 [1,1,1,1,3] 的斜率
    是 +0.4，和真正逐日爬升的 [1.4,1.8,2.2,2.6,3.0] 同号——而这两者恰恰是这个
    函数唯一要区分的两种情况。第一版就是这么写的，测试直接把它抓了出来。

    改成看**今天之前那几天**：单日脉冲的前几天贴着自己的均量（比值≈1），持续放量
    的前几天已经在均量上方。today 完全不参与判断，所以再大的单日量也影响不了它。
    """
    if len(volumes) < VOLUME_BASELINE_WINDOW + TREND_WINDOW:
        return None
    baseline = mean(volumes[-VOLUME_BASELINE_WINDOW:])
    prior = mean(volumes[-TREND_WINDOW:-1])
    if not baseline or prior is None:
        return None
    return prior / baseline >= EXPANSION_SUSTAIN


def _up_down_volume(closes: list[float], volumes: list[float]) -> float | None:
    """近 20 日上涨日成交量之和 ÷ 下跌日成交量之和。

    **不是主动买卖单**，见模块 docstring。任一侧为空时返回 None 而不是一个很大的
    数——「20 天没有一根阴线」是一个该被看见的事实，把它压成 `ratio=999` 会让它
    混进普通读数里。
    """
    if len(closes) < FLOW_WINDOW + 1 or len(volumes) < FLOW_WINDOW + 1:
        return None

    up_volume = 0.0
    down_volume = 0.0
    for index in range(len(closes) - FLOW_WINDOW, len(closes)):
        previous = closes[index - 1]
        if previous <= 0:
            continue
        change = closes[index] / previous - 1.0
        if change > 0:
            up_volume += volumes[index]
        elif change < 0:
            down_volume += volumes[index]

    if up_volume <= 0 or down_volume <= 0:
        return None
    return round(up_volume / down_volume, 4)


def _divergence(closes: list[float], volumes: list[float]) -> str | None:
    """价创新高/新低，量却没有跟上。

    判据是**和上一次极值那天比量**，不是和均量比：均量会被中间那些平淡的日子拉平，
    而背离问的恰恰是「这一次的推动力比上一次弱」。
    """
    if len(closes) < DIVERGENCE_WINDOW + 1 or len(volumes) != len(closes):
        return None

    window = closes[-DIVERGENCE_WINDOW:]
    volume_window = volumes[-DIVERGENCE_WINDOW:]
    last = window[-1]
    previous = window[:-1]
    if not previous:
        return None

    if last >= max(previous):
        peak_index = max(range(len(previous)), key=lambda i: previous[i])
        if volume_window[-1] < volume_window[peak_index]:
            return DIVERGENCE_TOP
        return None

    if last <= min(previous):
        trough_index = min(range(len(previous)), key=lambda i: previous[i])
        if volume_window[-1] < volume_window[trough_index]:
            return DIVERGENCE_BOTTOM
    return None


def _climax(ratio: float | None, position: float | None) -> bool | None:
    """天量。**必须叠加高位**——低位的天量是启动，不是见顶。"""
    if ratio is None or position is None:
        return None
    return ratio >= CLIMAX_RATIO and position >= CLIMAX_POSITION


def _climax_stall(bars: list[dict], ratio: float | None, position: float | None) -> bool | None:
    """天量滞涨：放巨量、在高位、且收在当日区间的下半段。

    这是五类持仓卖点（`monitor/holdings.py`）都不会响的那一类：止损远在下方、
    目标没到、支撑没破、期限没到、浮盈也还在——但买盘已经在这一天被吃完了。
    """
    climax = _climax(ratio, position)
    if not climax:
        return climax
    if not bars:
        return None
    # 一字板在 `close_position` 里返回 None（日内位置无定义），于是这里判 None
    # 而不是把一字涨停当成中性收盘——那正是这条判据最不该误报的一天。
    within_day = close_position(bars[-1])
    if within_day is None:
        return None
    return within_day <= CLIMAX_CLOSE_POSITION


def _dry_up(volumes: list[float]) -> bool | None:
    """地量：连续若干日量比都在阈值以下。"""
    if len(volumes) < VOLUME_BASELINE_WINDOW + DRY_UP_WINDOW:
        return None
    for offset in range(1, DRY_UP_WINDOW + 1):
        end = len(volumes) - offset + 1
        baseline = mean(volumes[end - 1 - VOLUME_BASELINE_WINDOW : end - 1])
        if not baseline:
            return None
        if volumes[end - 1] / baseline > DRY_UP_RATIO:
            return False
    return True


def _breakdown(
    closes: list[float],
    ret_1d: float | None,
    ratio: float | None,
    snapshot: Any,
) -> bool | None:
    """放量下跌且收盘跌破 MA20。

    「缩量回踩不破 MA20」是买点（`playbook._check_pullback`），这是它的镜像：
    同一条均线，放着量跌穿，是最该无条件离场的形态之一。
    """
    if ret_1d is None or ratio is None or ret_1d >= 0:
        return None
    ma20 = getattr(snapshot, "ma20", None)
    if ma20 is None:
        if len(closes) < VOLUME_BASELINE_WINDOW:
            return None
        ma20 = mean(closes[-VOLUME_BASELINE_WINDOW:])
    if not ma20 or ma20 <= 0:
        return None
    return ratio >= EXPANSION_RATIO and closes[-1] < ma20


# ---- helpers -------------------------------------------------------------


def _normalised_volumes(bars: list[dict]) -> list[float] | None:
    """整条序列归一成股。**任何一根推不出单位就整条弃权**——半条归一化的序列
    比没有归一化更危险，因为量比会在换单位的那一天凭空跳 100 倍。"""
    values: list[float] = []
    for row in bars or []:
        volume = normalised_volume(row)
        if volume is None:
            return None
        values.append(volume)
    return values or None


def _volume_ratio_1d(volumes: list[float]) -> float | None:
    """日环比量能：当日量 ÷ 前一日量。

    `volumes` 到这里已经过 `_normalised_volumes` 归一，而那一步对任何一根
    `volume <= 0` 的 K 线（停牌日就是这种）都会让**整条序列**弃权，所以除零在主
    链路上到不了这里。留着 `previous <= 0` 这道判断，是因为本函数只依赖入参、不
    依赖调用顺序——把不变量写在依赖它的地方，比写在注释里可靠。
    """
    if len(volumes) < 2:
        return None
    previous = volumes[-2]
    if previous <= 0:
        return None
    return round(volumes[-1] / previous, 4)


def _volume_ratio(volumes: list[float], snapshot: Any) -> float | None:
    existing = getattr(snapshot, "volume_ratio_20d", None)
    if existing is not None:
        return float(existing)
    if len(volumes) < VOLUME_BASELINE_WINDOW + 1:
        return None
    baseline = mean(volumes[-(VOLUME_BASELINE_WINDOW + 1) : -1])
    if not baseline:
        return None
    return round(volumes[-1] / baseline, 4)


def _turnover(
    volumes: list[float], float_shares: float | None, notes: list[str]
) -> tuple[float | None, float | None]:
    """换手率与其 20 日均值。

    口径是**流通股本**。有限售股的票在这个口径下换手率会系统性偏高，所以这条
    必须写进 notes 而不是只留在文档里——读简报的人看到的是数字，不是文档。
    """
    shares = finite(float_shares)
    if shares is None or shares <= 0:
        return None, None
    notes.append("换手率按流通股本计算；有限售股的标的该口径偏高")
    latest = round(volumes[-1] / shares, 6)
    average = None
    if len(volumes) >= VOLUME_BASELINE_WINDOW:
        mean_volume = mean(volumes[-VOLUME_BASELINE_WINDOW:])
        average = None if not mean_volume else round(mean_volume / shares, 6)
    return latest, average


def _range_position(bars: list[dict], snapshot: Any) -> float | None:
    existing = getattr(snapshot, "range_position_60d", None)
    if existing is not None:
        return float(existing)
    window = (bars or [])[-60:]
    highs = [value for value in (finite(row.get("high")) for row in window) if value is not None]
    lows = [value for value in (finite(row.get("low")) for row in window) if value is not None]
    close = finite(window[-1].get("close")) if window else None
    if not highs or not lows or close is None:
        return None
    high, low = max(highs), min(lows)
    if high <= low:
        return 0.5
    return round(max(0.0, min((close - low) / (high - low), 1.0)), 4)
