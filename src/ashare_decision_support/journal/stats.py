"""兑现统计：这套打分到底有没有用。

只报告，不调权重。样本量不足时按统计结果改参数，改的是噪声——系统会看起来在
进步，实际在漂移。所以这里的产出是给人看的表，不是回写到打分器的系数。

`MIN_BUCKET` 以下的分组照样列出来但标注不可信，因为「样本太少」这件事本身
就是结论的一部分，藏起来只会让人误读那个数字。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date
from statistics import median
from typing import Any

from ashare_decision_support.numeric import finite
from ashare_decision_support.strategy.regime import BLOWUP_HEALTHY, BLOWUP_STRESSED

# 一个分组低于这个样本数，统计量不作为判断依据。
MIN_BUCKET = 20

# 建议台账里有 200 条已兑现记录之前，不考虑上拟合模型。
#
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

# 炸板率的情绪周期切分，**与实时闸门共用同一组切点**（`strategy/regime.py`）。
# 两边各定一套是个隐蔽的坑：闸门按 0.50 切、统计按 0.55 切，那么「退潮期这套
# 规则管不管用」的样本就和真正被闸门拦下的日子对不上，闸门永远无法被自己的
# 台账验证。
REGIME_HEALTHY = BLOWUP_HEALTHY
REGIME_STRESSED = BLOWUP_STRESSED

# 全场成交额相对变化的分档，用于 `by_turnover` 分组。
#
# 和上面那组的处置**刻意不同**：情绪周期的切点必须与实时闸门共用，因为那是一个
# 会改变系统行为的闸门；成交额这一层不改任何行为，只是一个被存下来的数，所以
# 分档定在统计侧、由这里说了算。存的是原始数不是标签，也正是这个原因。
TURNOVER_EXPANDING = 0.05
TURNOVER_SHRINKING = -0.05
MACRO_UNRECORDED = "未记录"
MACRO_NONE = "无明确宏观链路"
MACRO_NO_SIGNAL = "无方向信号"
MACRO_NO_EXPECTATION = "无预期差"


def summarize(outcomes: list[dict]) -> dict[str, Any]:
    """按整体、分数桶、板块、趋势状态分组统计。"""
    usable = [row for row in outcomes if row.get("kind") == "outcome" or "mfe_pct" in row]
    readiness = _model_readiness(usable)
    return {
        "samples": len(usable),
        "model_ready": readiness["ready"],
        "model_ready_gap": max(0, MODEL_READY_SAMPLES - len(usable)),
        "readiness": readiness,
        "overall": _stats(usable),
        "by_score": _group(usable, _score_bucket),
        "by_sector": _group(usable, lambda row: row.get("sector") or "未分类"),
        "by_trend": _group(usable, lambda row: row.get("trend_state") or "未知"),
        # 按打法分组。回踩和突破的止损距离、持有期本来就不同，混在一起算出的
        # 平均兑现率不描述其中任何一类。这一组是「该保留哪几条打法」的答案位置。
        "by_playbook": _group(usable, lambda row: row.get("playbook") or "未记录"),
        # 入选 vs 对照。这一组是全表里唯一能回答「这套筛选本身有没有用」的：
        # 入选组次日涨 1.2% 听起来不错，但若对照组涨 1.4%，规则就是负价值。
        "by_group": _group(usable, lambda row: row.get("rec_kind") or "recommendation"),
        # 情绪周期分层。超短最重要的分层维度，此前完全没有。
        "by_regime": _group(usable, _regime_bucket),
        # 宏观链路是日级公共上下文，不直接进打分；这三组是它的验收位。
        "by_macro_topic": _multi_group(usable, _macro_topic_buckets),
        "by_macro_signal": _multi_group(usable, _macro_signal_buckets),
        "by_macro_expectation": _multi_group(usable, _macro_expectation_buckets),
        # ---- 以下三组是「只出读数、不进打分」那几层的**验收位**。
        # 它们存在的全部意义是回答同一个问题的三个版本：那一层报出来的东西，
        # 事后看有没有区分度。没有区分度就该删掉，而不是留着当装饰。
        #
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
        "selection_edge": _selection_edge(usable),
        # 闸门的代价。和 selection_edge 分开报：一个衡量排序，一个衡量否决，
        # 两者可以一好一坏，合成一个数会互相抵消掉。
        "veto_cost": _veto_cost(usable),
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
    usable = [row for row in outcomes if row.get("kind") == "outcome" or "mfe_pct" in row]
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
        "by_macro_expectation": _compact_buckets(
            _multi_group(rows, _macro_expectation_buckets)
        ),
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
    overrode = [row for row in rows if (row.get("adjudication") or {}).get("overrode_rule")]
    default = [
        row
        for row in rows
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

    def gap(field: str) -> float | None:
        return _median_gap(overrode, default, field)

    return {
        "available": True,
        "overrode_samples": len(overrode),
        "default_samples": len(default),
        "abstained_samples": abstained,
        "next_close_gap": gap("next_close_pct"),
        "close_gap": gap("close_pct"),
        "mfe_gap": gap("mfe_pct"),
        "mae_gap": gap("mae_pct"),
        "trusted": len(overrode) >= MIN_BUCKET and len(default) >= MIN_BUCKET,
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
    if not picked or not control:
        return {"available": False, "reason": "两组都有样本才能算差值"}

    def gap(field: str) -> float | None:
        return _median_gap(picked, control, field)

    return {
        "available": True,
        "picked_samples": len(picked),
        "control_samples": len(control),
        # 三个口径各算一遍：次日开盘（隔夜跳空）、次日收盘、五日期末。
        "next_open_gap": gap("next_open_pct"),
        "next_close_gap": gap("next_close_pct"),
        "close_gap": gap("close_pct"),
        "mfe_gap": gap("mfe_pct"),
        "trusted": len(picked) >= MIN_BUCKET and len(control) >= MIN_BUCKET,
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
    if not picked or not vetoed:
        return {"available": False, "reason": "入选组与否决组都有样本才能算差值"}

    def gap(field: str) -> float | None:
        return _median_gap(vetoed, picked, field)

    return {
        "available": True,
        "vetoed_samples": len(vetoed),
        "picked_samples": len(picked),
        # 正数 = 被否决的那批表现更好 = 闸门拦错了。
        "next_close_gap": gap("next_close_pct"),
        "close_gap": gap("close_pct"),
        "mfe_gap": gap("mfe_pct"),
        "trusted": len(vetoed) >= MIN_BUCKET and len(picked) >= MIN_BUCKET,
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


def _median_gap(left: list[dict], right: list[dict], field: str) -> float | None:
    """两组在同一列上的中位数之差。任一组没有可用样本就返回 None。

    三处差值统计（模型改单 / 入选对照 / 否决代价）此前各自内联了一份逐字相同的
    闭包。差的只是哪两组相减，而**方向是有语义的**——见各调用处的注释：
    `_selection_edge` 正数表示选得好，`_veto_cost` 正数表示闸门拦错了。
    """
    a, b = _column(left, field), _column(right, field)
    if not a or not b:
        return None
    return round(median(a) - median(b), 4)


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
    sessions = {row.get("trade_date") for row in rows if row.get("trade_date")}
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
    }


def _model_readiness(rows: list[dict]) -> dict[str, Any]:
    """样本够不够拟合，按三个维度一起判——**条数只是其中最弱的一个**。

    只看条数会在第 5 个交易日就亮绿灯，而那时有效样本量 n≈5。加上交易日数和
    情绪周期覆盖两道，是为了让「样本够了」这句话指的是「见过足够多**不同的
    行情**」，而不是「攒够了足够多**相关的记录**」。
    """
    sessions = {row.get("trade_date") for row in rows if row.get("trade_date")}
    regimes = {
        _regime_bucket(row)
        for row in rows
        if _regime_bucket(row) != "未记录"
    }
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
    recorded = state.get("regime")
    if isinstance(recorded, str) and recorded:
        return recorded

    rate = state.get("blowup_rate")
    if not isinstance(rate, (int, float)):
        return "未记录"
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
        return (MACRO_UNRECORDED,)
    if not context.get("recorded"):
        return (MACRO_UNRECORDED,)
    values = tuple(
        sorted(
            {
                str(item).strip()
                for item in (context.get(field) or [])
                if str(item).strip()
            }
        )
    )
    return values or (empty_bucket,)


def _pullback_bucket(row: dict) -> str:
    band = (row.get("dip") or {}).get("pullback_band")
    return str(band) if band else "未记录"


def _rotation_bucket(row: dict) -> str:
    """周度轮动方向。读的是建议发出当时记录下来的标签，不现算。

    老记录（这一层上线之前写的）没有这个字段，落进「未记录」——那正是它该去的
    地方：把它们混进任何一个真实档位，都会用一段没测过的历史去支持一个结论。
    """
    label = (row.get("market_state") or {}).get("rotation_adjacent")
    return str(label) if isinstance(label, str) and label else "未记录"


def _turnover_bucket(row: dict) -> str:
    """全场成交额是在放大还是收缩。

    存的是原始变化率、分档在这里定，因为这一层不改变系统行为——与 `_regime_bucket`
    必须复用闸门切点的理由正好相反，见模块顶部 `TURNOVER_EXPANDING` 的说明。
    """
    change = (row.get("market_state") or {}).get("turnover_total_change")
    if not isinstance(change, (int, float)):
        return "未记录"
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
        return "未记录"
    value = flow.get("large_net_amount")
    if not isinstance(value, (int, float)):
        return "未记录"
    return "大单净流入" if value > 0 else "大单净流出"


def _index_bucket(row: dict) -> str:
    """等权与市值加权是否背离。用记录下来的判定标签，不拿原始数现算。

    现算会让阈值调整**追溯改写历史**：调完之后，同一条老记录会落进另一个桶，
    而它当天实际报出去的结论并没有变。同一条纪律见 `_regime_bucket`。
    """
    label = (row.get("market_state") or {}).get("index_divergence")
    return str(label) if isinstance(label, str) and label else "未记录"


def _entry_basis_bucket(row: dict) -> str:
    entry = (row.get("adjudication") or {}).get("chosen_entry") or {}
    return str(entry.get("basis") or "未记录")


def _decider_bucket(row: dict) -> str:
    verdict = row.get("adjudication") or {}
    if not verdict:
        return "未记录"
    if verdict.get("abstained"):
        return "模型弃权"
    if verdict.get("used_fallback"):
        return "规则默认"
    return "模型推翻规则" if verdict.get("overrode_rule") else "模型认可规则"


def _score_bucket(row: dict) -> str:
    score = row.get("score")
    if not isinstance(score, (int, float)):
        return "无分数"
    for low, high in SCORE_BUCKETS:
        if low <= score < high:
            return f"{low}-{min(high, 100)}"
    return "无分数"
