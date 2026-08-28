"""盘后：给传入的标的（省略即持仓）打分，产出次日执行卡。

和 `morning.py` 相反：那个从全市场出发自上而下选票，这个给**你已经在盯的那批**
打分。两者共用同一段选股逻辑（`strategy/select.py`），所以两张表的分数可比。

**改造前这条链路走的是另一套规则。** 它把加权分数直接喂进 `research.py` 的矩阵
管线，那套管线自带一份阶跃函数的仓位算法和一套四档市场状态字符串，与盘前链路的
`EmotionCycle` 七档互不映射——于是盘后给出的次日候选，用的正是盘前明确否定过的
逻辑，而两边没有任何地方对账。矩阵管线已随本次改造删除。

**传进来的票不做隐式过滤。** 打法门槛在这里只做**标注**不做剔除（`require_playbook=
False`）：这批票是你自己放进来的，「今天没有可用打法」是一个要说出口的结论，不是
让它从表里消失的理由。同一条口径见 `jobs/advise.py` 对 ST 标的的处理。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.base import (
    apply_security_metadata_rows,
    float_shares,
    metadata_names,
    security_metadata,
)
from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.artifacts import POST_CLOSE_LATEST_NAME, SNAPSHOT_LATEST_NAME
from ashare_decision_support.jobs.sources import fetch_lhb_seats, fetch_limit_pool, fetch_trade_flow
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier, render_post_close_brief
from ashare_decision_support.notify.base import Notifier, publish, written_path
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.scoring import (
    RuleBasedScoreProvider,
    ScoreProvider,
    build_factor_contexts,
)
from ashare_decision_support.strategy.positioning import resolve_position_sizing
from ashare_decision_support.strategy.regime import regime_from_limit_pool
from ashare_decision_support.strategy.select import evaluate_universe


def run_post_close(
    adapter: Any,
    symbols: list[str],
    output_dir: str | Path,
    days: int = 180,
    strategy_version: str = "rule-v1",
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
) -> dict[str, Any]:
    provider = score_provider or RuleBasedScoreProvider()
    end = trade_date or beijing_today()
    start = end - timedelta(days=max(days, 1))

    bars_by_symbol = adapter.get_historical_bars(symbols, start, end)
    usable = {symbol: rows for symbol, rows in bars_by_symbol.items() if rows}
    data_notes = _data_notes(adapter, symbols, usable, end)

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
    actionable_rows = _attach_lhb_seats(_attach_trade_flow(
        apply_security_metadata_rows([item.to_row() for item in actionable], metadata),
        trade_flow,
    ), lhb_seats)
    watch_only_rows = _attach_lhb_seats(_attach_trade_flow(
        apply_security_metadata_rows([item.to_row() for item in watch_only], metadata),
        trade_flow,
    ), lhb_seats)
    payload = {
        "trade_date": str(trade_date_resolved),
        "kind": "post_close",
        "strategy_version": strategy_version,
        "regime": regime.to_dict(),
        "position_factor": effective_factor,
        "actionable": actionable_rows,
        "watch_only": watch_only_rows,
        "data_notes": data_notes,
    }

    content = render_post_close_brief(payload)

    # 辩论跑在**已经算完的**结论上，输入只有 facts。
    if debate is not None:
        debated = debate.run(payload)
        data_notes.extend(debated.notes)
        block = debated.markdown()
        if block:
            content = f"{content}\n\n## 多空视角\n\n{block}"

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
    file_notifier.write_payload(payload, POST_CLOSE_LATEST_NAME)
    file_notifier.write_payload(
        {
            symbol: snapshot.model_dump(mode="json")
            for symbol, snapshot in _snapshots_of(result).items()
        },
        SNAPSHOT_LATEST_NAME,
    )

    return {
        "title": title,
        "trade_date": str(trade_date_resolved),
        "scored": len(actionable) + len(watch_only),
        "actionable": len(actionable),
        "watch_only": len(watch_only),
        "requested": len(symbols),
        "regime": regime.cycle.value,
        "position_factor": effective_factor,
        "delivered": delivered,
        "data_notes": data_notes,
        "payload_path": str(Path(output_dir) / POST_CLOSE_LATEST_NAME),
        "brief_path": written_path(channels, file_notifier),
        "content": content,
    }


# ---- helpers -------------------------------------------------------------


def _snapshots_of(result: Any) -> dict[str, Any]:
    return {
        item.symbol: item.snapshot for item in list(result.selected) + list(result.rejected)
    }


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


def _attach_lhb_seats(rows: list[dict[str, Any]], readings: dict[str, dict]) -> list[dict[str, Any]]:
    return [
        {**row, "lhb_seats": dict(readings[row["symbol"]])}
        if row.get("symbol") in readings
        else dict(row)
        for row in rows
    ]
def _latest_bar_date(usable: dict[str, list[dict]], fallback: date | None = None) -> date:
    latest: date | None = None
    for rows in usable.values():
        timestamp = rows[-1].get("timestamp")
        if isinstance(timestamp, datetime):
            candidate = timestamp.date()
        elif isinstance(timestamp, date):
            candidate = timestamp
        else:
            continue
        if latest is None or candidate > latest:
            latest = candidate
    return latest or fallback or beijing_today()


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
