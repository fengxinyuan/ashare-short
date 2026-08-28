"""给外部 AI 的月度展望资料包；只组织事实和研究任务，不生成预测。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.artifacts import LEADERS_LATEST_NAME
from ashare_decision_support.facts import build_facts_bundle
from ashare_decision_support.schedule.timezone import beijing_now
from ashare_decision_support.sectors import DEFAULT_LEVEL, SectorRotationStore

MONTHLY_SCHEMA = "ashare-monthly-research-context-v1"

_FIXED_EVENTS = (
    (1, 1, "元旦", ("消费", "旅游", "零售")),
    (5, 1, "劳动节", ("旅游", "酒店餐饮", "交通运输", "消费")),
    (6, 18, "618 电商节点", ("电商", "物流", "消费电子", "平台经济")),
    (10, 1, "国庆节", ("旅游", "酒店餐饮", "交通运输", "影视院线", "消费")),
    (11, 11, "双十一", ("电商", "物流", "消费", "广告营销")),
)


def build_monthly_context(
    *,
    output_dir: str | Path = "briefs",
    moment: datetime | None = None,
    horizon_days: int = 31,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """装配外部研究者所需的月度上下文，不访问搜索引擎、不调用模型。"""
    now = moment or beijing_now()
    horizon = max(7, min(int(horizon_days), 93))
    bundle = build_facts_bundle(moment=now, symbols=symbols, output_dir=output_dir)
    facts_payload = bundle.to_dict(compact_rendered=True)
    facts = facts_payload.get("facts", {})
    historical_validation = {
        "journal_stats": facts.get("台账统计"),
        "recent_validation": facts.get("近期验证"),
        "interpretation": (
            "仅描述项目历史记录的兑现情况；样本不足、滚动窗口重叠或缺少兑现记录时，"
            "不得当作未来收益概率。"
        ),
    }
    sector_rotation_memory, rotation_notes = _load_sector_rotation_memory(now.date())
    leaders, leader_notes = _load_json(Path(output_dir) / LEADERS_LATEST_NAME)
    leaders = _compact_leaders(leaders)
    end = now.date() + timedelta(days=horizon)
    events = _event_window(now.date(), end)
    return {
        "schema": MONTHLY_SCHEMA,
        "as_of": now.isoformat(timespec="seconds"),
        "horizon": {"days": horizon, "end_date": str(end)},
        "contract": {
            "project_role": "provide deterministic A-share facts and historical context",
            "external_ai_role": "collect current external events, verify dates, reason about transmission, and write scenarios",
            "prediction_policy": "the project does not claim that a symbol or sector will rise",
            "evidence_policy": "separate observed facts, externally verified events, and hypotheses",
        },
        "a_share_facts": facts_payload,
        "historical_validation": historical_validation,
        "sector_rotation_memory": sector_rotation_memory,
        "sector_leaders": leaders,
        "event_window": events,
        "external_research_tasks": _research_tasks(now.date(), end, events),
        "requested_output": {
            "sections": [
                "market regime and one-month base rate",
                "industry and concept leadership map",
                "event-to-A-share transmission chains",
                "three scenarios with confirmation and invalidation conditions",
                "candidate positions grouped by evidence strength",
            ],
            "rules": [
                "do not introduce a stock code absent from verified sources without labeling it unverified",
                "do not treat leadership_rank as probability or expected return",
                "cite the fact field or external source behind every numeric claim",
                "state what evidence would falsify each scenario",
            ],
        },
        "notes": [*bundle.notes, *leader_notes, *rotation_notes],
    }


def _load_sector_rotation_memory(trade_date: date) -> tuple[dict[str, Any], list[str]]:
    """读已经落盘的行业轮动记忆。**只读，不算**——写入方是盘前主链路。

    月度包不因为缺少记忆而失败：一份还没跑过 `morning` 的状态目录里，
    `summarize` 返回的是 regime=「样本不足」的摘要，那本身就是一条如实的读数。
    这里只兜住状态文件损坏那一类（`read_json` 已经把它降级成空记忆，剩下的是
    路径本身取不到）。
    """
    try:
        summary = SectorRotationStore().summarize(trade_date, DEFAULT_LEVEL)
    except (OSError, ValueError) as exc:
        return {}, [f"板块轮动记忆读取失败（{type(exc).__name__}）"]
    return (
        {
            "industry_level": summary.to_dict(),
            "interpretation": "只描述已落盘的近期板块排名稳定性，不是未来收益预测。",
        },
        [],
    )


def _load_json(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.is_file():
        return None, [f"未找到 {path.name}；先调用 refresh_sector_leaders 再做全板块龙头比较"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"{path.name} 读取失败（{type(exc).__name__}）"]
    if not isinstance(payload, dict):
        return None, [f"{path.name} 不是 JSON 对象"]
    return payload, []


def _compact_leaders(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep research identities and decisive metrics; full audit stays in its own artifact."""
    if not isinstance(payload, dict):
        return payload
    sectors = []
    for sector in payload.get("sectors") or []:
        if not isinstance(sector, dict):
            continue
        leaders = []
        for item in (sector.get("leaders") or [])[:3]:
            if not isinstance(item, dict):
                continue
            keep = (
                "symbol", "name", "leadership_rank", "signal_date", "last_close",
                "return_5d", "return_20d", "return_60d", "average_amount_20d_yi",
                "trend_state",
            )
            leaders.append({key: item.get(key) for key in keep if key in item})
        sectors.append(
            {
                key: value
                for key, value in {
                    "name": sector.get("name"),
                    "level": sector.get("level"),
                    "coverage_ratio": sector.get("coverage_ratio"),
                    "leaders": leaders,
                    "warnings": sector.get("warnings") or [],
                }.items()
                if value not in (None, [], {})
            }
        )
    return {
        "schema": payload.get("schema"),
        "view": "summary",
        "as_of": payload.get("as_of"),
        "contract": payload.get("contract"),
        "methodology": payload.get("methodology"),
        "sector_count": payload.get("sector_count", len(sectors)),
        "sectors": sectors,
        "omitted": {
            "leader_fields": ["rank_components", "volume_ratio_20d", "range_position_60d", "ma20", "ma60", "trend_quality"],
            "hint": "完整字段见 sector_leaders_latest.json",
        },
    }


def _event_window(start: date, end: date) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for year in range(start.year, end.year + 1):
        for month, day, name, channels in _FIXED_EVENTS:
            event_date = date(year, month, day)
            if start <= event_date <= end:
                events.append(
                    {
                        "name": name,
                        "date": str(event_date),
                        "date_status": "calendar rule",
                        "possible_channels": list(channels),
                        "status": "research hypothesis; historical effect must be measured",
                    }
                )
        if year % 2 == 0:
            election = _us_federal_election_day(year)
            if start <= election <= end:
                events.append(
                    {
                        "name": "美国联邦选举日",
                        "date": str(election),
                        "date_status": "statutory calendar rule",
                        "possible_channels": ["全球风险偏好", "美元", "美债", "贸易政策", "科技限制"],
                        "status": "outcome, policy path, and A-share mapping require current external sources",
                    }
                )
    events.sort(key=lambda item: item["date"])
    return events


def _us_federal_election_day(year: int) -> date:
    day = date(year, 11, 1)
    while day.weekday() != 0:
        day += timedelta(days=1)
    return day + timedelta(days=1)


def _research_tasks(start: date, end: date, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "topic": "movable and newly announced events",
            "query_scope": f"{start}..{end}",
            "must_verify": ["中秋/春节等农历节日", "两会及政策会议", "央行议息", "中外重要选举", "重大产品发布"],
            "required_sources": "official calendars, regulators, central banks, exchanges, and primary company releases",
        },
        {
            "topic": "event transmission",
            "events": [item["name"] for item in events],
            "must_answer": ["事件是否已被定价", "映射到哪些 A 股行业/概念", "量价是否确认", "何种事实会证伪"],
        },
        {
            "topic": "business leadership verification",
            "must_answer": ["候选公司的真实主营和行业地位", "订单/业绩/估值约束", "是否只是题材标签", "最近公告风险"],
            "note": "本项目给的是量价龙头候选，产业龙头身份必须由外部资料核验",
        },
    ]
