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
        "min_bucket": MIN_BUCKET,
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
        "positive_close_rate": (
            round(sum(1 for value in close if value > 0) / len(close), 4) if close else None
        ),
    }


def _score_bucket(row: dict) -> str:
    score = row.get("score")
    if not isinstance(score, (int, float)):
        return "无分数"
    for low, high in SCORE_BUCKETS:
        if low <= score < high:
            return f"{low}-{high if high <= 100 else 100}"
    return "无分数"
