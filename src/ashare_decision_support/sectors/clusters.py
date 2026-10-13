"""Group overlapping ranked sectors into interpretable risk exposures."""

from __future__ import annotations

from typing import Any

CLUSTERS = (
    (
        "科技硬件",
        (
            "芯片",
            "半导体",
            "电子",
            "通信",
            "CPO",
            "光学",
            "存储",
            "PCB",
            "算力",
            "数据中心",
            "液冷",
        ),
    ),
    ("资源金属", ("有色", "黄金", "金属", "稀土", "煤炭", "石油", "能源金属")),
    ("农业食品", ("农林牧渔", "种植", "粮食", "转基因", "养殖", "食品", "农业")),
    ("医药医疗", ("医药", "医疗", "生物", "创新药", "中药")),
    ("金融", ("证券", "银行", "保险", "金融")),
    ("新能源电力", ("电力", "储能", "光伏", "风电", "电网", "锂电")),
)


def cluster_ranked_sectors(sectors: list[Any]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    ungrouped: list[str] = []
    for item in sectors:
        row = item if isinstance(item, dict) else _outlook_row(item)
        if not row:
            continue
        name = str(row.get("name") or "")
        cluster = cluster_of(name)
        if cluster is None:
            ungrouped.append(name)
            continue
        groups.setdefault(cluster, []).append(
            {
                "name": name,
                "level": row.get("level"),
                "score": row.get("score"),
                "ret_1d": row.get("ret_1d"),
                "ret_5d": row.get("ret_5d"),
            }
        )
    ranked = sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0]))
    total = max(1, len(sectors))
    clusters = [
        {
            "name": name,
            "sectors": rows,
            "seats": len(rows),
            "seat_share": round(len(rows) / total, 4),
            "max_score": max(
                (float(row["score"]) for row in rows if isinstance(row.get("score"), int | float)),
                default=None,
            ),
        }
        for name, rows in ranked
    ]
    crowded = [item for item in clusters if item["seats"] >= 3 or item["seat_share"] >= 0.5]
    return {
        "clusters": clusters,
        "ungrouped": ungrouped,
        "crowded": crowded,
        "interpretation": ("同一主题簇内的行业/概念高度重叠，只算一组风险暴露，不算多条独立确认。"),
    }


def cluster_of(name: str) -> str | None:
    """板块名 → 主题簇名；对不上任何簇返回 None。

    公开出来是给 `research/preopen.py` 用的：外盘映射指向的是「半导体」「存储芯片」
    这类板块名，而盘前假设是按簇立的——「费半 −2% 利空半导体」要能落到「科技硬件」
    这条假设上，两边得用**同一张**归簇表，否则同一个板块名在两处归到不同的簇。
    """
    upper = name.upper()
    for cluster, terms in CLUSTERS:
        if any(term.upper() in upper for term in terms):
            return cluster
    return None


def _outlook_row(item: Any) -> dict[str, Any]:
    metrics = getattr(item, "metrics", None)
    name = getattr(item, "name", None)
    if not name:
        return {}
    return {
        "name": name,
        "level": getattr(item, "level", None),
        "score": getattr(item, "score", None),
        "ret_1d": getattr(metrics, "ret_1d", None),
        "ret_5d": getattr(metrics, "ret_5d", None),
    }
