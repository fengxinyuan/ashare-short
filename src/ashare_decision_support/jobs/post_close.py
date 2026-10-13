"""盘后：给传入的标的（省略即持仓）打分，产出次日执行卡和过程复盘。

与盘前共用 `strategy/select.py`。打法门槛只标注、不剔除用户指定标的，
没有可用打法时仍保留分析并说明原因。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.base import (
    apply_security_metadata_rows,
    fetch_daily_bars,
    float_shares,
    metadata_names,
    security_metadata,
)
from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.artifacts import (
    MORNING_LATEST_NAME,
    POST_CLOSE_LATEST_NAME,
    SNAPSHOT_LATEST_NAME,
)
from ashare_decision_support.jobs.daily_review import run_daily_review
from ashare_decision_support.jobs.morning_facts import market_state
from ashare_decision_support.jobs.morning_record import record_candidate_rows
from ashare_decision_support.jobs.sources import fetch_lhb_seats, fetch_limit_pool, fetch_trade_flow
from ashare_decision_support.journal import Journal
from ashare_decision_support.journal.records import DEFAULT_STRATEGY_VERSION
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier, render_post_close_brief
from ashare_decision_support.notify.base import Notifier, publish, written_path
from ashare_decision_support.research.daily_review import render_next_checks
from ashare_decision_support.research.sector_confirmation import build_sector_confirmation
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.scoring import (
    RuleBasedScoreProvider,
    ScoreProvider,
    build_factor_contexts,
)
from ashare_decision_support.signals.series import bar_date
from ashare_decision_support.strategy.positioning import resolve_position_sizing
from ashare_decision_support.strategy.regime import regime_from_limit_pool
from ashare_decision_support.strategy.select import evaluate_universe

# 台账里这条链路的来源标签。和 `jobs/advise.py:ADVISE_SOURCE` 同一条理由：
# `journal/stats.py:_screened` 只拿晨报那条链路算入选/对照差值，盘后卡的标的是
# 人手点名的持仓与自选，从没进过板块排序，混进去会把那个差值稀释成谁都不像的数。
POST_CLOSE_SOURCE = "post_close"


def run_post_close(
    adapter: Any,
    symbols: list[str],
    output_dir: str | Path,
    days: int = 180,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    position_factor: float | None = None,
    score_provider: ScoreProvider | None = None,
    notifiers: list[Notifier] | None = None,
    top_n: int | None = None,
    debate: DebatePanel | None = None,
    trade_date: date | None = None,
    limit_pool_adapter: Any | None = None,
    use_limit_pool: bool = True,
    detail_adapter: Any | None = None,
    security_master_adapter: Any | None = None,
    use_trade_flow: bool = True,
    lhb_seat_adapter: Any | None = None,
    use_lhb_seats: bool = True,
    journal: Journal | None = None,
) -> dict[str, Any]:
    provider = score_provider or RuleBasedScoreProvider()
    end = trade_date or beijing_today()
    start = end - timedelta(days=max(days, 1))

    data_notes: list[str] = []
    bars_by_symbol = fetch_daily_bars(adapter, symbols, start, end, data_notes)
    usable = {symbol: rows for symbol, rows in bars_by_symbol.items() if rows}
    data_notes.extend(_data_notes(adapter, symbols, usable, end))

    # 情绪周期闸门。**盘后也要过**——「明天允许哪一类打法、仓位上限多少」是一个
    # 独立于个股分数的判断，而次日执行卡正是拿来照着做的那张纸。
    limit_snapshot = fetch_limit_pool(
        limit_pool_adapter,
        end,
        enabled=use_limit_pool,
        notes=data_notes,
        off_note="已关闭涨停池读数，本次无情绪周期判定",
    )
    regime = regime_from_limit_pool(limit_snapshot)
    data_notes.extend(regime.reasons)
    data_notes.extend(regime.warnings)

    trade_flow = fetch_trade_flow(
        detail_adapter,
        list(usable),
        enabled=use_trade_flow,
        notes=data_notes,
        off_note="已关闭逐笔资金旁证，本次盘后卡不含主动买卖摘要",
        subject="本次盘后卡",
    )
    factor_contexts = build_factor_contexts(
        [],
        {},
        trade_flow_by_symbol=trade_flow,
    )
    metadata = security_metadata(adapter, list(usable), security_master_adapter)
    names = metadata_names(metadata, list(usable))
    # 换手率与筹码分布都要流通股本；缺了这两项整体弃权，其余读数照算。
    shares = float_shares(adapter, list(usable))
    if not shares:
        data_notes.append("未取到流通股本，本次无换手率与筹码分布估算")
    result = evaluate_universe(
        usable,
        float_shares=shares,
        regime=regime,
        names=names,
        contexts=factor_contexts,
        provider=provider,
        # 见模块 docstring：传进来的票只标注不剔除。
        require_playbook=False,
    )
    data_notes.extend(result.notes)

    actionable = result.selected[:top_n] if top_n is not None else result.selected
    watch_only = result.rejected

    sizing = resolve_position_sizing(
        cycle_label=regime.cycle.value,
        cycle_cap=regime.position_cap,
        manual_factor=position_factor,
    )
    data_notes.extend(sizing.notes)
    effective_factor = sizing.effective_factor

    trade_date_resolved = _latest_bar_date(usable, end)
    lhb_seats = fetch_lhb_seats(
        lhb_seat_adapter, list(usable), trade_date_resolved, enabled=use_lhb_seats, notes=data_notes
    )
    sector_confirmation = _load_sector_confirmation(output_dir, trade_date_resolved, data_notes)
    actionable_rows, watch_only_rows = (
        _decorate_rows(items, metadata, trade_flow, lhb_seats) for items in (actionable, watch_only)
    )
    payload = {
        "trade_date": str(trade_date_resolved),
        "kind": "post_close",
        "strategy_version": strategy_version,
        "regime": regime.to_dict(),
        "position_factor": effective_factor,
        "actionable": actionable_rows,
        "watch_only": watch_only_rows,
        "sector_confirmation": sector_confirmation,
        "data_notes": data_notes,
    }
    # 「明天可以做的」那几行进台账，事后才有地方核对这张卡做对了没有。
    recorded = _record_ledger(
        journal,
        actionable_rows,
        trade_date_resolved,
        payload,
        market_state(None, effective_factor, limit_snapshot, regime, sizing=sizing),
        data_notes,
    )

    daily_review = _daily_review(output_dir, trade_date_resolved, data_notes)
    if daily_review is not None:
        payload["daily_review"] = daily_review
        if daily_review.get("original_plan_available"):
            payload["sector_confirmation"] = daily_review["sector_confirmation"]

    content = render_post_close_brief(payload)
    if daily_review is not None:
        content += f"\n\n{render_next_checks(daily_review)}"

    # 辩论跑在**已经算完的**结论上，输入只有 facts。
    content = _append_debate(content, payload, debate, data_notes)

    title = f"盘后简报 {trade_date_resolved}"
    # 文件名必须跟行情所属交易日走，而不是跟命令执行日走。15:00 前、休市日或
    # 数据源晚一日时二者会不同；按执行日命名会让次日盘前拿错复盘归属。
    file_notifier = FileNotifier(
        output_dir,
        slug="post-close",
        stamp=trade_date_resolved.strftime("%Y%m%d"),
    )
    channels = notifiers if notifiers is not None else [ConsoleNotifier(), file_notifier]
    delivered = publish(channels, title, content)

    # 交给次日盘前那一步。
    payload_path = _save_handoff(file_notifier, payload, result, data_notes)

    return {
        "title": title,
        "trade_date": str(trade_date_resolved),
        "scored": len(actionable) + len(watch_only),
        "actionable": len(actionable),
        "watch_only": len(watch_only),
        "requested": len(symbols),
        "regime": regime.cycle.value,
        "position_factor": effective_factor,
        "journal_records": recorded,
        "delivered": delivered,
        "data_notes": data_notes,
        "payload_path": str(payload_path) if payload_path is not None else None,
        "daily_review_path": (daily_review or {}).get("payload_path"),
        "brief_path": written_path(channels, file_notifier),
        "content": content,
    }


def _append_debate(
    content: str, payload: dict, debate: DebatePanel | None, notes: list[str]
) -> str:
    if debate is None:
        return content
    debated = debate.run(payload)
    notes.extend(debated.notes)
    block = debated.markdown()
    return f"{content}\n\n## 多空视角\n\n{block}" if block else content


def _decorate_rows(
    items: list[Any],
    metadata: dict[str, dict[str, Any]],
    trade_flow: dict[str, dict[str, Any]],
    lhb_seats: dict[str, Any],
) -> list[dict[str, Any]]:
    """选股行 → 补名称与分类 → 挂逐笔旁证 → 挂龙虎榜席位。可做与只能看两批走同一条路。"""
    rows = apply_security_metadata_rows([item.to_row() for item in items], metadata)
    return _attach_lhb_seats(_attach_trade_flow(rows, trade_flow), lhb_seats)


def _record_ledger(
    journal: Journal | None,
    rows: list[dict],
    day: date,
    payload: dict,
    state: dict[str, Any],
    notes: list[str],
) -> int:
    """只记过了打法门槛的 actionable；watch_only 没有可执行计划，记进去只会污染统计。

    参考价是当日收盘（行里的 `bar_date`），核对窗口从下一个交易日开始。没给台账
    就不记：测试与只读场景不该悄悄写状态目录。
    """
    if journal is None or not rows:
        return 0
    recorded = record_candidate_rows(
        journal, rows, day, payload, source=POST_CLOSE_SOURCE, market_state=state
    )
    notes.append(f"{recorded} 条次日执行卡候选已落台账（来源 post_close），将在 T+1..T+5 核对")
    return recorded


def _save_handoff(
    notifier: FileNotifier, payload: dict, result: Any, notes: list[str]
) -> Path | None:
    path = notifier.write_payload(payload, POST_CLOSE_LATEST_NAME)
    if path is None:
        notes.append("盘后执行卡保存失败，次日交接无法读取本轮结果")
    snapshots = {
        symbol: snapshot.model_dump(mode="json")
        for symbol, snapshot in _snapshots_of(result).items()
    }
    if notifier.write_payload(snapshots, SNAPSHOT_LATEST_NAME) is None:
        notes.append("盘后价位快照保存失败，次日交接缺少本轮价位")
    return path


def _daily_review(output_dir: str | Path, day: date, notes: list[str]) -> dict | None:
    try:
        result = run_daily_review(output_dir, trade_date=day, notifiers=[])
    except (OSError, ValueError, TypeError) as exc:
        notes.append(f"每日归因复盘未完成（{type(exc).__name__}: {exc}），本次仍保留盘后执行卡")
        return None
    review = result["payload"]
    if result["payload_path"] is None:
        notes.append("每日复盘未归档，次日复查项仅保存在本次盘后结果中")
    return {
        **{
            key: review.get(key)
            for key in (
                "schema",
                "trade_date",
                "as_of",
                "original_plan_available",
                "original_plan_id",
                "strategy_version",
                "sector_confirmation",
                "counts",
                "next_checks",
                "data_notes",
            )
        },
        "payload_path": result["payload_path"],
    }


def _snapshots_of(result: Any) -> dict[str, Any]:
    return {item.symbol: item.snapshot for item in list(result.selected) + list(result.rejected)}


def _load_sector_confirmation(
    output_dir: str | Path,
    trade_date: date,
    data_notes: list[str],
) -> dict[str, Any] | None:
    """盘后轻量验证盘前板块思路。

    没有盘前产物时直接跳过；有产物但盘中截面不足时，把 `sector_confirmation`
    自己给出的 data_status 渲染出来，不把盘后卡打断。
    """
    if not (Path(output_dir) / MORNING_LATEST_NAME).exists():
        return None
    try:
        confirmation = build_sector_confirmation(output_dir)
    except Exception as exc:
        data_notes.append(f"盘前板块验证失败（{type(exc).__name__}: {exc}）")
        return None
    target = confirmation.get("target_session")
    if target and str(target) != str(trade_date):
        data_notes.append(
            f"盘前板块验证归属 {target}，与本次盘后交易日 {trade_date} 不一致，已跳过"
        )
        return None
    return confirmation


def _attach_trade_flow(
    rows: list[dict[str, Any]],
    trade_flow: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    attached: list[dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        symbol = str(row.get("symbol") or "")
        if symbol and symbol in trade_flow:
            copied["trade_flow"] = dict(trade_flow[symbol])
        attached.append(copied)
    return attached


def _attach_lhb_seats(
    rows: list[dict[str, Any]], readings: dict[str, dict]
) -> list[dict[str, Any]]:
    return [
        {**row, "lhb_seats": dict(readings[row["symbol"]])}
        if row.get("symbol") in readings
        else dict(row)
        for row in rows
    ]


def _latest_bar_date(usable: dict[str, list[dict]], fallback: date | None = None) -> date:
    return max(
        (stamp for rows in usable.values() for row in rows if (stamp := bar_date(row)) is not None),
        default=fallback or beijing_today(),
    )


def _data_notes(
    adapter: Any,
    symbols: list[str],
    usable: dict[str, list[dict]],
    today: date | None = None,
) -> list[str]:
    notes: list[str] = []
    reference = today or beijing_today()
    try:
        health = adapter.health_check()
    except Exception:
        health = {}

    mode = health.get("mode")
    capabilities = health.get("capabilities", {})
    if mode:
        notes.append(f"行情源模式：{mode}")
    if mode == "free" or not capabilities.get("supports_intraday", True):
        notes.append("免费档仅提供日线，盘中监控与做 T 提示需要付费档实时/分钟数据")

    missing = [symbol for symbol in symbols if symbol not in usable]
    if missing:
        notes.append(f"无可用日线的标的：{'、'.join(missing)}")

    trade_date = _latest_bar_date(usable, reference) if usable else None
    if trade_date and trade_date != reference:
        notes.append(f"最新日线为 {trade_date}，非当日数据（休市或数据尚未更新）")

    notes.append("日线默认前复权，可用于结构与均线；涨跌停、封单与价格档位判断需改用不复权数据")
    return notes
