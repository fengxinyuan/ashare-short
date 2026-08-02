"""兑现统计：这套打分到底有没有用。

只报告，不调权重。样本量不足时按统计结果改参数，改的是噪声——系统会看起来在
进步，实际在漂移。所以这里的产出是给人看的表，不是回写到打分器的系数。

`MIN_BUCKET` 以下的分组照样列出来但标注不可信，因为「样本太少」这件事本身
就是结论的一部分，藏起来只会让人误读那个数字。
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any, Callable

# 一个分组低于这个样本数，统计量不作为判断依据。
MIN_BUCKET = 20

# 建议台账里有 200 条已兑现记录之前，不考虑上拟合模型。
MODEL_READY_SAMPLES = 200

SCORE_BUCKETS = ((0, 60), (60, 70), (70, 80), (80, 101))

# 炸板率的情绪周期切分。低于 HEALTHY 封板结实（启动/主升），高于 STRESSED
# 封板意愿崩塌（退潮）。同一套规则在这两种状态下的期望值可以是反号的，混在
# 一起统计只会得到一个谁都不像的平均数。
REGIME_HEALTHY = 0.30
REGIME_STRESSED = 0.50


def summarize(outcomes: list[dict]) -> dict[str, Any]:
    """按整体、分数桶、板块、趋势状态分组统计。"""
    usable = [row for row in outcomes if row.get("kind") == "outcome" or "mfe_pct" in row]
    return {
        "samples": len(usable),
        "model_ready": len(usable) >= MODEL_READY_SAMPLES,
        "model_ready_gap": max(0, MODEL_READY_SAMPLES - len(usable)),
        "overall": _stats(usable),
        "by_score": _group(usable, _score_bucket),
        "by_sector": _group(usable, lambda row: row.get("sector") or "未分类"),
        "by_trend": _group(usable, lambda row: row.get("trend_state") or "未知"),
        # 入选 vs 对照。这一组是全表里唯一能回答「这套筛选本身有没有用」的：
        # 入选组次日涨 1.2% 听起来不错，但若对照组涨 1.4%，规则就是负价值。
        "by_group": _group(usable, lambda row: row.get("rec_kind") or "recommendation"),
        # 情绪周期分层。超短最重要的分层维度，此前完全没有。
        "by_regime": _group(usable, _regime_bucket),
        # 回调深度分层。低吸的核心问题「调多少买最好」只有这一组能回答。
        "by_pullback": _group(usable, _pullback_bucket),
        "selection_edge": _selection_edge(usable),
        "min_bucket": MIN_BUCKET,
    }


def _selection_edge(rows: list[dict]) -> dict[str, Any]:
    """入选组减对照组。**这个差值才是这套规则的真实价值。**

    单看入选组的绝对收益毫无意义——普涨的日子里随便选都赚。两组同日、同筛选
    流程、只差最后一刀排序，差值才把市场本身的涨跌消掉了。
    """
    picked = [row for row in rows if (row.get("rec_kind") or "recommendation") == "recommendation"]
    control = [row for row in rows if row.get("rec_kind") == "control"]
    if not picked or not control:
        return {"available": False, "reason": "两组都有样本才能算差值"}

    def gap(field: str) -> float | None:
        left = [row[field] for row in picked if isinstance(row.get(field), (int, float))]
        right = [row[field] for row in control if isinstance(row.get(field), (int, float))]
        if not left or not right:
            return None
        return round(median(left) - median(right), 4)

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


def _group(rows: list[dict], key: Callable[[dict], str]) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[key(row)].append(row)
    out = [{"bucket": name, **_stats(items)} for name, items in buckets.items()]
    return sorted(out, key=lambda item: item["bucket"])


def _stats(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"samples": 0, "trusted": False}

    entered = [row for row in rows if row.get("entered")]
    mfe = [row["mfe_pct"] for row in rows if isinstance(row.get("mfe_pct"), (int, float))]
    mae = [row["mae_pct"] for row in rows if isinstance(row.get("mae_pct"), (int, float))]
    close = [row["close_pct"] for row in rows if isinstance(row.get("close_pct"), (int, float))]
    next_open = [
        row["next_open_pct"] for row in rows if isinstance(row.get("next_open_pct"), (int, float))
    ]
    next_close = [
        row["next_close_pct"] for row in rows if isinstance(row.get("next_close_pct"), (int, float))
    ]
    first_target = sum(1 for row in rows if row.get("first_hit") == "target")
    first_stop = sum(1 for row in rows if row.get("first_hit") == "stop")
    decided = first_target + first_stop

    return {
        "samples": len(rows),
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


def _regime_bucket(row: dict) -> str:
    """按炸板率切情绪周期。没记市场状态的老记录单独一档，不混进任何一类。"""
    rate = (row.get("market_state") or {}).get("blowup_rate")
    if not isinstance(rate, (int, float)):
        return "未记录"
    if rate < REGIME_HEALTHY:
        return f"封板结实(<{REGIME_HEALTHY:.0%})"
    if rate < REGIME_STRESSED:
        return f"有分歧({REGIME_HEALTHY:.0%}-{REGIME_STRESSED:.0%})"
    return f"退潮(≥{REGIME_STRESSED:.0%})"


def _pullback_bucket(row: dict) -> str:
    band = (row.get("dip") or {}).get("pullback_band")
    return str(band) if band else "未记录"


def _score_bucket(row: dict) -> str:
    score = row.get("score")
    if not isinstance(score, (int, float)):
        return "无分数"
    for low, high in SCORE_BUCKETS:
        if low <= score < high:
            return f"{low}-{high if high <= 100 else 100}"
    return "无分数"
