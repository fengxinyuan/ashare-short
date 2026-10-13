"""兑现统计：这套打分到底有没有用。

只报告，不调权重。样本量不足时按统计结果改参数，改的是噪声——系统会看起来在
进步，实际在漂移。所以这里的产出是给人看的表，不是回写到打分器的系数。

`MIN_BUCKET` 以下的分组照样列出来但标注不可信，因为「样本太少」这件事本身
就是结论的一部分，藏起来只会让人误读那个数字。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date
from statistics import fmean, median
from typing import Any

from ashare_decision_support.journal.records import record_key
from ashare_decision_support.journal.verify import (
    CLOSED_EXITS,
    EXEC_NO_OPEN,
    EXIT_LIMIT_LOCKED,
    EXIT_STOP,
    EXIT_T1_LOCKED,
    EXIT_TARGET,
    EXIT_TIME,
)
from ashare_decision_support.models.enums import EmotionCycle
from ashare_decision_support.numeric import finite
from ashare_decision_support.signals.series import parse_bar_datetime
from ashare_decision_support.strategy.regime import BLOWUP_HEALTHY, BLOWUP_STRESSED
from ashare_decision_support.symbols import normalize_symbol

# 一个分组低于这个样本数，统计量不作为判断依据。
MIN_BUCKET = 20

# 对照差值至少跨五个共同交易日才值得人工复查，避免单日的大样本掩盖行情相关性。
# 这只是报告的观察门槛；拟合或调参仍受下面的 200 条 / 60 日 / 3 周期约束。
MIN_COMPARISON_SESSIONS = 5

# 建议台账里有 200 条已兑现记录之前，不考虑上拟合模型。

# **这个门槛按「条」计，而条数不是有效样本量。** 60–90 条/天，5 个交易日就能凑
# 够 200 条——但那是 5 天的市场，不是 200 个独立样本：A 股日内截面相关极高，
# 同一天几十只票的涨跌基本由大盘和板块 beta 决定。所以真正的放行条件是下面
# 那个按**交易日数**算的门槛，两个都满足才叫样本够。
MODEL_READY_SAMPLES = 200

# 覆盖到的交易日数下限，以及必须覆盖到的情绪周期档数。前者防「一天几百条冒充
# 几百个样本」，后者防「全部样本来自同一种行情」——同一套规则在退潮期和启动期
# 的期望值可以是反号的，只在一种周期里验证过的结论不能外推到另一种。
MODEL_READY_SESSIONS = 60
MODEL_READY_REGIMES = 3

SCORE_BUCKETS = ((0, 60), (60, 70), (70, 80), (80, 101))

# 盈亏比分桶。1.0 以下是「赢一次赔一次也亏钱」，1.5 是盘中执行门槛
# （`jobs/advise.py:_risk_qualified_plan`），2.5 是打分满分点（`scoring/rule_based.py`）。
# 切点跟着那两处走，这样「门槛以上的计划兑现率是不是真的更高」才对得上账。
RRR_BUCKETS = ((0.0, 1.0), (1.0, 1.5), (1.5, 2.5), (2.5, float("inf")))

# 止损距离分桶，单位是标的自身的日波幅（ATR%）。`signals/daily_technical.py` 把止损
# 夹在 1–3 个 ATR 之间；这一组回答的是「三个日波幅的止损对 1–5 日的持有期是不是
# 太宽」——那是个实证问题，先分桶记着，样本够了再谈改上限。
STOP_ATR_BUCKETS = ((0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, float("inf")))

# 固定扣减 0.10% 的往返成本情景，用于估计费用敏感度，不是逐笔费用结算。
# 未记录成交金额，无法核算最低佣金；按开盘/触发价模拟也没有包含滑点。
# 只在统计侧扣，台账保留毛收益及模拟假设。
ROUND_TRIP_COST_PCT = 0.001

# 逐日曲线保留最近多少个成交日给报告和事实包。
EQUITY_DAILY_LIMIT = 20

# 有效分数的全部分档；`无分数` 不参与比较，低于 60 的有效分数仍需验证。
SCORED_BUCKETS = ("0-60", "60-70", "70-80", "80-100")

# 炸板率的情绪周期切分，**与实时闸门共用同一组切点**（`strategy/regime.py`）。
# 两边各定一套是个隐蔽的坑：闸门按 0.50 切、统计按 0.55 切，那么「退潮期这套
# 规则管不管用」的样本就和真正被闸门拦下的日子对不上，闸门永远无法被自己的
# 台账验证。
REGIME_HEALTHY = BLOWUP_HEALTHY
REGIME_STRESSED = BLOWUP_STRESSED

# 全场成交额相对变化的分档，用于 `by_turnover` 分组。

# 和上面那组的处置**刻意不同**：情绪周期的切点必须与实时闸门共用，因为那是一个
# 会改变系统行为的闸门；成交额这一层不改任何行为，只是一个被存下来的数，所以
# 分档定在统计侧、由这里说了算。存的是原始数不是标签，也正是这个原因。
TURNOVER_EXPANDING = 0.05
TURNOVER_SHRINKING = -0.05

# 「这一条记录里没有这个读数」的统一桶名。全表十来个分组函数都会往这里丢两类行：
# 该层上线之前写的老记录，以及关掉那一层（`--no-trade-flow` 之类）跑出来的批次。

# 收成一个常量是因为其中一处是**比较**而不是返回：`_model_readiness` 要把未记录
# 的行排除在「见过几种情绪周期」之外，于是同一个字符串在 `_regime_bucket` 里写、
# 在那句 `!=` 里读。两端各写一份字面量时，改了桶名而漏掉那句比较**不会报错**，
# 只会让「未记录」当成一种真实周期计数，样本充分性的门槛就此提前变绿。
UNRECORDED = "未记录"
MACRO_NONE = "无明确宏观链路"
MACRO_NO_SIGNAL = "无方向信号"
MACRO_NO_EXPECTATION = "无预期差"


def summarize(outcomes: list[dict], recommendations: list[dict] | None = None) -> dict[str, Any]:
    """按整体、分数桶、板块、趋势状态分组统计。

    `recommendations` 可选：传入建议记录时，把发出当时的 `score_components` 接回到
    兑现记录上，才能按评分分项拆解（`by_component`）。兑现记录本身不带分项——
    那是几十个数，按月全读的 JSONL 里每条都复制一份不值得，靠 `record_key` 关联即可。
    """
    recorded = [
        row
        for row in outcomes
        if isinstance(row, dict) and (row.get("kind") == "outcome" or "mfe_pct" in row)
    ]
    usable = _with_execution_fields(
        _with_score_components(_first_decisions(recorded), recommendations)
    )
    readiness = _model_readiness(usable)
    selection = _selection_edge(usable)
    veto = _veto_cost(usable)
    by_score = _group(usable, _score_bucket)
    by_component = _component_breakdown(usable)
    return {
        "samples": len(usable),
        "decisions_recorded": len(recorded),
        "revision_policy": "总体统计每只股票、每日、每个来源只取首次判断；策略版本分组各取该版本首次判断。",
        "by_strategy_version": _group(
            _with_execution_fields(_first_decisions(recorded, by_version=True)),
            lambda row: row.get("strategy_version") or UNRECORDED,
        ),
        "model_ready": readiness["ready"],
        "model_ready_gap": max(0, MODEL_READY_SAMPLES - len(usable)),
        "readiness": readiness,
        "overall": _stats(usable),
        "by_score": by_score,
        # 分数分档反向时，下一个问题是「哪个分项反向」。这一组按每个分项的高半/
        # 低半按日配对比兑现，是逐项复查的起点；没传建议记录时为空。
        "by_component": by_component,
        # 盈亏比与止损距离是计划发出时就定下的两个数。它们分桶后的兑现率是
        # 「门槛该不该收紧」「止损上限该不该压低」唯一的实证依据；在此之前那两个
        # 阈值都只是先验。
        "by_rrr": _group(usable, _rrr_bucket),
        "by_stop_distance": _group(usable, _stop_distance_bucket),
        "by_sector": _group(usable, lambda row: row.get("sector") or "未分类"),
        "by_trend": _group(usable, lambda row: row.get("trend_state") or "未知"),
        # 按打法分组。回踩和突破的止损距离、持有期本来就不同，混在一起算出的
        # 平均兑现率不描述其中任何一类。这一组是「该保留哪几条打法」的答案位置。
        "by_playbook": _group(usable, lambda row: row.get("playbook") or UNRECORDED),
        "by_execution_model": _group(usable, _execution_model),
        # 入选 vs 对照。这一组是全表里唯一能回答「这套筛选本身有没有用」的：
        # 入选组次日涨 1.2% 听起来不错，但若对照组涨 1.4%，规则就是负价值。
        "by_group": _group(usable, lambda row: row.get("rec_kind") or "recommendation"),
        # 情绪周期分层。超短最重要的分层维度，此前完全没有。
        "by_regime": _group(usable, _regime_bucket),
        # 宏观链路是日级公共上下文，不直接进打分；这三组是它的验收位。
        "by_macro_topic": _multi_group(usable, _macro_topic_buckets),
        "by_macro_signal": _multi_group(usable, _macro_signal_buckets),
        "by_macro_expectation": _multi_group(usable, _macro_expectation_buckets),
        # 以下三组是「只出读数、不进打分」那几层的**验收位**。
        # 它们存在的全部意义是回答同一个问题的三个版本：那一层报出来的东西，
        # 事后看有没有区分度。没有区分度就该删掉，而不是留着当装饰。
        # 周度轮动方向。用 `rotation_adjacent`（相邻等长窗口）而不是月度那个：
        # 超短的持有期是 1–5 天，月度趋势标签跨越了好几个持有期，拿它分组等于
        # 把同一段行情里方向相反的日子混进一个桶。
        "by_rotation": _group(usable, _rotation_bucket),
        # 存量还是增量。同一个「高低切换」在缩量和放量下的含义相反，这一组是
        # 把两者分开之后才谈得上「切换的日子该不该做」。
        "by_turnover": _group(usable, _turnover_bucket),
        # 等权与市值加权是否背离。这一组直接验收 `adapters/broad_index.py` 那一层：
        # 若「等权强加权弱」的日子兑现率与「一致」的日子没有差别，那条告警就是
        # 噪声，该撤掉；有差别才谈得上要不要让它影响仓位。
        "by_index_divergence": _group(usable, _index_bucket),
        # 上一交易日的大单净额方向。这一组验收的是**逐笔那一层**：它挂在候选行上
        # 只作旁证，若正负两档的兑现率没有差别，那它连旁证都算不上。
        "by_large_net": _group(usable, _large_net_bucket),
        # 回调深度分层。低吸的核心问题「调多少买最好」只有这一组能回答。
        "by_pullback": _group(usable, _pullback_bucket),
        # 买入位的**依据类别**。分时均价回踩 / 五档买盘墙 / 日线支撑哪一类兑现率
        # 更高，是个纯实证问题——L2 给每个候选带 `basis` 就是为了这一组能存在。
        "by_entry_basis": _group(usable, _entry_basis_bucket),
        # 规则默认 vs 模型裁决。加 L3 那层时唯一无法在设计阶段回答的问题，
        # 只能靠样本回答；在此之前它不参与任何加权。
        "by_decider": _group(usable, _decider_bucket),
        "selection_edge": selection,
        # 闸门的代价。和 selection_edge 分开报：一个衡量排序，一个衡量否决，
        # 两者可以一好一坏，合成一个数会互相抵消掉。
        "veto_cost": veto,
        # 按成交计划日归集的逐日已实现收益：入选组照计划做会长什么样，对照组并列。
        "equity": _equity_curves(usable),
        "rule_feedback": _rule_feedback(selection, veto, usable, by_component),
        "adjudication_edge": _adjudication_edge(usable),
        "min_bucket": MIN_BUCKET,
    }


def rolling_validation(
    outcomes: list[dict],
    windows: tuple[int, ...] = (20, 60),
) -> dict[str, Any]:
    """按最近 N 个交易日切窗，观察验证结果是否正在变化。

    这里按交易日而不是按记录条数切窗，避免某一天候选很多时把单日行情误当成
    多个独立样本。它只报告近期状态，不参与调权或自动学习。
    """
    usable = _with_execution_fields(
        _first_decisions(
            [
                row
                for row in outcomes
                if isinstance(row, dict) and (row.get("kind") == "outcome" or "mfe_pct" in row)
            ]
        )
    )
    dates = sorted({_trade_date(row) for row in usable if _trade_date(row) is not None})
    result: dict[str, Any] = {
        "available_sessions": len(dates),
        "latest_session": dates[-1] if dates else None,
        "windows": {},
    }

    for size in windows:
        if size <= 0:
            continue
        selected = set(dates[-size:])
        rows = [row for row in usable if _trade_date(row) in selected]
        label = f"last_{size}_sessions"
        result["windows"][label] = _rolling_window(rows, dates[-size:])
    return result


def _first_decisions(rows: list[dict], *, by_version: bool = False) -> list[dict]:
    """新版本不能靠同日反复运行增加样本；无版本的旧台账保留原有统计口径。"""
    legacy = [row for row in rows if not row.get("decision_id")]
    versions = sorted(
        (row for row in rows if row.get("decision_id")),
        key=lambda row: (
            str(parse_bar_datetime(row.get("recorded_at")) or ""),
            str(row.get("decision_id")),
        ),
    )

    def key(row: dict) -> tuple | None:
        day = _trade_date(row)
        symbol, source = row.get("symbol"), row.get("source")
        if (
            not day
            or not isinstance(symbol, str)
            or not symbol
            or not isinstance(source, str)
            or not source
        ):
            return None
        result = (normalize_symbol(symbol), day, source)
        return (*result, row.get("strategy_version") or UNRECORDED) if by_version else result

    seen = {value for row in legacy if (value := key(row)) is not None}
    result = list(legacy)
    for row in versions:
        # 首次判断尚未兑现时，不能用后续修改中先兑现的一条替补它。
        revision = row.get("strategy_revision" if by_version else "revision")
        if isinstance(revision, int) and revision > 1:
            continue
        value = key(row)
        if value is not None and value in seen:
            continue
        result.append(row)
        if value is not None:
            seen.add(value)
    return result


def _rolling_window(rows: list[dict], dates: list[str | None]) -> dict[str, Any]:
    valid_dates = [item for item in dates if item is not None]
    return {
        "from": valid_dates[0] if valid_dates else None,
        "to": valid_dates[-1] if valid_dates else None,
        "sessions": len(valid_dates),
        "samples": len(rows),
        "overall": _stats(rows),
        "selection_edge": _selection_edge(rows),
        "veto_cost": _veto_cost(rows),
        "by_macro_topic": _compact_buckets(_multi_group(rows, _macro_topic_buckets)),
        "by_macro_signal": _compact_buckets(_multi_group(rows, _macro_signal_buckets)),
        "by_macro_expectation": _compact_buckets(_multi_group(rows, _macro_expectation_buckets)),
    }


def _compact_buckets(rows: list[dict]) -> list[dict[str, Any]]:
    """近期窗口只保留能判断趋势的字段，避免事实包被历史分组撑爆。"""
    return [
        {
            "bucket": row["bucket"],
            "samples": row["samples"],
            "sessions": row.get("sessions", 0),
            "trusted": row["trusted"],
            "median_next_close_pct": row.get("median_next_close_pct"),
            "positive_close_rate": row.get("positive_close_rate"),
        }
        for row in sorted(rows, key=lambda item: (-item["samples"], item["bucket"]))[:12]
    ]


def _trade_date(row: dict) -> str | None:
    value = row.get("trade_date")
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _adjudication_edge(rows: list[dict]) -> dict[str, Any]:
    """模型推翻规则的那些次，比规则自己选的那些次好还是差。

    **只比「模型改了排序」的那一组和「规则默认」那一组。** 模型选了和规则同一个
    候选的记录被排除在外——它们两边都算的话，会把大量「模型没做决定」的样本
    掺进模型那一组，把差值稀释向零。那正好是最容易得出「加了也没变化」这种
    错误结论的方式。

    弃权的记录也不进这里：它们没有买入价，兑现流程里根本不算「入场」，
    和有价位的记录不可比。弃权的价值要单独看——见 `abstained_samples`。
    """
    eligible = [
        row
        for row in rows
        if not (row.get("adjudication") or {}).get("abstained")
        and (row.get("rec_kind") or "recommendation") == "recommendation"
    ]
    overrode = [row for row in eligible if (row.get("adjudication") or {}).get("overrode_rule")]
    default = [
        row
        for row in eligible
        if (row.get("adjudication") or {}).get("used_fallback")
        or not (row.get("adjudication") or {})
    ]
    abstained = sum(1 for row in rows if (row.get("adjudication") or {}).get("abstained"))

    if not overrode or not default:
        return {
            "available": False,
            "reason": "模型改过排序的样本与规则默认的样本都有，才能算差值",
            "overrode_samples": len(overrode),
            "default_samples": len(default),
            "abstained_samples": abstained,
        }

    return {
        "overrode_samples": len(overrode),
        "default_samples": len(default),
        "abstained_samples": abstained,
        **_paired_gaps(overrode, default, ("next_close_pct", "close_pct", "mfe_pct", "mae_pct")),
    }


def _screened(rows: list[dict]) -> list[dict]:
    """走过盘前筛选的那些行。`_selection_edge` 与 `_veto_cost` 的入选组用它。

    两个函数的差值都建立在「同日、同筛选流程、只差最后一刀排序」之上，而台账里
    并非每条记录都满足这个前提：`advise` 那条链路记的是**人手点名**的标的，它们
    从来没进过板块排序和业绩闸门。按 `rec_kind` 取会把它们一并算进入选组，于是
    「这套筛选有没有用」的分子里混进了一批不是这套筛选选出来的票。

    按 `source` 而不是 `kind` 分：`kind` 分的是入选/对照/否决三个**组**，
    `source` 分的是这条记录由**哪条链路**产生的——这里要的是后者。
    """
    return [row for row in rows if (row.get("source") or "morning") == "morning"]


def _selection_edge(rows: list[dict]) -> dict[str, Any]:
    """入选组减对照组。**这个差值才是这套规则的真实价值。**

    单看入选组的绝对收益毫无意义——普涨的日子里随便选都赚。两组同日、同筛选
    流程、只差最后一刀排序，差值才把市场本身的涨跌消掉了。
    """
    screened = _screened(rows)
    picked = [
        row for row in screened if (row.get("rec_kind") or "recommendation") == "recommendation"
    ]
    control = [row for row in screened if row.get("rec_kind") == "control"]
    # `exec_return_pct` 是执行口径的已实现收益（见 `_with_execution_fields`）：先成交、
    # T+1 起可卖、跳空按开盘价。信号口径的差值说排序方向对不对，这个差值说照着
    # 两组计划下单之后账户里差多少——两者可以异号。
    return {
        "picked_samples": len(picked),
        "control_samples": len(control),
        **_paired_gaps(
            picked,
            control,
            ("next_open_pct", "next_close_pct", "close_pct", "mfe_pct", "exec_return_pct"),
        ),
    }


def _veto_cost(rows: list[dict]) -> dict[str, Any]:
    """否决组减入选组。**正数意味着这道闸门在帮倒忙。**

    和 `_selection_edge` 的方向刻意相反，因为问的不是同一件事：那个问「排在
    前面的是不是真的更好」，这个问「被拦下来的是不是本来该买」。

    业绩否决是这套系统里唯一一道**明知有代价还是加了**的闸门——它会挡掉亏损但
    正当红的题材股，而那在超短线上完全可能是当期最强的品种。这个数就是那笔代价
    的账单：如果否决组的次日收益持续高于入选组，闸门就该放松或撤掉。

    在样本够之前它只是被记录，不驱动任何自动调整——和别处一样，先落台账，等
    数据说话。
    """
    screened = _screened(rows)
    picked = [
        row for row in screened if (row.get("rec_kind") or "recommendation") == "recommendation"
    ]
    vetoed = [row for row in screened if row.get("rec_kind") == "vetoed"]
    return {
        "vetoed_samples": len(vetoed),
        "picked_samples": len(picked),
        # 正数 = 被否决的那批表现更好 = 闸门拦错了。
        **_paired_gaps(vetoed, picked, ("next_close_pct", "close_pct", "mfe_pct")),
    }


def _paired_gaps(left: list[dict], right: list[dict], fields: tuple[str, ...]) -> dict:
    """只比较共同交易日，先算日内中位数之差，再取日差的中位数。

    同日内还匹配来源与策略版本；晨报和盘后记录的次日对应不同价格窗口，不能混算。
    每个可比组先求中位数之差，再归并到当日，最后让各日等权。期末指标还匹配窗口。
    """
    result: dict[str, Any] = {}
    coverage = {}
    for field in fields:
        key = field.removesuffix("_pct") + "_gap"
        result[key], coverage[key] = _session_gap(left, right, field)
    available = any(value is not None for value in result.values())
    primary = coverage.get("next_close_gap", {})
    return {
        **result,
        "available": available,
        "trusted": primary.get("trusted", False),
        "matched_sessions": primary.get("sessions", 0),
        "metric_coverage": coverage,
        "comparison_basis": (
            "匹配交易日、来源与策略版本；先算各可比组中位数之差，归并到当日后按日等权；"
            "期末指标匹配兑现窗口，执行收益还需匹配模拟版本"
        ),
        **({} if available else {"reason": "缺少同日、同来源、同策略版本及兑现窗口的有效对照数据"}),
    }


def _session_gap(left: list[dict], right: list[dict], field: str) -> tuple[float | None, dict]:
    a, b = _session_values(left, field), _session_values(right, field)
    daily: dict[str, list[float]] = defaultdict(list)
    left_samples = right_samples = 0
    for key in a.keys() & b.keys():
        daily[key[0]].append(median(a[key]) - median(b[key]))
        left_samples += len(a[key])
        right_samples += len(b[key])
    gap = round(median(median(values) for values in daily.values()), 4) if daily else None
    return gap, {
        "left_samples": left_samples,
        "right_samples": right_samples,
        "sessions": len(daily),
        "trusted": (
            min(left_samples, right_samples) >= MIN_BUCKET and len(daily) >= MIN_COMPARISON_SESSIONS
        ),
    }


def _comparison_cohort(row: dict) -> tuple[str, str, str] | None:
    day = _trade_date(row)
    if day is None:
        return None
    # 无来源的旧台账来自晨报；未记录版本单独配对，不推断成当前版本。
    return day, str(row.get("source") or "morning"), str(row.get("strategy_version") or UNRECORDED)


def _session_values(
    rows: list[dict], field: str
) -> dict[tuple[str, str, str, int | None, str | None], list[float]]:
    values: dict[tuple[str, str, str, int | None, str | None], list[float]] = defaultdict(list)
    for row in rows:
        cohort, value = _comparison_cohort(row), finite(row.get(field))
        if cohort is None or value is None:
            continue
        period = None
        if not field.startswith("next_"):
            number = finite(row.get("sessions"))
            if number is None or number < 1 or not number.is_integer():
                continue
            period = int(number)
        model = _execution_model(row) if field == "exec_return_pct" else None
        values[(*cohort, period, model)].append(value)
    return values


def _execution_model(row: dict) -> str:
    execution = row.get("execution")
    return (
        str(execution.get("model_version") or UNRECORDED)
        if isinstance(execution, dict)
        else UNRECORDED
    )


def _rule_feedback(
    selection: dict,
    veto: dict,
    rows: list[dict],
    by_component: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """四条复查项，只提人、不改规则。

    前两条读次日收盘差（信号口径）：排序有没有用、闸门拦对了没有。第三条读执行口径
    的已实现收益差——方向对了但买不到、买到了被跳空打掉，只有这一条看得见。第四条
    看分数分档：分数最高档的兑现若低于最低可信档，加分项本身就该复查，而这件事在
    任何一个差值里都不会显现。
    """
    feedback = [
        _gap_feedback(
            "selection",
            "筛选排序",
            selection.get("next_close_gap"),
            (selection.get("metric_coverage") or {}).get("next_close_gap") or {},
            adverse_when_negative=True,
            adverse_action="入选组落后对照组，检查排序因子与板块集中度。",
        ),
        _gap_feedback(
            "veto",
            "风险否决",
            veto.get("next_close_gap"),
            (veto.get("metric_coverage") or {}).get("next_close_gap") or {},
            adverse_when_negative=False,
            adverse_action="否决组次日表现更强，复查具体否决原因及尾部风险，不据此自动放松闸门。",
        ),
        _gap_feedback(
            "execution",
            "执行口径排序",
            selection.get("exec_return_gap"),
            (selection.get("metric_coverage") or {}).get("exec_return_gap") or {},
            adverse_when_negative=True,
            adverse_action=(
                "按计划成交后入选组落后对照组：先查止损与目标是否过宽、成交日跳空是否"
                "吃掉了优势，再看排序因子。"
            ),
            gap_label="已实现收益差",
            missing_action="补齐两组按计划成交后的已实现收益，再评估执行口径。",
        ),
        _score_feedback(rows, by_component or []),
    ]
    # 老的两条保留 `next_close_gap` 字段名：事实包和已有消费者按它读。
    for item in feedback[:2]:
        item["next_close_gap"] = item["gap"]
    return feedback


def _gap_feedback(
    rule: str,
    title: str,
    gap: float | None,
    evidence: dict,
    *,
    adverse_when_negative: bool,
    adverse_action: str,
    gap_label: str = "次日差值",
    missing_action: str = "补齐共同交易日的两组次日收盘读数，再评估规则。",
) -> dict[str, Any]:
    adverse = gap is not None and (gap < 0 if adverse_when_negative else gap > 0)
    if gap is None:
        status, action = "待补数据", missing_action
    elif not evidence.get("trusted"):
        status, action = (
            "继续观察",
            f"有效样本须两组各达到 {MIN_BUCKET} 条，并覆盖 {MIN_COMPARISON_SESSIONS} 个共同交易日。",
        )
    elif adverse:
        status, action = "建议复查", adverse_action
    else:
        status, action = "保留观察", "当前差值未提示反向效果，继续按情绪周期和近期窗口观察。"
    return {
        "rule": rule,
        "title": title,
        "status": status,
        "action": action,
        "gap": gap,
        "gap_label": gap_label,
        "evidence": evidence,
    }


def _score_feedback(rows: list[dict], by_component: list[dict]) -> dict[str, Any]:
    """分档方向使用逐日配对差；不能把两档各有五日误当成有五个共同交易日。"""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        name = _score_bucket(row)
        if name in SCORED_BUCKETS:
            buckets[name].append(row)
    item: dict[str, Any] = {
        "rule": "score",
        "title": "分数分档",
        "gap": None,
        "gap_label": "可比高档 − 低档次日收盘差",
        "evidence": {},
    }
    if not buckets:
        return item | {"status": "待补数据", "action": "还没有带分数的兑现记录。"}
    if len(buckets) < 2:
        return item | {
            "status": "继续观察",
            "action": (
                f"至少两个分数档各达到 {MIN_BUCKET} 条有效数据、覆盖 {MIN_COMPARISON_SESSIONS} 个共同交易日，"
                "才比较分档方向。"
            ),
        }
    gap, evidence = _score_comparison(buckets)
    evidence["text"] = (
        f"{evidence['top_bucket']} 档 {evidence['left_samples']} 条对 "
        f"{evidence['bottom_bucket']} 档 {evidence['right_samples']} 条有效数据，"
        f"覆盖 {evidence['sessions']} 个共同交易日；来源与策略版本相同，按日等权。"
    )
    feedback = _gap_feedback(
        "score",
        "分数分档",
        gap,
        evidence,
        adverse_when_negative=True,
        adverse_action="可比高分档的次日表现落后低分档，需复查评分；不要按总分加仓。",
        gap_label=item["gap_label"],
        missing_action="补齐同日、同来源、同策略版本的两档次日收盘读数，再比较分档方向。",
    )
    if feedback["status"] == "建议复查":
        feedback["action"] += _score_inversion(by_component, evidence)
    return feedback


def _score_comparison(buckets: dict[str, list[dict]]) -> tuple[float | None, dict]:
    """按分档顺序优先比较最高与最低可比档，只按覆盖选档，不看收益正负。"""
    ranked = [name for name in SCORED_BUCKETS if name in buckets]
    comparisons = []
    for index in range(len(ranked) - 1, 0, -1):
        high = ranked[index]
        for low in ranked[:index]:
            gap, coverage = _session_gap(buckets[high], buckets[low], "next_close_pct")
            comparisons.append((gap, coverage | {"top_bucket": high, "bottom_bucket": low}))
    return min(comparisons, key=lambda item: (not item[1]["trusted"], item[0] is None))


def _score_inversion(by_component: list[dict], evidence: dict) -> str:
    """分档读的是次日差，复查提示也必须使用覆盖达标的次日差，不能由少量执行样本决定。"""
    inverted = [
        row
        for row in by_component
        if row.get("direction") == "反向" and row["metric_coverage"]["next_close_gap"]["trusted"]
    ]
    if inverted:
        worst = min(inverted, key=lambda row: row["next_close_gap"])
        evidence["inverted_component"] = worst["component"]
        return (
            f"反向最重的分项是 {worst['component']}"
            f"（高值组 − 低值组次日差 {worst['next_close_gap']:+.2%}），先复查它的关联读数。"
        )
    if by_component:
        return "分项拆解里还没有次日覆盖达标的反向项，见评分分项拆解表。"
    return "逐项看 score_components；review 会在有建议记录时给出分项拆解表。"


def _with_score_components(rows: list[dict], recommendations: list[dict] | None) -> list[dict]:
    """把建议发出当时的评分分项接回兑现记录。按 `record_key` 关联，同一判断编号对同一条。"""
    if not recommendations:
        return rows
    lookup: dict[str, dict[str, float]] = {}
    for row in recommendations:
        if not isinstance(row, dict) or row.get("kind") == "outcome":
            continue
        components = row.get("score_components")
        if isinstance(components, dict) and components:
            lookup.setdefault(record_key(row), components)
    result: list[dict] = []
    for row in rows:
        if not row.get("score_components"):
            components = lookup.get(record_key(row))
            if components:
                row = {**row, "score_components": components}
        result.append(row)
    return result


def _component_breakdown(rows: list[dict]) -> list[dict[str, Any]]:
    """按同日、同来源、同策略版本的分项中位数分组；各收益口径分别计算有效覆盖。"""
    scored = [
        row
        for row in rows
        if isinstance(row.get("score_components"), dict) and row["score_components"]
    ]
    names = sorted(
        {
            name
            for row in scored
            for name, value in row["score_components"].items()
            if finite(value) is not None
        }
    )
    result: list[dict[str, Any]] = []
    for name in names:
        valued = [
            (value, row)
            for row in scored
            if (value := finite(row["score_components"].get(name))) is not None
        ]
        if len(valued) < MIN_BUCKET:
            continue
        high, low, cuts = _split_component(valued)
        gaps = _paired_gaps(high, low, ("next_close_pct", "close_pct", "exec_return_pct"))
        readings = _trusted_component_metrics(gaps)
        has_dates = any(_trade_date(row) for _, row in valued)
        result.append(
            {
                **gaps,
                "component": name,
                "samples": len(valued),
                "cut": round(median(cuts), 4) if cuts else None,
                "cut_min": round(min(cuts), 4) if cuts else None,
                "cut_max": round(max(cuts), 4) if cuts else None,
                "high_samples": len(high),
                "low_samples": len(low),
                "unpaired_samples": len(valued) - len(high) - len(low),
                "trusted": bool(readings),
                "direction": (
                    _component_direction(gaps, readings) if cuts or not has_dates else "无区分度"
                ),
                "direction_basis": (
                    "次日与执行" if len(readings) == 2 else "仅" + next(iter(readings))
                )
                if readings
                else "覆盖不足",
            }
        )
    return sorted(result, key=_component_order)


def _split_component(
    valued: list[tuple[float, dict]],
) -> tuple[list[dict], list[dict], list[float]]:
    """同分保持在同一组；中位数等于最大值时，把较低值作为低组，避免误报常数分项。"""
    cohorts: dict[tuple[str, str, str], list[tuple[float, dict]]] = defaultdict(list)
    for value, row in valued:
        if (cohort := _comparison_cohort(row)) is not None:
            cohorts[cohort].append((value, row))
    high, low, cuts = [], [], []
    for values in cohorts.values():
        numbers = [value for value, _ in values]
        if min(numbers) == max(numbers):
            continue
        cut = median(numbers)
        upper_tie = cut == max(numbers)
        for value, row in values:
            (high if value > cut or (upper_tie and value == cut) else low).append(row)
        cuts.append(cut)
    return high, low, cuts


def _trusted_component_metrics(item: dict) -> dict[str, float]:
    return {
        label: item[field]
        for field, label in (("next_close_gap", "次日"), ("exec_return_gap", "执行"))
        if item.get(field) is not None
        and (item.get("metric_coverage", {}).get(field) or {}).get("trusted")
    }


def _component_direction(gaps: dict, readings: dict[str, float]) -> str:
    """覆盖达标的口径才参与判读；仅一项达标时由 direction_basis 明示依据。"""
    if not readings:
        return (
            "继续观察"
            if any(gaps.get(key) is not None for key in ("next_close_gap", "exec_return_gap"))
            else "待补数据"
        )
    if all(value > 0 for value in readings.values()):
        return "同向"
    if all(value < 0 for value in readings.values()):
        return "反向"
    if all(value == 0 for value in readings.values()):
        return "持平"
    return "分歧"


def _component_order(item: dict[str, Any]) -> tuple[int, float]:
    """达标口径优先；执行覆盖不足时不让少量极端成交决定复查顺序。"""
    readings = _trusted_component_metrics(item)
    gap = readings.get("执行", readings.get("次日"))
    if gap is None:
        gap = item.get("exec_return_gap")
        if gap is None:
            gap = item.get("next_close_gap")
    return int(not readings), gap if gap is not None else float("inf")


def _equity_curves(rows: list[dict]) -> dict[str, Any]:
    """入选组与对照组各一条按成交计划日归集的逐日已实现收益。

    每笔等权、扣固定成本情景，把已平仓计划的持有期收益归到成交日后复乘。
    这是合成研究曲线；持有期仍可能重叠，未模拟资金占用、仓位上限或每日盯市，
    不能据此声称得到组合净值或账户最大回撤。
    """
    screened = _screened(rows)
    picked = [
        row for row in screened if (row.get("rec_kind") or "recommendation") == "recommendation"
    ]
    control = [row for row in screened if row.get("rec_kind") == "control"]
    return {
        "cost_pct": ROUND_TRIP_COST_PCT,
        "basis": (
            "仅已平仓模拟，每笔等权、扣固定成本情景、按成交日归集持有期收益后复乘；"
            "持有期可能重叠，未模拟资金占用，曲线及回撤不是账户净值指标"
        ),
        "slippage_included": False,
        "minimum_commission_included": False,
        "picked": _daily_equity(picked),
        "control": _daily_equity(control),
    }


def _daily_equity(rows: list[dict]) -> dict[str, Any]:
    by_day: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        execution = row.get("execution") or {}
        if not _closed_execution(execution):
            continue
        value = finite(execution.get("return_pct"))
        day = execution.get("fill_session")
        if value is None or not day:
            continue
        by_day[str(day)].append(value - ROUND_TRIP_COST_PCT)
    if not by_day:
        return {"sessions": 0, "trades": 0}

    equity = peak = 1.0
    drawdown = 0.0
    daily: list[dict[str, Any]] = []
    for day in sorted(by_day):
        mean = fmean(by_day[day])
        equity *= 1.0 + mean
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
        daily.append(
            {
                "date": day,
                "trades": len(by_day[day]),
                "mean_net_return_pct": round(mean, 4),
                "equity": round(equity, 4),
            }
        )
    return {
        "sessions": len(daily),
        "trades": sum(item["trades"] for item in daily),
        "cumulative_pct": round(equity - 1.0, 4),
        "max_drawdown_pct": round(drawdown, 4),
        "positive_sessions": sum(1 for item in daily if item["mean_net_return_pct"] > 0),
        "daily": daily[-EQUITY_DAILY_LIMIT:],
    }


def _column(rows: list[dict], field: str) -> list[float]:
    """取一列兑现读数。取不到、不是数、NaN，都不进来。

    走 `numeric.finite` 而不是 `isinstance(value, (int, float))`——这一列会直接
    进 `median()`，而 **NaN 不会让 median 报错，它会让 median 悄悄给错数**：
    `median([0.1, nan, 0.9, 0.2, 0.3])` 返回 0.2，而去掉 NaN 之后的真值是 0.25。
    排序时 NaN 参与的比较恒为 False，于是它落在哪、把谁挤到中间，取决于原始顺序。

    台账是 JSONL，`json.dumps` 会把一个 NaN 原样写成裸 `NaN`、`json.loads` 再原样
    读回来，所以这条路是通的：只要有一条记录的 `close_pct` 是 NaN（上游给了个
    NaN 收盘价，`round(nan/base - 1, 6)` 照样是 nan，`is None` 判断拦不住），
    整张兑现表的中位数就全部偏掉，而且不报任何错。

    顺带挡掉 bool：`isinstance(True, (int, float))` 是 True，那会让一个写错类型的
    字段被当成 1.0 记进收益。
    """
    values: list[float] = []
    for row in rows:
        value = finite(row.get(field))
        if value is not None:
            values.append(value)
    return values


def _group(rows: list[dict], key: Callable[[dict], str]) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[key(row)].append(row)
    out = [{"bucket": name, **_stats(items)} for name, items in buckets.items()]
    return sorted(out, key=lambda item: item["bucket"])


def _multi_group(rows: list[dict], keys: Callable[[dict], tuple[str, ...]]) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        seen: set[str] = set()
        for name in keys(row):
            if not name or name in seen:
                continue
            seen.add(name)
            buckets[name].append(row)
    out = [{"bucket": name, **_stats(items)} for name, items in buckets.items()]
    return sorted(out, key=lambda item: item["bucket"])


def _stats(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"samples": 0, "sessions": 0, "trusted": False}

    entered = [row for row in rows if row.get("entered")]
    mfe = _column(rows, "mfe_pct")
    mae = _column(rows, "mae_pct")
    close = _column(rows, "close_pct")
    next_open = _column(rows, "next_open_pct")
    next_close = _column(rows, "next_close_pct")
    sessions = {stamp for row in rows if (stamp := _trade_date(row)) is not None}
    first_target = sum(1 for row in rows if row.get("first_hit") == "target")
    first_stop = sum(1 for row in rows if row.get("first_hit") == "stop")
    decided = first_target + first_stop

    return {
        "samples": len(rows),
        "sessions": len(sessions),
        # 样本不足时统计量照给，但 trusted=False——把它藏起来只会让人误读。
        "trusted": len(rows) >= MIN_BUCKET,
        "entry_rate": round(len(entered) / len(rows), 4),
        "target_first_rate": round(first_target / decided, 4) if decided else None,
        "stop_first_rate": round(first_stop / decided, 4) if decided else None,
        "median_mfe_pct": round(median(mfe), 4) if mfe else None,
        "median_mae_pct": round(median(mae), 4) if mae else None,
        "median_close_pct": round(median(close), 4) if close else None,
        # T+1 口径：隔夜跳空与次日收盘。持有期该多长还没定论，两套并列。
        "median_next_open_pct": round(median(next_open), 4) if next_open else None,
        "median_next_close_pct": round(median(next_close), 4) if next_close else None,
        "next_close_positive_rate": (
            round(sum(1 for value in next_close if value > 0) / len(next_close), 4)
            if next_close
            else None
        ),
        "positive_close_rate": (
            round(sum(1 for value in close if value > 0) / len(close), 4) if close else None
        ),
        "execution": _execution_stats(rows),
    }


def _with_execution_fields(rows: list[dict]) -> list[dict]:
    """把执行口径的已实现收益抬到顶层，让按日配对的差值函数能直接读它。

    仅纳入已平仓模拟。未成交和仍持仓的计划均不补零，也不沿用旧的顶层缓存值。
    """
    result: list[dict] = []
    for row in rows:
        execution = row.get("execution")
        clean = {key: value for key, value in row.items() if key != "exec_return_pct"}
        if _closed_execution(execution):
            clean["exec_return_pct"] = execution.get("return_pct")
        result.append(clean)
    return result


def _closed_execution(execution: Any) -> bool:
    """兼容旧台账；旧版把期末锁仓记成 time 的记录无法确认已卖出，保守排除。"""
    if not isinstance(execution, dict) or execution.get("filled") is not True:
        return False
    if execution.get("exit") not in CLOSED_EXITS or execution.get("realized") is False:
        return False
    legacy_locked = (
        execution.get("realized") is None
        and execution.get("exit") == EXIT_TIME
        and (finite(execution.get("locked_sessions")) or 0) > 0
    )
    return not legacy_locked


def _open_execution_stats(filled: list[dict], closed_count: int) -> dict[str, Any]:
    marked = [item for item in filled if item.get("exit") in (EXIT_T1_LOCKED, EXIT_LIMIT_LOCKED)]
    floating = _column(marked, "return_pct")
    return {
        "open_positions": len(filled) - closed_count,
        "marked_positions": len(marked),
        "unresolved_positions": len(filled) - closed_count - len(marked),
        "median_unrealized_return_pct": round(median(floating), 4) if floating else None,
    }


def _execution_stats(rows: list[dict]) -> dict[str, Any]:
    """执行口径：先成交、成交当日不能卖、跳空按开盘价（`journal/verify.py`）。

    和 `_stats` 里那组从参考收盘起算的数分开报——那组不管有没有买到，回答「方向
    对不对」；这组只报告日线价格模拟。收益、胜负及可信样本数仅使用已平仓记录。
    """
    replayed = [
        execution
        for row in rows
        if isinstance(execution := row.get("execution"), dict) and execution.get("basis")
    ]
    replayable = [
        item
        for item in replayed
        if item.get("replayable", item.get("basis") != EXEC_NO_OPEN) and item.get("eligible", True)
    ]
    filled = [item for item in replayed if item.get("filled") is True]
    closed = [item for item in filled if _closed_execution(item)]
    returns = _column(closed, "return_pct")
    net = [value - ROUND_TRIP_COST_PCT for value in returns]
    maes = [value for item in filled if (value := finite(item.get("mae_pct"))) is not None]
    holds = _column(closed, "hold_sessions")
    skipped = Counter(str(item.get("basis")) for item in replayed if item.get("filled") is not True)

    def share(count: int, total: int) -> float | None:
        return round(count / total, 4) if total else None

    return {
        "samples": len(replayed),
        "replayable": len(replayable),
        "filled": len(filled),
        "closed": len(closed),
        "return_samples": len(returns),
        **_open_execution_stats(filled, len(closed)),
        "fill_rate": share(len(filled), len(replayable)),
        "target_rate": share(sum(1 for item in closed if item["exit"] == EXIT_TARGET), len(closed)),
        "stop_rate": share(sum(1 for item in closed if item["exit"] == EXIT_STOP), len(closed)),
        "time_exit_rate": share(
            sum(1 for item in closed if item["exit"] == EXIT_TIME), len(closed)
        ),
        "median_return_pct": round(median(returns), 4) if returns else None,
        "positive_rate": share(sum(1 for value in returns if value > 0), len(returns)),
        # 扣一笔来回成本之后。超短的中位收益本来就是零点几个百分点，费率不是零头。
        "median_net_return_pct": round(median(net), 4) if net else None,
        "net_positive_rate": share(sum(1 for value in net if value > 0), len(net)),
        "median_mae_pct": round(median(maes), 4) if maes else None,
        "median_hold_sessions": round(median(holds), 1) if holds else None,
        # 止损被一字跌停锁住过的笔数：这些记录的实际亏损比止损位深，不是滑点。
        "locked_exits": sum(1 for item in filled if (item.get("locked_sessions") or 0) > 0),
        "skipped": dict(sorted(skipped.items())),
        "trusted": len(returns) >= MIN_BUCKET,
    }


def _rrr_bucket(row: dict) -> str:
    value = finite(row.get("rrr"))
    if value is None or value < 0:
        return UNRECORDED
    return _range_bucket(value, RRR_BUCKETS, "盈亏比 ")


def _stop_distance_bucket(row: dict) -> str:
    """止损距离折成日波幅倍数。`stop_pct` 是负数，`dip.atr_pct` 是正的比例。"""
    stop = finite(row.get("stop_pct"))
    atr = finite((row.get("dip") or {}).get("atr_pct"))
    if stop is None or stop >= 0 or atr is None or atr <= 0:
        return UNRECORDED
    return _range_bucket(-stop / atr, STOP_ATR_BUCKETS, "止损 ", " 个日波幅")


def _range_bucket(
    value: float,
    buckets: tuple[tuple[float, float], ...],
    prefix: str,
    suffix: str = "",
) -> str:
    for low, high in buckets:
        if low <= value < high:
            span = f"≥{low:.1f}" if high == float("inf") else f"{low:.1f}-{high:.1f}"
            return f"{prefix}{span}{suffix}"
    return UNRECORDED


def _model_readiness(rows: list[dict]) -> dict[str, Any]:
    """样本够不够拟合，按三个维度一起判——**条数只是其中最弱的一个**。

    只看条数会在第 5 个交易日就亮绿灯，而那时有效样本量 n≈5。加上交易日数和
    情绪周期覆盖两道，是为了让「样本够了」这句话指的是「见过足够多**不同的
    行情**」，而不是「攒够了足够多**相关的记录**」。
    """
    sessions = {stamp for row in rows if (stamp := _trade_date(row)) is not None}
    regimes = {_regime_bucket(row) for row in rows if _regime_bucket(row) != UNRECORDED}
    checks = {
        "samples": (len(rows), MODEL_READY_SAMPLES),
        "sessions": (len(sessions), MODEL_READY_SESSIONS),
        "regimes": (len(regimes), MODEL_READY_REGIMES),
    }
    return {
        "ready": all(actual >= need for actual, need in checks.values()),
        "detail": {
            name: {"actual": actual, "required": need, "met": actual >= need}
            for name, (actual, need) in checks.items()
        },
        "regimes_seen": sorted(regimes),
    }


def _regime_bucket(row: dict) -> str:
    """情绪周期分档。

    优先读建议发出当时**闸门实际判定的那个标签**（`market_state.regime`）；只有
    老记录（闸门存在之前写的）才回退到用炸板率现算。回退档的切点与闸门共用，
    见模块顶部 `REGIME_HEALTHY` 的说明。
    """
    state = row.get("market_state") or {}
    if not isinstance(state, dict):
        return UNRECORDED
    recorded = state.get("regime")
    if isinstance(recorded, str) and recorded:
        return UNRECORDED if recorded == EmotionCycle.UNKNOWN.value else recorded

    rate = finite(state.get("blowup_rate"))
    if rate is None or not 0 <= rate <= 1:
        return UNRECORDED
    if rate < REGIME_HEALTHY:
        return f"封板结实(<{REGIME_HEALTHY:.0%})"
    if rate < REGIME_STRESSED:
        return f"有分歧({REGIME_HEALTHY:.0%}-{REGIME_STRESSED:.0%})"
    return f"退潮(≥{REGIME_STRESSED:.0%})"


def _macro_topic_buckets(row: dict) -> tuple[str, ...]:
    return _macro_buckets(row, "topics", MACRO_NONE)


def _macro_signal_buckets(row: dict) -> tuple[str, ...]:
    return _macro_buckets(row, "signals", MACRO_NO_SIGNAL)


def _macro_expectation_buckets(row: dict) -> tuple[str, ...]:
    return _macro_buckets(row, "expectations", MACRO_NO_EXPECTATION)


def _macro_buckets(row: dict, field: str, empty_bucket: str) -> tuple[str, ...]:
    context = row.get("macro_context")
    if not isinstance(context, dict) or not context:
        return (UNRECORDED,)
    if not context.get("recorded"):
        return (UNRECORDED,)
    values = tuple(
        sorted({str(item).strip() for item in (context.get(field) or []) if str(item).strip()})
    )
    return values or (empty_bucket,)


def _pullback_bucket(row: dict) -> str:
    band = (row.get("dip") or {}).get("pullback_band")
    return str(band) if band else UNRECORDED


def _state_label_bucket(row: dict, field: str) -> str:
    """取建议发出**当时**记录下来的 `market_state` 标签，取不到就落进未记录桶。

    轮动方向与指数背离两组分别调它一次。两处共用的不只是这三行，更是同一条
    纪律：**读记录下来的标签、不拿原始数现算**。现算会让阈值调整追溯改写历史
    ——调完之后同一条老记录落进另一个桶，而它当天实际报出去的结论并没有变。
    同一条纪律见 `_regime_bucket`（它多一层：老记录才回退到现算）。
    """
    label = (row.get("market_state") or {}).get(field)
    return str(label) if isinstance(label, str) and label else UNRECORDED


def _rotation_bucket(row: dict) -> str:
    """周度轮动方向。

    老记录（这一层上线之前写的）没有这个字段，落进「未记录」——那正是它该去的
    地方：把它们混进任何一个真实档位，都会用一段没测过的历史去支持一个结论。
    """
    return _state_label_bucket(row, "rotation_adjacent")


def _turnover_bucket(row: dict) -> str:
    """全场成交额是在放大还是收缩。

    存的是原始变化率、分档在这里定，因为这一层不改变系统行为——与 `_regime_bucket`
    必须复用闸门切点的理由正好相反，见模块顶部 `TURNOVER_EXPANDING` 的说明。
    """
    change = (row.get("market_state") or {}).get("turnover_total_change")
    if not isinstance(change, (int, float)):
        return UNRECORDED
    if change >= TURNOVER_EXPANDING:
        return f"放量(≥{TURNOVER_EXPANDING:.0%})"
    if change <= TURNOVER_SHRINKING:
        return f"缩量(≤{TURNOVER_SHRINKING:.0%})"
    return "平"


def _large_net_bucket(row: dict) -> str:
    """上一交易日大单净额的方向。

    只分正负、不分档：金额本身跨标的不可比（一只 300 亿成交的票和一只 3 亿的，
    大单净额差两个数量级），而**方向**是可比的。这与
    `scoring/context.py` 对资金流的处置同一条口径——单日绝对金额不直接跨标的比较。

    没有这个读数的记录落进「未记录」：`--no-trade-flow` 跑的那些批次、以及这一层
    上线之前的老记录都在里面，它们不该被算进任何一档。
    """
    flow = row.get("trade_flow") or {}
    if not flow.get("ok"):
        return UNRECORDED
    value = flow.get("large_net_amount")
    if not isinstance(value, (int, float)):
        return UNRECORDED
    return "大单净流入" if value > 0 else "大单净流出"


def _index_bucket(row: dict) -> str:
    """等权与市值加权是否背离。用记录下来的判定标签，不拿原始数现算——
    理由见 `_state_label_bucket`。
    """
    return _state_label_bucket(row, "index_divergence")


def _entry_basis_bucket(row: dict) -> str:
    entry = (row.get("adjudication") or {}).get("chosen_entry") or {}
    return str(entry.get("basis") or UNRECORDED)


def _decider_bucket(row: dict) -> str:
    verdict = row.get("adjudication") or {}
    if not verdict:
        return UNRECORDED
    if verdict.get("abstained"):
        return "模型弃权"
    if verdict.get("used_fallback"):
        return "规则默认"
    return "模型推翻规则" if verdict.get("overrode_rule") else "模型认可规则"


def _score_bucket(row: dict) -> str:
    score = finite(row.get("score"))
    if score is None or not 0 <= score <= 100:
        return "无分数"
    for low, high in SCORE_BUCKETS:
        if low <= score < high:
            return f"{low}-{min(high, 100)}"
    return "无分数"
