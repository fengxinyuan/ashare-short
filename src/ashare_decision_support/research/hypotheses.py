"""按主题簇组合盘前情景、外盘映射与历史基准率。

簇名和证伪条件与盘中确认共用，证据分别标为已发生、历史统计或推演。
外盘指向但未进入盘前前列的方向单独提示，不加入假设或评分。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ashare_decision_support.research.sector_forecast import (
    build_sector_forecast,
    execution_protocol,
    target_session,
)
from ashare_decision_support.schedule.calendar import next_session
from ashare_decision_support.sectors.clusters import cluster_of

SCHEMA = "ashare-preopen-hypotheses-v1"

# 先手观察最多几条。研报要求写的是「1-3 条先手观察」，多于三条就不是先手而是清单。
MAX_HYPOTHESES = 3
# 每条假设下最多带几条基准率。基准率按板块出，一个簇里五个板块乘两个外盘品种就是
# 十行，读的人只会看头两行。
MAX_BASE_RATES = 2

EVIDENCE_HAPPENED = "已发生"
EVIDENCE_HISTORY = "历史统计"
EVIDENCE_INFERENCE = "推演"

STATUS_PENDING = "待验证"
VERIFY_WITH = "09:35 后运行 `ashare-ds sector-confirmation`，按假设名逐条对账"


def build_sector_hypotheses(
    morning: dict[str, Any] | None,
    overseas: dict[str, Any] | None,
    *,
    session_date: date | None = None,
) -> dict[str, Any]:
    """盘前主链路 + 外盘快照 → 假设清单。纯计算，不碰网络与磁盘。

    `morning` 为空时返回空清单。盘前组包传入 `session_date`，只把**针对同一个交易日**
    的计划作为当前假设：周日跑的计划和周一盘前的研究对的是同一个周一，而上周五
    的计划不是。历史或日期不明的排序仍由外层事实包保存，不能冒充当天预案。
    """
    protocol = execution_protocol()
    unavailable = None
    if not isinstance(morning, dict) or not morning.get("sectors"):
        unavailable = "尚无当日盘前主链路落盘结果，不能立假设；先运行 `ashare-ds morning`"
    elif session_date is not None and (
        target_session(morning.get("trade_date")) != next_session(session_date).isoformat()
    ):
        unavailable = (
            f"盘前主链路日期 {morning.get('trade_date') or '未记录'} 与研究日期 {session_date} "
            "对应的交易日不一致或无效，不能作为当日假设；先运行 `ashare-ds morning`"
        )
    if unavailable:
        return {
            "schema": SCHEMA,
            "target_session": None,
            "items": [],
            "overseas_only": [],
            "protocol": protocol,
            "notes": [unavailable],
        }

    forecast = build_sector_forecast(facts=morning)
    links = _links(overseas)
    base_rates = _base_rates(overseas)
    items = [
        _hypothesis(row, links, base_rates, protocol)
        for row in (forecast.get("forecasts") or [])[:MAX_HYPOTHESES]
    ]
    covered = {name for item in items for name in item["members"]}
    covered |= {item["name"] for item in items}
    return {
        "schema": SCHEMA,
        "target_session": forecast.get("target_session"),
        "information_date": forecast.get("information_date"),
        "market_state": (forecast.get("market") or {}).get("state"),
        "items": items,
        "overseas_only": _overseas_only(links, covered),
        "protocol": protocol,
        "notes": [
            "假设按主题簇立，同簇内的行业与概念只算一组暴露；名字与盘中确认结果的 name 一致。",
            "证据分「已发生 / 历史统计 / 推演」三类；只有第一类是事实，后两类不能当方向读。",
            "证伪条件取自盘中确认协议，盘前写下之后不再改；被否决的假设按否决记账，不补新理由。",
        ],
    }


def _hypothesis(
    row: dict[str, Any],
    links: list[dict[str, Any]],
    base_rates: list[dict[str, Any]],
    protocol: dict[str, Any],
) -> dict[str, Any]:
    name = str(row.get("name") or "")
    members = [str(item) for item in row.get("members") or []]
    supporting, opposing = _split_links(name, members, links)
    rates = _rates_for(name, members, base_rates)

    evidence: list[dict[str, str]] = []
    evidence.extend(
        {"kind": EVIDENCE_HAPPENED, "text": item["headline"]} for item in (*supporting, *opposing)
    )
    evidence.extend(
        {"kind": EVIDENCE_HISTORY, "text": str(item.get("headline"))}
        for item in rates
        if item.get("headline")
    )
    evidence.append({"kind": EVIDENCE_INFERENCE, "text": _inference_text(row)})

    return {
        "name": name,
        "members": members,
        "scenario": row.get("scenario"),
        "score": row.get("score"),
        "ret_1d": row.get("ret_1d"),
        "ret_5d": row.get("ret_5d"),
        "breadth": row.get("breadth"),
        "independent_signal": bool(row.get("independent_signal")),
        "overseas": {
            "supporting": [item["headline"] for item in supporting],
            "opposing": [item["headline"] for item in opposing],
            # 同一个簇同时被利多与利空链路指向。**这是信息，不是噪声**：2026-08-20
            # 韩股 +5.89% 与费半 −2.12% 同夜指向半导体，压成净方向会把当晚最重要的
            # 一条抹掉。
            "conflicted": bool(supporting and opposing),
        },
        "base_rates": [item.get("headline") for item in rates],
        "evidence": evidence,
        "confirm_after": protocol["confirm_after"],
        "confirm": list(protocol["confirm"]),
        "invalidate": list(protocol["invalidate"]),
        "status": STATUS_PENDING,
        "verify_with": VERIFY_WITH,
    }


def _inference_text(row: dict[str, Any]) -> str:
    score = row.get("score")
    scenario = row.get("scenario") or "等待方向确认"
    shown = f"{score:.1f}" if isinstance(score, int | float) else "—"
    tail = "" if row.get("independent_signal") else "；簇内成员重叠，多板块同涨只算一条证据"
    return f"盘前排序评分 {shown}，情景「{scenario}」——排序是优先级不是预测{tail}"


def _links(overseas: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(overseas, dict):
        return []
    rows = (overseas.get("sector_links") or {}).get("links") or []
    return [item for item in rows if isinstance(item, dict)]


def _base_rates(overseas: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(overseas, dict):
        return []
    rows = (overseas.get("sector_base_rates") or {}).get("items") or []
    return [item for item in rows if isinstance(item, dict)]


def _split_links(
    cluster: str,
    members: list[str],
    links: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """指向这条假设的链路，按方向分成两堆。

    一条链路的 `targets` 是板块名（「半导体」「存储芯片」），假设名是簇名（「科技硬件」）。
    对齐走两条路：板块名正好是簇成员，或板块名按同一张归簇表归到这个簇。后者是主要
    路径——外盘映射表里的行业「半导体」与盘前排序的「芯片概念」可归到同一簇，
    在保留行业、概念维度的同时关联它们的证据。
    """
    member_set = set(members)
    supporting: list[dict[str, Any]] = []
    opposing: list[dict[str, Any]] = []
    for link in links:
        targets = [str(item) for item in link.get("targets") or []]
        hit = [t for t in targets if t in member_set or cluster_of(t) == cluster]
        if not hit:
            continue
        entry = {**link, "hit_targets": hit, "headline": _link_text(link, hit)}
        (supporting if link.get("direction") == "bullish" else opposing).append(entry)
    return supporting, opposing


def _link_text(link: dict[str, Any], targets: list[str]) -> str:
    change = link.get("change_pct")
    move = f"{change:+.2f}%" if isinstance(change, int | float) else "—"
    tag = "强" if link.get("strength") == "strong" else ""
    arrow = "利多" if link.get("direction") == "bullish" else "利空"
    return (
        f"{link.get('instrument_name', link.get('instrument', '—'))} {move} → "
        f"{'、'.join(targets)} {tag}{arrow}（{link.get('mechanism', '—')}）"
    )


def _rates_for(
    cluster: str, members: list[str], base_rates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """簇成员对应的基准率，**有样本的排前面**，最多取几条。

    `trusted` 的排在前面而不是过滤掉不可信的：样本不足的读数照样报（藏起来只会让人
    误读剩下的那个数字），但先看能用的。对齐口径与 `_split_links` 相同：板块名是
    簇成员，或按同一张归簇表归到这个簇。
    """
    member_set = set(members)
    matched = [
        item
        for item in base_rates
        if str(item.get("sector")) in member_set or cluster_of(str(item.get("sector"))) == cluster
    ]
    matched.sort(
        key=lambda item: (bool(item.get("trusted")), int(item.get("samples") or 0)),
        reverse=True,
    )
    return matched[:MAX_BASE_RATES]


def _overseas_only(links: list[dict[str, Any]], covered: set[str]) -> list[dict[str, Any]]:
    """外盘指向、但盘前排序没进前列的板块。

    按板块聚合而不是按链路：同一个板块被三条链路指向，读者要的是「半导体：三条」
    而不是三行。这一段只观察，不进假设、不进打分——见 `ashare-no-score-discipline`：
    映射为强正向的板块进观察名单，标注进入理由是外盘映射，不改变排序。
    """
    by_sector: dict[str, list[str]] = {}
    for link in links:
        for target in link.get("targets") or []:
            sector = str(target)
            if sector in covered or cluster_of(sector) in covered:
                continue
            by_sector.setdefault(sector, []).append(_link_text(link, [sector]))
    return [
        {"sector": sector, "links": rows, "reason": "外盘映射，未进盘前前列；只观察不打分"}
        for sector, rows in by_sector.items()
    ]
