"""台账中的建议与兑现记录。

recommendation 保存入选判断，control 保存相邻落选对照，vetoed 保存业绩否决样本。
三组共用兑现流程，由 Outcome.rec_kind 保留归属，用于比较排序与风险闸门效果。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from ashare_decision_support.signals.series import parse_bar_date
from ashare_decision_support.symbols import normalize_symbol

KIND_RECOMMENDATION = "recommendation"
KIND_CONTROL = "control"
KIND_VETOED = "vetoed"
KIND_OUTCOME = "outcome"

# 会被兑现流程核对的三类记录。对照组与否决组必须在内，否则它们记了也白记。
VERIFIABLE_KINDS = (KIND_RECOMMENDATION, KIND_CONTROL, KIND_VETOED)
# v2：买入区间上沿核算风险、门槛用未舍入值、缺失情绪读数不补零。
# 已有台账中的版本原样保留；新建议另行分组，便于比较规则变更前后表现。
DEFAULT_STRATEGY_VERSION = "rule-v2"


@dataclass
class Recommendation:
    """一条建议在发出当时的完整状态。

    价位存两份：`*_price` 是当时报出的不复权价，给人看；`*_pct` 是相对参考价的
    百分比，给核对用。核对只读百分比——见模块 docstring。
    """

    symbol: str
    trade_date: str
    source: str  # morning | advise | monitor
    name: str | None = None
    sector: str | None = None
    score: float | None = None
    reference_price: float | None = None
    entry_low_price: float | None = None
    entry_high_price: float | None = None
    stop_price: float | None = None
    target_prices: list[float] = field(default_factory=list)
    entry_low_pct: float | None = None
    entry_high_pct: float | None = None
    stop_pct: float | None = None
    target_pcts: list[float] = field(default_factory=list)
    rrr: float | None = None
    trend_state: str | None = None
    wyckoff_bias: str | None = None
    # 这条建议走的是哪套打法（回踩 / 突破 / 区间下沿 / 超跌反转），以及判据链的
    # 明细。**必须在采集之前定好**：没有它，几个月后台账里会有几千条记录却无法
    # 回答「回踩和突破哪个兑现率高」——而那两类的止损距离和持有期本来就不同，
    # 混在一起算出的平均兑现率不描述其中任何一类。
    playbook: str | None = None
    setup: dict[str, Any] = field(default_factory=dict)
    # 建议发出当时的个股舆情读数。存下来是为了将来能回答一个现在答不上来的问题：
    # 带正面舆情的建议兑现率是不是真的更高。样本够之前不参与任何加权。
    news_polarity: float | None = None
    news_intensity: float | None = None
    news_doc_count: int | None = None
    # 交叉因子拆分。保存每项贡献，而不是只保存合成分数，复盘时才能回答
    # 「这套规则是靠板块关系、资金流还是业绩催化选对的」。
    score_components: dict[str, float] = field(default_factory=dict)
    factor_context: dict[str, Any] = field(default_factory=dict)
    # 低吸画像（`signals/dipbuy.py`）。回撤深度、缩量程度、距支撑距离——这几个数
    # 决定了「这是不是一个低吸位置」，而它们此前完全没有被记录过。
    dip: dict[str, Any] = field(default_factory=dict)
    # 建议发出当时的业绩读数（`adapters/earnings.py`）。

    # **不存它，业绩层就是不可验证的。** 上面 `score_components` 的注释里写着要能
    # 回答「这套规则是靠板块关系、资金流还是业绩催化选对的」，而业绩催化那一项
    # 在这个字段建起来之前根本没进过台账——`screens/earnings.py` 刻意只标注不加分，
    # 于是它对结果的影响**完全不体现在分数里**，只体现在这里。

    # 存的是裁决而不是原始预告：`kind`（预增/首亏…）、`metric`（哪个口径）、
    # `change_pct`、`is_fresh`。有了它，「带新鲜大幅预增的建议兑现率高多少」
    # 是一个 60 个交易日后能用 `journal/stats.py` 分组回答的问题。
    earnings: dict[str, Any] = field(default_factory=dict)
    # 建议发出当时的**上一交易日逐笔资金**（`adapters/tencent_detail.py`）：
    # 主动买占比、大单净额、尾盘 30 分钟净额。

    # 与上面 `earnings` 完全同一条理由——**它刻意不进分数，所以台账是它唯一的
    # 可验证出口**。不存的话，「大单净额为正的候选兑现率是不是更高」这个问题
    # 永远问不出口，于是这一层要么永远停在旁证状态，要么某天凭感觉被提成因子。

    # 存摘要不存逐笔：4000+ 笔明细进 JSONL 会把台账撑爆，而分组统计要的只是
    # 那几个汇总数。方向是腾讯按盘口的推断（非交易所委托方向），这一条写在
    # 适配器里，用这个字段做统计时必须记得。
    trade_flow: dict[str, Any] = field(default_factory=dict)
    # 建议发出当时的**大盘状态**。此前台账 24 个字段全是个股维度，一个都没记
    # 当天市场什么样，后果是三个月后有了几百条记录却**无法按情绪周期分层**——
    # 而这恰恰是超短最重要的分层维度。同一套规则在退潮期和启动期的期望值可以
    # 是反号的，混在一起统计只会得到一个谁都不像的平均数。
    market_state: dict[str, Any] = field(default_factory=dict)
    # 买卖点的三层裁决记录。存的是**两套结论**：规则默认排序选的那组价，和
    # 模型裁决选的那组价。只存最终结论的话，「模型裁决比规则默认准不准」这个
    # 问题永远答不了——那正是加这一层时唯一没法在设计阶段回答的问题。

    # `basis` 尤其要存：它让统计能按**依据类别**分组（分时均价回踩 vs 五档买盘墙
    # vs 日线支撑），而不是只能按最终价格分。哪一类出处更靠谱是可以被测出来的。
    adjudication: dict[str, Any] = field(default_factory=dict)
    # 宏观链路只存可分组标签（主题 / 方向 / 预期差），不存整段叙事。
    macro_context: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    facts_hash: str | None = None
    kind: str = KIND_RECOMMENDATION
    decision_id: str | None = None
    recorded_at: str | None = None
    strategy_version: str = DEFAULT_STRATEGY_VERSION
    plan_id: str | None = None
    revision: int = 1
    strategy_revision: int = 1
    supersedes: str | None = None
    # 参考价格所在交易日。晨报是上一交易日收盘；分时建议用同日开盘价对齐
    # 复权尺度。参考日不等于计划执行日，核对窗口不能早于计划日。
    reference_date: str | None = None
    # 分时参考价与日线通过同日开盘价对齐复权尺度，不能把盘中最新价当成收盘价。
    reference_open_price: float | None = None

    @classmethod
    def from_advice(
        cls,
        advice: Any,
        trade_date: date,
        source: str,
        sector: str | None = None,
        facts: Any = None,
    ) -> Recommendation | None:
        """建。参考价缺失或为零时返回 None——没有参考价就算不出相对值，
        存一条无法核对的记录只会污染统计。"""
        reference = advice.last_price
        if not reference:
            return None

        def pct(value: float | None) -> float | None:
            return None if value is None else round(value / reference - 1.0, 6)

        return cls(
            symbol=advice.symbol,
            trade_date=str(trade_date),
            source=source,
            name=advice.name,
            sector=sector,
            score=advice.score,
            score_components=dict(getattr(advice, "score_components", {}) or {}),
            reference_price=reference,
            entry_low_price=advice.entry_low,
            entry_high_price=advice.entry_high,
            stop_price=advice.stop,
            target_prices=list(advice.targets),
            entry_low_pct=pct(advice.entry_low),
            entry_high_pct=pct(advice.entry_high),
            stop_pct=pct(advice.stop),
            target_pcts=[pct(level) for level in advice.targets if pct(level) is not None],
            rrr=advice.rrr,
            trend_state=advice.trend_state,
            wyckoff_bias=advice.wyckoff_bias,
            playbook=getattr(advice, "playbook", None),
            setup=dict(getattr(advice, "setup", {}) or {}),
            reasons=list(advice.reasons),
            warnings=list(advice.warnings),
            adjudication=_adjudication_record(advice),
            facts_hash=facts_hash(facts) if facts is not None else None,
            strategy_version=(facts.get("strategy_version") or DEFAULT_STRATEGY_VERSION)
            if isinstance(facts, dict)
            else DEFAULT_STRATEGY_VERSION,
            plan_id=facts.get("plan_id") if isinstance(facts, dict) else None,
            # 原始分钟开盘价在同一日内不变，可以与事后的复权日线开盘配对，
            # 不借用盘中建议发出之后的收盘价重设参考价格。
            reference_date=(
                str(advice.session_date) if getattr(advice, "session_date", None) else None
            ),
            reference_open_price=(getattr(advice, "intraday", {}) or {}).get("open"),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def record_key(row: Any) -> str:
    """以股票、日期、来源关联建议与兑现，新记录再按判断编号区分修订。

    保留旧记录的关联方式；编号重复或损坏时也不能把其他股票的记录视为已核对。
    输入为读回的 JSONL 行，写入与待核对查询共用此定义。
    """
    symbol = normalize_symbol(row.get("symbol") or "")
    day = parse_bar_date(row.get("trade_date")) or row.get("trade_date")
    base = f"{symbol}|{day}|{row.get('source')}"
    decision_id = row.get("decision_id")
    if isinstance(decision_id, str) and decision_id:
        return f"{base}|decision:{decision_id}"
    return base


def _adjudication_record(advice: Any) -> dict[str, Any]:
    """把三层裁决压成几个可统计的字段。

    存 `basis` 和 `price` 两个维度，不存整个候选集：候选集在一条记录里能到几十
    个数，而台账是按月全读的 JSONL，体积会直接反映成每次统计的耗时。真要回溯
    完整候选集，`facts_hash` 指向的那份 facts 里有。
    """
    verdict = getattr(advice, "adjudication", None)
    if verdict is None:
        return {}

    def leg(candidate: Any) -> dict[str, Any] | None:
        if candidate is None:
            return None
        return {"price": candidate.price, "basis": candidate.basis}

    return {
        "abstained": verdict.abstained,
        "used_fallback": verdict.used_fallback,
        "overrode_rule": verdict.overrode_rule,
        "reason": verdict.reason or None,
        "chosen_entry": leg(verdict.entry),
        "chosen_stop": leg(verdict.stop),
        "chosen_target": leg(verdict.target),
        "rule_entry": leg(verdict.rule_entry),
        "rule_stop": leg(verdict.rule_stop),
        "rule_target": leg(verdict.rule_target),
    }


@dataclass
class Outcome:
    """建议发出后 N 个交易日里实际发生了什么。

    **两套口径并存，因为持有期本身还没有定论。** T+5 的 MFE/MAE 对应低吸的
    自然持有期（等回调结束→反弹到前高，通常 2–5 日）；`next_*` 三个数对应
    「次日就卖」。哪一种更适合这套规则，现在拍板都是猜——两个都记下来，等
    样本够了让数据回答。

    `next_open_pct` 单独拿出来是有原因的：持仓过夜的收益里，隔夜跳空往往比
    日内择时贡献大得多。把跳空和日内分开记，才能归因到底是选股选对了，还是
    单纯吃到了普涨。
    """

    symbol: str
    trade_date: str
    source: str
    resolved_on: str
    sessions: int
    entered: bool
    stop_hit: bool
    target_hit: bool
    first_hit: str | None  # stop | target | None
    mfe_pct: float | None  # 最大有利偏移
    mae_pct: float | None  # 最大不利偏移
    close_pct: float | None  # 期末收益
    # T+1 口径：隔夜跳空、次日最高（当天能卖到的最好价）、次日收盘
    next_open_pct: float | None = None
    next_high_pct: float | None = None
    next_low_pct: float | None = None
    next_close_pct: float | None = None
    score: float | None = None
    sector: str | None = None
    trend_state: str | None = None
    wyckoff_bias: str | None = None
    # 打法原样带过来，理由同 `Recommendation.playbook`：兑现统计必须能按打法分组。
    playbook: str | None = None
    # 来自建议还是对照组。少了这个字段，两组数据混在一起就再也分不开。
    rec_kind: str = KIND_RECOMMENDATION
    # 特征与市场状态原样带过来，让一条兑现记录自带全部分层维度，统计时不必回头
    # 关联建议记录。台账是 JSONL，冗余几个字段远比 join 便宜。
    dip: dict[str, Any] = field(default_factory=dict)
    market_state: dict[str, Any] = field(default_factory=dict)
    # 逐笔旁证原样带过来，理由与上面同一条：让一条兑现记录自带全部分层维度。
    # 它是 `Recommendation.trade_flow` 唯一的下游——不带过来的话，那个字段存了
    # 也白存，统计层看到的兑现记录里根本没有它。
    trade_flow: dict[str, Any] = field(default_factory=dict)
    # 三层裁决的结论，原样带过来。有了它，统计才能回答两个此前问不出口的问题：
    # 「模型推翻规则的那些次，是不是真的更准」，以及「哪一类出处的买入位兑现率高」。
    adjudication: dict[str, Any] = field(default_factory=dict)
    # 当天在交易哪条宏观线，与 `market_state` 的情绪周期分层是两件事。
    macro_context: dict[str, Any] = field(default_factory=dict)
    note: str | None = None
    kind: str = KIND_OUTCOME
    decision_id: str | None = None
    recorded_at: str | None = None
    strategy_version: str | None = None
    plan_id: str | None = None
    revision: int | None = None
    strategy_revision: int | None = None
    # 百分比价位实际锚定在哪一根日线的收盘上（见 `Recommendation.reference_date`）。
    anchor_date: str | None = None
    # 建议发出时的盈亏比与止损距离。统计要按它们分桶——「盈亏比高的计划兑现率
    # 是不是真的更高」「三个日波幅的止损是不是太宽」都是只有台账能回答的问题。
    rrr: float | None = None
    stop_pct: float | None = None
    # 可执行口径的重放（`journal/verify.py:_replay_execution`）：先成交、T+1 起可卖、
    # 跳空按开盘价。上面的 `first_hit` / `mfe_pct` 是从参考收盘起算的信号口径，
    # 不管有没有买到；两套并存，各回答各的问题。
    execution: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def facts_hash(facts: Any) -> str:
    """输入事实的指纹，让「当时到底看到了什么」可复现。"""
    payload = json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
