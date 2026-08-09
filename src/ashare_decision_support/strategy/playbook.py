"""趋势分支：这只票**能用哪些打法**，以及每条打法的入场判据。

改造前趋势是权重（UPTREND +12 / DOWNTREND −15），不是分支。权重的问题是它可以
被别的项买回来——下跌趋势扣 15 分，只要威科夫和盈亏比加够，照样进候选。所以
「下跌趋势什么样的可以买」这个问题从来没有被回答，它只是被打了个折。

现在每种趋势走**不同的判据链**：

```text
UPTREND    突破买（创新高 + 放量）      回踩买（缩量回踩不破 MA20）
REBOUND    只允许回踩买
RANGE      只允许区间下沿买
DOWNTREND  只允许超跌反转——且必须有反转触发，跌得多本身不是理由
```

**最要紧的一处修正在 PULLBACK。** 改造前「缩量」只是 ±6 分的权重，压不过趋势
与威科夫的 50 分，结果 2026-08-03 的 60 条建议里 52 条是**放量回调**——而模块
自己写着「回调放量意味着出货」。现在缩量是**门槛**：`volume_contraction ≤ 0.90`
不过就不是回踩，无论别的项多好看。

0.90 这个数标定自 2026-08-03 当日 120 只入围票的分布（中位数 1.066，即中位候选
其实是**放量**的）：≤0.75 只剩 13%、≤0.90 剩 25%、≤1.0 剩 38%。取 0.90 是为了
挡掉明确放量的那 31%（>1.2）同时留下够填满候选池的样本。**这是先验校准，不是
收益验证**——哪个切点真的更值得买，要等回测和台账回答。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.models.enums import Playbook, TrendState

# 趋势 → 允许的打法。DOWNTREND 只留一条，且那一条自带触发要求。
TREND_PLAYBOOKS: dict[TrendState, frozenset[Playbook]] = {
    TrendState.UPTREND: frozenset({Playbook.BREAKOUT, Playbook.PULLBACK}),
    TrendState.REBOUND: frozenset({Playbook.PULLBACK}),
    TrendState.RANGE: frozenset({Playbook.RANGE_LOW}),
    TrendState.DOWNTREND: frozenset({Playbook.REVERSAL}),
}

# 同时命中多条时报哪一条。回踩排在突破前面：两者都成立时回踩的止损距离更短，
# 而这套系统给不出概率，只能在风险侧择优。
_PRIORITY = (Playbook.PULLBACK, Playbook.BREAKOUT, Playbook.RANGE_LOW, Playbook.REVERSAL)

# --- PULLBACK 回踩 ---
# 缩量门槛。见模块 docstring 的标定说明。
PULLBACK_CONTRACTION_MAX = 0.90
# 更严的一档，只作为质量标注，不参与门槛。等同 `signals/dipbuy.py:CONTRACTION_FULL`。
PULLBACK_CONTRACTION_GOOD = 0.75

# --- BREAKOUT 突破 ---
BREAKOUT_POSITION_MIN = 0.85
BREAKOUT_VOLUME_MIN = 1.2
# 超过这个量比是情绪透支不是突破确认，两端都要挡。
BREAKOUT_VOLUME_MAX = 4.0

# --- RANGE_LOW 区间下沿 ---
RANGE_POSITION_MAX = 0.35
RANGE_VOLUME_MAX = 1.2

# --- REVERSAL 超跌反转 ---
# 反包所需的放量程度。反转必须有量，缩量阴跌不是反转。
REVERSAL_VOLUME_MIN = 1.2

# --- 量价门槛（`signals/volume_price.py`）---
#
# 这几条补的是「放量上涨 / 缩量下跌」里此前没有判据的那一半。它们全部做**门槛**
# 不做权重，理由和缩量门槛完全一样：dip 在打分里占 10/85，靠加权永远压不过
# 「趋势强、位置高」的票。
#
# 回踩要求近 20 日上涨日的量不小于下跌日的量。低于 1.0 意味着这段回调是**放着量
# 砸下来**的，即使今天单日缩量也一样——单日量比看不到这件事。
PULLBACK_UP_DOWN_VOL_MIN = 1.0
# 突破要求量能是**持续**放大而不是单日脉冲。斜率读数见 `_volume_trend`。
BREAKOUT_REQUIRES_EXPANSION = True


@dataclass(frozen=True)
class SetupVerdict:
    """一条打法在一只票上成不成立，以及为什么。"""

    playbook: Playbook
    ok: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "playbook": self.playbook.value,
            "ok": self.ok,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PlaybookMatch:
    """一只票在今天允许的打法集合下的评估结果。"""

    trend: TrendState | None
    matched: list[SetupVerdict] = field(default_factory=list)
    rejected: list[SetupVerdict] = field(default_factory=list)
    # 被情绪周期挡掉的打法：这只票的趋势本来允许，但今天不允许。
    blocked_by_regime: list[Playbook] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.matched)

    @property
    def primary(self) -> Playbook | None:
        """命中的打法里优先级最高的那条。"""
        hit = {item.playbook for item in self.matched}
        return next((item for item in _PRIORITY if item in hit), None)

    @property
    def reasons(self) -> list[str]:
        return [text for item in self.matched for text in item.reasons]

    @property
    def warnings(self) -> list[str]:
        return [text for item in self.matched for text in item.warnings]

    @property
    def reject_reasons(self) -> list[str]:
        """**这只票为什么不能用这条打法。**

        每条拒绝理由本身就是一句可读的回答——「超跌但无反转触发（无 Spring、无
        放量反包、未收回 30 日成交成本）——只是跌得多，属于接刀」。这是低吸系统
        最该说出口的一句话，而它此前只活在内存里：`to_dict()` 不带它，于是台账、
        问答、盘后卡三处都看不到，人只能看到「这只票没入选」。
        """
        return [text for item in self.rejected for text in item.reasons]

    def to_dict(self) -> dict:
        primary = self.primary
        return {
            "trend": self.trend.value if self.trend else None,
            "primary": primary.value if primary else None,
            "matched": [item.to_dict() for item in self.matched],
            # 命中的打法也带上未命中的那些：「为什么是回踩不是突破」在事后复盘时
            # 和「为什么一条都不成立」同样要紧，而两者是同一份数据。
            "rejected": [item.to_dict() for item in self.rejected],
            "blocked_by_regime": sorted(item.value for item in self.blocked_by_regime),
        }


def playbooks_for_trend(trend: TrendState | None) -> frozenset[Playbook]:
    if trend is None:
        return frozenset()
    return TREND_PLAYBOOKS.get(trend, frozenset())


def evaluate_playbooks(
    snapshot: Any,
    dip: Any = None,
    wyckoff: Any = None,
    allowed: frozenset[Playbook] | None = None,
    volume_price: Any = None,
) -> PlaybookMatch:
    """交叉趋势允许的打法与今天允许的打法，对留下的逐条验判据。

    `allowed` 为 None 表示不施加情绪周期约束（`advise` 单票分析走这条路——人工
    问一只票时该看到完整判断，不该被今天的大盘状态静默截断）。

    `volume_price` 为 None 时量价门槛**全部放行并标注**，不是判负。取不到读数
    和读数不合格是两件事，用「没测到」当否决理由等于让数据缺失冒充风险判断
    （同 `jobs/morning.py:_market_scan` 的口径）。
    """
    trend = getattr(snapshot, "trend_state", None)
    by_trend = playbooks_for_trend(trend)
    if allowed is None:
        candidates, blocked = by_trend, frozenset()
    else:
        candidates = by_trend & allowed
        blocked = by_trend - allowed

    matched: list[SetupVerdict] = []
    rejected: list[SetupVerdict] = []
    for playbook in _PRIORITY:
        if playbook not in candidates:
            continue
        verdict = _CHECKS[playbook](snapshot, dip, wyckoff, volume_price)
        (matched if verdict.ok else rejected).append(verdict)

    return PlaybookMatch(
        trend=trend,
        matched=matched,
        rejected=rejected,
        blocked_by_regime=sorted(blocked, key=_PRIORITY.index),
    )


# ---- 各打法的判据链 -------------------------------------------------------


def _check_pullback(snapshot: Any, dip: Any, wyckoff: Any, vp: Any = None) -> SetupVerdict:
    """回踩买：趋势还在 × 回调到位 × **缩量** × 这段回调不是放量砸的。

    四个条件全是门槛。改造前只有第一个是门槛，后面是权重——那正是选出来的票
    87% 在放量回调的原因。
    """
    reasons: list[str] = []
    warnings: list[str] = []

    band = getattr(dip, "pullback_band", None)
    if band not in ("shallow", "ideal"):
        detail = "尚未回调" if band == "none" else f"回调档位 {band or '未知'}"
        return _no(Playbook.PULLBACK, f"{detail}，不是回踩位置")

    contraction = getattr(dip, "volume_contraction", None)
    if contraction is None:
        return _no(Playbook.PULLBACK, "量能数据不足，无法确认回调是缩量还是放量")
    if contraction > PULLBACK_CONTRACTION_MAX:
        return _no(
            Playbook.PULLBACK,
            f"回调放量 {contraction:.2f}（>{PULLBACK_CONTRACTION_MAX}），"
            "抛压未收敛，疑似出货",
        )

    # 单日/近三日缩量看不见「这一整段是怎么跌下来的」。近 20 日涨跌量比补的正是
    # 那一段：今天缩量、但整段是放着量砸下来的，那不是回踩，是出货途中的喘息。
    flow = getattr(vp, "up_down_vol_ratio", None)
    if flow is not None and flow < PULLBACK_UP_DOWN_VOL_MIN:
        return _no(
            Playbook.PULLBACK,
            f"近 20 日涨跌量比 {flow:.2f}（<{PULLBACK_UP_DOWN_VOL_MIN}），"
            "跌的时候比涨的时候放量，这段回调是被砸出来的",
        )

    distance = getattr(dip, "dist_to_ma20_pct", None)
    if distance is None:
        return _no(Playbook.PULLBACK, "无 MA20 读数，无法确认趋势结构是否完好")
    if distance < 0:
        # **收盘跌破 MA20 就不再是回踩，等站回去。** 这一条在盘前主链路上和
        # `TrendState` 是重合的（UPTREND / REBOUND 的定义本身就要求收在 MA20
        # 上方），留着是因为 `advise` 单票分析会传进别处构造的 snapshot。
        #
        # 这是一个**刻意的保守偏置**：一只强势票回踩到 MA20 下方 0.5% 就被判出
        # 局，其中有一部分次日会收回。放宽多少合适是个实证问题，回测框架建起来
        # 之后才有资格调——现在宁可漏，不愿在结构已破的位置给出买点。
        return _no(
            Playbook.PULLBACK,
            f"收盘已跌破 MA20{abs(distance):.1%}，回踩转为破位，等站回 MA20 再看",
        )

    pullback = getattr(dip, "pullback_pct", None) or 0.0
    depth = getattr(dip, "pullback_atr", None) or 0.0
    reasons.append(
        f"回踩：回调 {pullback:+.1%}（{depth:.1f} 个日波幅，{band}）、"
        f"缩量至 {contraction:.2f}、MA20 上方 {distance:+.1%}"
    )
    if flow is None:
        warnings.append("无涨跌量比读数，未能确认这段回调是不是放量砸出来的")
    elif flow >= 1.3:
        reasons.append(f"近 20 日涨跌量比 {flow:.2f}：上涨日明显比下跌日放量")
    if contraction <= PULLBACK_CONTRACTION_GOOD:
        reasons.append(f"缩量充分 {contraction:.2f}（≤{PULLBACK_CONTRACTION_GOOD}），回调质量好")
    if getattr(vp, "dry_up", False):
        reasons.append("地量：抛压已经枯竭")
    if getattr(wyckoff, "last_point_of_support", False):
        reasons.append("威科夫 LPS：回踩获得承接")
    if getattr(wyckoff, "spring", False):
        reasons.append("威科夫 Spring：假跌破后收回")
    return SetupVerdict(Playbook.PULLBACK, True, reasons, warnings)


def _check_breakout(snapshot: Any, dip: Any, wyckoff: Any, vp: Any = None) -> SetupVerdict:
    """突破买：贴近区间上沿 + **持续**放量确认，且没有放到透支、没有顶背离。

    这是系统此前结构上产不出的那一类买点（`advice/candidates.py` 直接不生成
    高于现价的买入价）。它只在 UPTREND 且情绪周期允许时才会被验到。
    """
    reasons: list[str] = []
    warnings: list[str] = []

    position = getattr(snapshot, "range_position_60d", None)
    if position is None:
        return _no(Playbook.BREAKOUT, "无 60 日区间分位，无法确认是否在突破位")
    if position < BREAKOUT_POSITION_MIN:
        return _no(
            Playbook.BREAKOUT,
            f"60 日分位 {position:.2f}（<{BREAKOUT_POSITION_MIN}），尚未到突破位",
        )

    ratio = getattr(snapshot, "volume_ratio_20d", None)
    if ratio is None:
        return _no(Playbook.BREAKOUT, "无量比读数，突破无法确认")
    if ratio < BREAKOUT_VOLUME_MIN:
        return _no(
            Playbook.BREAKOUT,
            f"量比 {ratio:.2f}（<{BREAKOUT_VOLUME_MIN}），无量突破不成立",
        )
    if ratio > BREAKOUT_VOLUME_MAX:
        return _no(
            Playbook.BREAKOUT,
            f"量比 {ratio:.2f}（>{BREAKOUT_VOLUME_MAX}）已是情绪透支，不是突破确认",
        )

    # 顶背离：价创新高而量比不上创前高那天。这是「突破」和「最后一冲」的分界，
    # 而单日量比对两者给出的读数完全一样。
    if getattr(vp, "divergence", None) == "顶背离":
        return _no(
            Playbook.BREAKOUT,
            "价创 20 日新高但量能低于前高那日（顶背离），推动力在衰减，不是有效突破",
        )

    expanding = getattr(vp, "expanding", None)
    if BREAKOUT_REQUIRES_EXPANSION and expanding is False:
        trend = getattr(vp, "vol_trend_5d", None)
        detail = f"（近 5 日量能斜率 {trend:+.3f}）" if trend is not None else ""
        return _no(
            Playbook.BREAKOUT,
            f"量能未持续放大{detail}，是单日脉冲不是放量突破",
        )

    band = getattr(dip, "pullback_band", None)
    if band in ("ideal", "deep"):
        return _no(
            Playbook.BREAKOUT,
            f"已从 20 日高回落至 {band} 档，不再是突破形态",
        )

    reasons.append(f"突破：60 日分位 {position:.2f}、放量 {ratio:.2f} 确认")
    if expanding:
        reasons.append("量能连续放大，不是单日脉冲")
    elif expanding is None:
        warnings.append("无量能趋势读数，未能确认这是持续放量还是单日脉冲")
    if getattr(wyckoff, "sign_of_strength", False):
        reasons.append("威科夫 SOS：放量上推突破")
    if getattr(wyckoff, "upthrust", False):
        warnings.append("威科夫 Upthrust 同时成立：可能是假突破，止损必须贴紧")
    warnings.append("突破买的止损在突破位下方，距离通常大于回踩买——仓位按此折算")
    return SetupVerdict(Playbook.BREAKOUT, True, reasons, warnings)


def _check_range_low(snapshot: Any, dip: Any, wyckoff: Any, vp: Any = None) -> SetupVerdict:
    """区间下沿买：震荡市唯一允许的买法，目标锁在区间上沿。"""
    reasons: list[str] = []
    warnings: list[str] = []

    position = getattr(snapshot, "range_position_60d", None)
    if position is None:
        return _no(Playbook.RANGE_LOW, "无 60 日区间分位，无法定位区间下沿")
    if position > RANGE_POSITION_MAX:
        return _no(
            Playbook.RANGE_LOW,
            f"60 日分位 {position:.2f}（>{RANGE_POSITION_MAX}），不在区间下沿",
        )

    contraction = getattr(dip, "volume_contraction", None)
    if contraction is not None and contraction > RANGE_VOLUME_MAX:
        return _no(
            Playbook.RANGE_LOW,
            f"下沿放量 {contraction:.2f}（>{RANGE_VOLUME_MAX}），疑似向下破位而非支撑",
        )

    supports = [
        level
        for level in (getattr(snapshot, "supports", None) or [])
        if getattr(level, "price", None)
        and float(level.price) < float(snapshot.last_close)
    ]
    if not supports:
        return _no(Playbook.RANGE_LOW, "下方无有效支撑，区间下沿无法界定")

    reasons.append(f"区间下沿：60 日分位 {position:.2f}，下方有 {len(supports)} 档支撑")
    # 地量在区间下沿是加分项而不是门槛：它是好事，但要求它会把大多数可做的
    # 下沿位置挡在外面。
    if getattr(vp, "dry_up", False):
        reasons.append("地量：下沿抛压枯竭")
    if getattr(vp, "divergence", None) == "底背离":
        reasons.append("底背离：价创新低而量能萎缩，卖压在衰竭")
    warnings.append("震荡市买法，目标锁区间上沿——不按趋势票的持有期拿")
    return SetupVerdict(Playbook.RANGE_LOW, True, reasons, warnings)


def _check_reversal(snapshot: Any, dip: Any, wyckoff: Any, vp: Any = None) -> SetupVerdict:
    """超跌反转：下跌趋势里唯一允许的买法。

    **跌得多本身不是买入理由。** 必须同时有一个反转触发，否则就是接刀。四种触发
    任一成立即可，它们都是「卖方衰竭并被买方接管」的不同表现：

      Spring          假跌破后收回，威科夫口径的衰竭
      放量反包        当日放量收阳，卖压被吃掉
      踩回成本收回    盘中跌破 30 日成交成本、收盘站回
      地量后首阳      地量见地之后第一根放量阳线

    没有触发时明确拒绝，并把理由写出来——这条拒绝理由本身就是回答「这只跌了很多
    的票为什么不买」。
    """
    reasons: list[str] = []
    warnings: list[str] = []

    band = getattr(dip, "pullback_band", None)
    if band != "deep":
        return _no(
            Playbook.REVERSAL,
            f"回调档位 {band or '未知'}，未进入超跌区，下跌趋势中不构成反转位",
        )

    spring = bool(getattr(wyckoff, "spring", False))
    ret_1d = getattr(dip, "ret_1d", None)
    contraction = getattr(dip, "volume_contraction", None)
    engulf = (
        ret_1d is not None
        and ret_1d > 0
        and contraction is not None
        and contraction >= REVERSAL_VOLUME_MIN
    )
    reclaimed = bool(getattr(dip, "turnover_cost_30_reclaimed", False))
    # 地量见地 → 放量阳线，是经典的衰竭反转序列。地量本身**不算触发**——它只
    # 说明没人卖了，不说明有人买。
    dry_up_turn = bool(getattr(vp, "dry_up", False)) and (
        getattr(vp, "quadrant", None) == "放量上涨"
    )

    if not (spring or engulf or reclaimed or dry_up_turn):
        return _no(
            Playbook.REVERSAL,
            "超跌但无反转触发（无 Spring、无放量反包、未收回 30 日成交成本、"
            "无地量后首阳）——只是跌得多，属于接刀",
        )

    if spring:
        reasons.append("反转触发：威科夫 Spring，假跌破后收回")
    if engulf:
        reasons.append(f"反转触发：放量反包，当日 {ret_1d:+.1%} 且量能 {contraction:.2f}")
    if reclaimed:
        reasons.append("反转触发：盘中跌破 30 日成交成本后收盘站回")
    if dry_up_turn:
        reasons.append("反转触发：地量见地之后首根放量阳线")

    depth = getattr(dip, "pullback_atr", None) or 0.0
    reasons.append(f"超跌 {depth:.1f} 个日波幅（deep 档）")
    warnings.append(
        "逆势买入：仓位减半，目标只看前高的一半，跌破触发当日最低即无条件离场"
    )
    return SetupVerdict(Playbook.REVERSAL, True, reasons, warnings)


def _no(playbook: Playbook, reason: str) -> SetupVerdict:
    return SetupVerdict(playbook, False, [reason])


_CHECKS = {
    Playbook.PULLBACK: _check_pullback,
    Playbook.BREAKOUT: _check_breakout,
    Playbook.RANGE_LOW: _check_range_low,
    Playbook.REVERSAL: _check_reversal,
}
