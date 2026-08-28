"""读取最近一次盘前产出的每日板块分析。

`morning` 负责取数和计算，本模块只读取已落盘的事实，不访问网络、不重复计算。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.artifacts import MORNING_LATEST_NAME
from ashare_decision_support.runhealth import STATUS_STALE, HealthLedger
from ashare_decision_support.schedule.timezone import beijing_today

SCHEMA = "ashare-sector-analysis-v1"
_FACT_KEYS = (
    "market",
    "sectors",
    "sector_index",
    "rotation_price",
    "turnover_migration",
    "seesaw",
    "rotation",
    "limit_pool",
    "news_coverage",
    "sector_forecast_validation",
    "sector_clusters",
    "data_notes",
)


def load_daily_sector_analysis(output_dir: str | Path = "briefs") -> dict[str, Any]:
    """读取最近一次 `morning` 的板块事实包。"""
    path = Path(output_dir) / MORNING_LATEST_NAME
    if not path.is_file():
        raise FileNotFoundError(
            f"未找到 {path}；先运行 `ashare-ds morning` 生成每日板块分析"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 {path}：{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 不是有效的板块事实包")

    trade_date = payload.get("trade_date")
    today = beijing_today()
    return {
        "schema": SCHEMA,
        "source": str(path),
        "trade_date": trade_date,
        "data_health": _data_health(trade_date, today),
        "available": [key for key in _FACT_KEYS if payload.get(key) is not None],
        **{key: payload.get(key) for key in _FACT_KEYS},
    }


def render_daily_sector_analysis(payload: dict[str, Any]) -> str:
    """把板块事实渲染成短报告，不添加新判断。"""
    trade_date = payload.get("trade_date") or "未知日期"
    lines = [
        f"# 每日板块分析 {trade_date}",
        "",
        f"数据日期：{trade_date}",
        f"数据来源：`{payload.get('source', '未知')}`",
    ]
    health = payload.get("data_health") or {}
    if health.get("is_stale"):
        lines += [
            (
                f"⚠️ 数据已落后 {health.get('sessions_behind')} 个交易日"
                f"（今天 {health.get('today')}）。"
                "这是历史复看，不适合作为“现在”的板块判断；先运行 `ashare-ds morning` 刷新。"
                + (f"（{health['caveat']}）" if health.get("caveat") else "")
            ),
        ]

    market = payload.get("market") or {}
    lines += ["", "## 大盘背景"]
    lines.append(
        f"- 方向：{market.get('direction') or '—'}；综合读数：{_score(market.get('score'))}；"
        f"等权宽度：{_percent(market.get('breadth'))}；5日中位收益：{_percent(market.get('median_ret_5d'))}"
    )
    _append_values(lines, "大盘提示", market.get("warnings") or [])

    sectors = payload.get("sectors") or []
    lines += ["", "## 主攻板块"]
    if not sectors:
        lines.append("- 没有可用的主攻板块数据。")
    for item in sectors:
        lines.append(
            f"- **{item.get('name', '未知')}**（{item.get('level', '—')}）："
            f"{item.get('direction') or '—'}，评分 {_score(item.get('score'))}，"
            f"1日 {_percent(item.get('ret_1d'))}，5日 {_percent(item.get('ret_5d'))}，"
            f"广度 {_percent(item.get('breadth'))}，覆盖 {item.get('covered', '—')}/{item.get('constituents', '—')}"
        )
        lines.extend(f"  - {reason}" for reason in (item.get("reasons") or [])[:2])
        lines.extend(f"  - 注意：{warning}" for warning in (item.get("warnings") or [])[:2])

    rotation = payload.get("rotation_price")
    turnover = payload.get("turnover_migration")
    if rotation or turnover:
        lines += ["", "## 轮动与成交额"]
    if rotation:
        lines += [
            f"- 价格口径：{rotation.get('switch_label') or '—'}（周度：{rotation.get('adjacent_label') or '—'}），截至 {rotation.get('trade_date') or '—'}",
            "- 排名上升：" + _names(rotation.get("rising")),
            "- 排名下降：" + _names(rotation.get("falling")),
        ]
    if turnover:
        lines += [
            f"- 成交额总量变化：{_percent(turnover.get('total_change'))}；近端趋势：{_percent(turnover.get('total_trend'))}",
            "- 占比上升：" + _names(turnover.get("rising")),
            "- 放量下跌：" + _names(turnover.get("distribution")),
        ]

    index = payload.get("sector_index")
    if index:
        lines += [
            "",
            "## 行业指数位置",
            "- 上涨靠前：" + _names(index.get("gainers")),
            "- 下跌靠前：" + _names(index.get("losers")),
        ]
        if index.get("faded"):
            lines.append("- 上涨但收在区间低位：" + _names(index["faded"]))

    limit_pool = payload.get("limit_pool")
    if limit_pool:
        lines += [
            "",
            "## 情绪旁证",
            (
                f"- 涨停 {limit_pool.get('limit_up', '—')}，"
                f"跌停 {limit_pool.get('limit_down', '—')}，"
                f"炸板率 {_percent(limit_pool.get('blowup_rate'))}，"
                f"最高连板 {limit_pool.get('max_streak', '—')}"
            ),
        ]

    _append_forecast_validation(lines, payload.get("sector_forecast_validation"))
    _append_sector_clusters(lines, payload.get("sector_clusters"))

    notes = payload.get("data_notes") or []
    if notes:
        lines += ["", "## 数据边界"]
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines)


def _append_forecast_validation(lines: list[str], value: Any) -> None:
    validation = value if isinstance(value, dict) else {}
    if not validation:
        return
    lines += ["", "## 历史预测验证"]
    for item in validation.get("horizons") or []:
        win_rate = item.get("win_rate")
        excess = item.get("mean_excess_return")
        lines.append(
            f"- {item.get('horizon', '—')}日：样本 {item.get('samples', 0)}，"
            f"跑赢率 {_percent(win_rate) if win_rate is not None else '—'}，"
            f"平均超额 {_percent(excess) if excess is not None else '—'}"
        )
    if not validation.get("trusted"):
        lines.append("- ⚠️ 样本尚不足以校准置信度，当前评分不能解释为上涨概率。")


def _append_sector_clusters(lines: list[str], value: Any) -> None:
    clusters = value if isinstance(value, dict) else {}
    crowded = clusters.get("crowded") or []
    if not crowded:
        return
    lines += ["", "## 主题集中度"]
    for item in crowded:
        names = "、".join(row.get("name", "—") for row in item.get("sectors") or [])
        lines.append(
            f"- **{item.get('name')}**占前列 {item.get('seats')} 席"
            f"（{_percent(item.get('seat_share'))}）：{names}"
        )
    lines.append("- 同一主题簇只算一组风险暴露，不算多条独立确认。")


def compact_daily_sector_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the model-facing view without bulky per-sector diagnostic matrices."""
    sectors = []
    for item in (payload.get("sectors") or [])[:10]:
        sectors.append(
            {
                key: item.get(key)
                for key in (
                    "name", "level", "score", "direction", "ret_1d", "ret_5d",
                    "breadth", "volume_ratio", "net_inflow", "reasons", "warnings",
                )
                if item.get(key) is not None
            }
        )
    notes = payload.get("data_notes") or []
    return {
        "schema": payload.get("schema"),
        "view": "summary",
        "source": payload.get("source"),
        "trade_date": payload.get("trade_date"),
        "data_health": payload.get("data_health"),
        "market": payload.get("market"),
        "sectors": sectors,
        "sector_index": _compact_diagnostic(payload.get("sector_index")),
        "rotation_price": _compact_diagnostic(payload.get("rotation_price")),
        "turnover_migration": _compact_diagnostic(payload.get("turnover_migration")),
        "seesaw": _compact_diagnostic(payload.get("seesaw")),
        "limit_pool": payload.get("limit_pool"),
        "sector_forecast_validation": payload.get("sector_forecast_validation"),
        "sector_clusters": payload.get("sector_clusters"),
        "data_notes": notes[:20],
        "omitted": {
            "sectors": max(0, len(payload.get("sectors") or []) - len(sectors)),
            "data_notes": max(0, len(notes) - 20),
            "sections": ["rotation", "news_coverage"],
            "hint": "detail='full' 可读取完整审计数据",
        },
        "rendered": render_daily_sector_analysis(
            {**payload, "sectors": sectors, "data_notes": notes[:20]}
        ),
    }


def _compact_diagnostic(value: Any) -> Any:
    """Keep conclusions and leading examples, dropping embedded all-sector series."""
    if not isinstance(value, dict):
        return value
    compact = {}
    for key, item in value.items():
        if key == "sectors":
            continue
        if isinstance(item, list):
            compact[key] = item[:3] if key in {"observations", "notes"} else item[:5]
        else:
            compact[key] = item
    return compact


def _append_values(lines: list[str], title: str, values: list[Any]) -> None:
    if values:
        lines.append(f"- {title}：{'；'.join(str(value) for value in values[:3])}")


def _names(items: Any) -> str:
    if not items:
        return "—"
    names = []
    for item in items[:5]:
        names.append(str(item.get("name") or item.get("sector") or "未知") if isinstance(item, dict) else str(item))
    return "、".join(names)


def _score(value: Any) -> str:
    return "—" if value is None else f"{value:.1f}" if isinstance(value, float) else str(value)


def _percent(value: Any) -> str:
    return "—" if not isinstance(value, (int, float)) else f"{value:+.2%}"


def _data_health(trade_date: Any, today: date) -> dict[str, Any]:
    """这一份板块排序是哪天的。判定复用 `runhealth`，按**交易日**数。

    这里原来自己数自然日，`lag_days > 0` 就判陈旧——两处都是错的，而且错在同一
    个方向（把正常状态报成陈旧）：

    - **盘前正常状态被判陈旧**：`morning` 跑完落的是上一交易日的收盘事实，今天
      开盘前读它，自然日差 1，于是每天早上都挂一行「这是历史复看，不适合作为
      现在的板块判断」——而它恰恰就是现在该看的那一份。
    - **周一早上差 3 天**：上周五那份数据只落后 1 个交易日，按自然日报「滞后 3
      天」。这正是 `DECISIONS.md` §二十四 已经定过的口径，只是这一路没接上。

    顺带删掉了本模块自己那份日期解析——`runhealth._as_date` 认同样的三种写法。
    """
    ledger = HealthLedger(today=today)
    block = ledger.stamped("板块事实包", trade_date)
    return {
        "today": str(today),
        "trade_date": block.trade_date,
        "status": block.status,
        "sessions_behind": block.sessions_behind,
        "is_stale": block.status == STATUS_STALE,
        "caveat": block.detail,
    }
