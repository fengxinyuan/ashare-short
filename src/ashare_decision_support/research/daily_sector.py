"""读取最近一次盘前产出的每日板块分析。

`morning` 负责取数和计算，本模块只读取已落盘的事实，不访问网络、不重复计算。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.artifacts import MORNING_LATEST_NAME
from ashare_decision_support.research.format import names, percent, score
from ashare_decision_support.runhealth import (
    STATUS_FUTURE,
    STATUS_PREVIOUS,
    STATUS_STALE,
    STATUS_UNKNOWN,
    HealthLedger,
)
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.sectors.panel import compact_reading
from ashare_decision_support.signals.series import parse_bar_date
from ashare_decision_support.state.store import read_json

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
        raise FileNotFoundError(f"未找到 {path}；先运行 `ashare-ds morning` 生成每日板块分析")
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 不是有效的板块事实包")

    trade_date = payload.get("trade_date")
    data_date = information_date(payload)
    today = beijing_today()
    return {
        "schema": SCHEMA,
        "source": str(path),
        # `trade_date` 是这份计划的日期（`morning` 运行日），`data_date` 是它用的行情
        # 截止到哪个交易日。盘前正常相差一个交易日；两者写成一个就分不清了。
        "trade_date": trade_date,
        "data_date": data_date,
        "data_health": _data_health(trade_date, data_date, today),
        "execution_ready": payload.get("execution_ready"),
        "execution_blocks": payload.get("execution_blocks") or [],
        "available": [key for key in _FACT_KEYS if payload.get(key) is not None],
        **{key: payload.get(key) for key in _FACT_KEYS},
    }


def information_date(payload: dict[str, Any]) -> str | None:
    """这份盘前事实包的行情截止日，`YYYY-MM-DD`。

    `morning` 直接写 `data_date`；更早的产物没有这个键，退回板块指数序列的日期
    （板块动量与它取的是同一批「最后一个完整交易日」的日线），再退回计划日。
    三处按序取第一个能解析的；都解析不出来就 None，时效判定会把它记成「时点未知」。
    """
    if "data_date" in payload:
        day = parse_bar_date(payload["data_date"])
        return day.isoformat() if day is not None else None
    rotation = payload.get("rotation_price")
    for value in (
        rotation.get("trade_date") if isinstance(rotation, dict) else None,
        payload.get("trade_date"),
    ):
        day = parse_bar_date(value)
        if day is not None:
            return day.isoformat()
    return None


def _data_boundary_lines(payload: dict[str, Any], health: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if payload.get("execution_ready") is False:
        reasons = "；".join(payload.get("execution_blocks") or ["缺少可执行数据确认"])
        lines.append(f"⚠️ 当前板块结论仅供观察，不可用于买入：{reasons}。")
    if health.get("is_stale"):
        lines.append(
            f"⚠️ 数据已落后 {health.get('sessions_behind')} 个交易日"
            f"（今天 {health.get('today')}）。"
            "这是历史复看，不适合作为“现在”的板块判断；先运行 `ashare-ds morning` 刷新。"
            + (f"（{health['caveat']}）" if health.get("caveat") else "")
        )
    elif health.get("status") == STATUS_PREVIOUS:
        lines.append(
            "⚠️ 排名依据上一交易日收盘行情，仅是盘前观察清单；"
            "盘中先运行 `ashare-ds monitor --once` 核对板块，再用"
            " `ashare-ds advise <代码>` 核对个股当日价位，不能直接按此排名买入。"
        )
    elif health.get("is_degraded"):
        lines.append(f"⚠️ {health.get('status')}：{health.get('caveat')}。请先核对数据日期。")
    return lines


def render_daily_sector_analysis(payload: dict[str, Any]) -> str:
    """把板块事实渲染成短报告，不添加新判断。"""
    trade_date = payload.get("trade_date") or "未知日期"
    health = payload.get("data_health") or {}
    # 两个日期分开印：报告日期是 `morning` 跑的那天，行情截止是它用的最后一根日线。
    # 周日跑的报告，前者是周日、后者是上周五——写成一个日期就是把周五的行情标成周日的。
    data_line = f"行情截止：{payload.get('data_date') or '未知日期'}"
    if health.get("status") and not health.get("is_degraded"):
        data_line += f"（{health['status']}）"
    lines = [
        f"# 每日板块分析 {trade_date}",
        "",
        f"报告日期：{trade_date}",
        data_line,
        f"数据来源：`{payload.get('source', '未知')}`",
    ]
    lines.extend(_data_boundary_lines(payload, health))

    market = payload.get("market") or {}
    lines += ["", "## 大盘背景"]
    lines.append(
        f"- 方向：{market.get('direction') or '—'}；综合读数：{score(market.get('score'))}；"
        f"等权宽度：{percent(market.get('breadth'))}；5日中位收益：{percent(market.get('median_ret_5d'))}"
    )
    _append_values(lines, "大盘提示", market.get("warnings") or [])

    sectors = payload.get("sectors") or []
    sector_title = "盘前观察板块" if health.get("status") == STATUS_PREVIOUS else "主攻板块"
    lines += ["", f"## {sector_title}"]
    if not sectors:
        lines.append("- 没有可用的主攻板块数据。")
    for item in sectors:
        lines.append(
            f"- **{item.get('name', '未知')}**（{item.get('level', '—')}）："
            f"{item.get('direction') or '—'}，评分 {score(item.get('score'))}，"
            f"1日 {percent(item.get('ret_1d'))}，5日 {percent(item.get('ret_5d'))}，"
            f"广度 {percent(item.get('breadth'))}，覆盖 {item.get('covered', '—')}/{item.get('constituents', '—')}"
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
            "- 排名上升：" + names(rotation.get("rising")),
            "- 排名下降：" + names(rotation.get("falling")),
        ]
    if turnover:
        lines += [
            f"- 成交额总量变化：{percent(turnover.get('total_change'))}；近端趋势：{percent(turnover.get('total_trend'))}",
            "- 占比上升：" + names(turnover.get("rising")),
            "- 放量下跌：" + names(turnover.get("distribution")),
        ]

    index = payload.get("sector_index")
    if index:
        lines += [
            "",
            "## 行业指数位置",
            "- 上涨靠前：" + names(index.get("gainers")),
            "- 下跌靠前：" + names(index.get("losers")),
        ]
        if index.get("faded"):
            lines.append("- 上涨但收在区间低位：" + names(index["faded"]))

    limit_pool = payload.get("limit_pool")
    if limit_pool:
        lines += [
            "",
            "## 情绪旁证",
            (
                f"- 涨停 {limit_pool.get('limit_up', '—')}，"
                f"跌停 {limit_pool.get('limit_down', '—')}，"
                f"炸板率 {percent(limit_pool.get('blowup_rate'))}，"
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
            f"跑赢率 {percent(win_rate)}，"
            f"平均超额 {percent(excess)}"
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
        # 不叫 `names`：模块顶上从 `research.format` 导进来的就是这个名字，在函数里
        # 拿它当局部变量，Python 会把整个函数体里的 `names` 都当成局部——今天没崩
        # 只是因为这个函数恰好没调过那个格式化器，下一个人加一行就是 UnboundLocalError。
        # 簇内成员**不截断**（`format.names` 会只留前 5 个），这里要的是完整名单。
        members = "、".join(row.get("name", "—") for row in item.get("sectors") or [])
        lines.append(
            f"- **{item.get('name')}**占前列 {item.get('seats')} 席"
            f"（{percent(item.get('seat_share'))}）：{members}"
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
                    "name",
                    "level",
                    "score",
                    "direction",
                    "ret_1d",
                    "ret_5d",
                    "breadth",
                    "volume_ratio",
                    "net_inflow",
                    "reasons",
                    "warnings",
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
        "data_date": payload.get("data_date"),
        "data_health": payload.get("data_health"),
        "execution_ready": payload.get("execution_ready"),
        "execution_blocks": payload.get("execution_blocks") or [],
        "market": payload.get("market"),
        "sectors": sectors,
        "sector_index": compact_reading(payload.get("sector_index")),
        "rotation_price": compact_reading(payload.get("rotation_price")),
        "turnover_migration": compact_reading(payload.get("turnover_migration")),
        "seesaw": compact_reading(payload.get("seesaw")),
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


def _append_values(lines: list[str], title: str, values: list[Any]) -> None:
    if values:
        lines.append(f"- {title}：{'；'.join(str(value) for value in values[:3])}")


def _data_health(trade_date: Any, data_date: Any, today: date) -> dict[str, Any]:
    """按交易日判这份板块事实的时效，看的是**行情截止日**，不是计划日。

    盘前跑出来的那份用的是上一交易日的收盘，它在盘前是「上一交易日」这个正常状态，
    而不是「当日」——按计划日判的话，周日跑的报告会把上周五的行情标成周日当天的。
    计划日本身缺失或晚于今天时，这份事实包不可信，按计划日的判定降级。
    """
    ledger = HealthLedger(today=today)
    plan = ledger.stamped("盘前计划", trade_date)
    data = ledger.stamped("板块行情", data_date)
    block = plan if plan.status in (STATUS_UNKNOWN, STATUS_FUTURE) else data
    return {
        "today": str(today),
        "plan_date": plan.trade_date,
        "data_date": data.trade_date,
        "status": block.status,
        "sessions_behind": block.sessions_behind,
        "is_stale": block.status == STATUS_STALE,
        "is_degraded": block.degraded,
        "caveat": block.detail,
    }
