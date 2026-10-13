"""归档盘前计划及入选、对照、否决样本，供事后核对筛选效果。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.jobs.morning_facts import build_facts, market_state
from ashare_decision_support.jobs.morning_stages import (
    Collected,
    Fusion,
    Ledger,
    MorningConfig,
    Selection,
    Sources,
)
from ashare_decision_support.journal import Journal
from ashare_decision_support.journal.records import (
    DEFAULT_STRATEGY_VERSION,
    KIND_CONTROL,
    KIND_RECOMMENDATION,
    KIND_VETOED,
    Recommendation,
    facts_hash,
)
from ashare_decision_support.news import macro_context_of
from ashare_decision_support.signals.series import bar_date
from ashare_decision_support.state.workflow import archive_plan


def stage_record(
    sources: Sources,
    collected: Collected,
    fusion: Fusion,
    selection: Selection,
    today: date,
    notes: list[str],
    report: Callable[[str], Any],
    *,
    config: MorningConfig | None = None,
    output_path: Path | None = None,
    actionable: bool = True,
    execution_blocks: list[str] | None = None,
    metric_date: str | None = None,
) -> Ledger:
    """⑤ 落台账。

    这里从前还要把候选写进 `candidates.json`，供 `advise` / `monitor` / `ask`
    第二天当默认名单读。候选池 2026-08-11 删掉了（见 `state/positions.py`）：
    候选是**这份晨报的结论**，它该被你读到，而不是变成一份没人维护、却在后面
    每条命令里默默扩大取数范围的名单。候选仍然进 facts、进简报、进台账——
    少的只是那次落盘。
    """
    candidates = selection.candidates

    facts = build_facts(
        today,
        fusion.selected,
        candidates,
        fusion.market,
        fusion.effective_factor if actionable else 0.0,
        notes,
        collected.news_as_of,
        fusion.limit_snapshot,
        fusion.sector_index,
        fusion.daily_read,
        fusion.macro_links,
        fusion.rotation,
        fusion.rotation_price,
        fusion.earnings,
        turnover_migration=fusion.turnover_migration,
        seesaw=fusion.seesaw,
        market_source_counts=collected.market_source_counts,
        news_coverage=collected.coverage,
        news_targets=collected.news_targets,
        news_diagnostics=collected.coverage_diagnostics,
        theme_radar=fusion.theme_radar,
        theme_validation=fusion.theme_validation,
        sector_forecast_validation=fusion.sector_forecast_validation,
        sector_news_memory=fusion.sector_news_memory,
        overseas=fusion.overseas,
        metric_date=metric_date,
    )
    if selection.diagnostic:
        facts["diagnostic"] = dict(selection.diagnostic)
    facts["execution_ready"] = actionable
    facts["execution_blocks"] = execution_blocks or []
    cfg = config or MorningConfig()
    facts["strategy_version"] = cfg.strategy_version
    facts["strategy_parameters"] = asdict(cfg)
    if output_path is not None:
        try:
            facts.update(archive_plan(output_path, facts))
        except (OSError, TypeError, ValueError) as exc:
            notes.append(f"盘前计划归档失败（{type(exc).__name__}），本轮无法按计划版本回放")
    state = market_state(
        fusion.market,
        fusion.effective_factor if actionable else 0.0,
        fusion.limit_snapshot,
        fusion.regime,
        rotation_price=fusion.rotation_price,
        turnover=fusion.turnover_migration,
        broad_index=fusion.broad_index,
        index_verdict=fusion.index_verdict,
        sizing=fusion.sizing,
    )
    journal = sources.journal or Journal()
    macro_context = macro_context_of(fusion.macro_links)
    recorded = _record_journal(
        journal,
        candidates if actionable else [],
        today,
        facts,
        state,
        macro_context=macro_context,
        reference_date=metric_date,
    )
    # 对照组和入选组走同一套记录与兑现流程，只是 kind 不同、不推送、不进候选。
    recorded_control = _record_journal(
        journal,
        selection.control if actionable else [],
        today,
        facts,
        state,
        kind=KIND_CONTROL,
        macro_context=macro_context,
        reference_date=metric_date,
    )
    # 否决组：被业绩闸门移除的那些。它们在打分之前就消失了，所以必须单独从
    # `metric_bars` 里取参考价补记——不补的话，这道**明知有代价还是加了**的闸门
    # 永远无法被自己的台账验证，代价只能停留在设计者的自述里。
    recorded_vetoed = _record_vetoed(
        journal,
        selection.earnings_verdicts if actionable else {},
        collected.metric_bars,
        selection.selected_sector_of,
        today,
        facts,
        state,
        macro_context=macro_context,
    )
    if recorded:
        notes.append(
            f"{recorded} 条候选已落台账，将在 T+1..T+5 自动核对兑现情况"
            + (
                f"；另记 {recorded_control} 条对照组（紧邻落选，只记不推），"
                "用于事后回答「这套筛选本身有没有用」"
                if recorded_control
                else ""
            )
            + (
                f"；另记 {recorded_vetoed} 条业绩否决组，用于事后回答「这道闸门拦对了没有」"
                if recorded_vetoed
                else ""
            )
        )
    report(f"④ 当日候选共 {len(candidates)} 只，正在整理晨报与多空辩论")
    return Ledger(facts=facts, state=state, recorded=recorded)


def _pct_against(value: Any, reference: float) -> float | None:
    """相对参考价的涨跌幅。

    提到模块层而不是留在循环里定义闭包：闭包捕获的是**循环变量本身**，
    今天它恰好每轮都立刻调用所以没出事，但只要有人把调用挪到循环外
    （比如攒起来批量算），所有记录就会一起用上最后一只票的参考价——
    而且不会报错，只会让台账里的百分比整体偏掉。
    """
    return None if not value else round(float(value) / reference - 1.0, 6)


def _reward_risk(stop_pct: float | None, target_pct: float | None) -> float | None:
    """原始盈亏比：目标距离 ÷ 止损距离，不带打分层那个按波幅定的分母下限。

    台账要的是计划发出时**报出去的**那两个价位之比，用来事后分桶（`journal/stats.py`
    的 `by_rrr`）；打分层的下限是为了不让 0.04% 的止损算出 172 的比值拿满分，
    那是打分的事，不该改写台账里的事实。
    """
    if stop_pct is None or target_pct is None or stop_pct >= 0 or target_pct <= 0:
        return None
    return round(target_pct / -stop_pct, 4)


def _last_bar_date(bars: list[dict]) -> str | None:
    """这批日线最后一根的交易日；被否决标的的参考价就是它的收盘。"""
    for bar in reversed(bars):
        if isinstance(bar.get("close"), (int, float)) and bar["close"]:
            stamp = bar_date(bar)
            return stamp.isoformat() if stamp is not None else None
    return None


def _record_vetoed(
    journal: Journal,
    verdicts: dict[str, Any],
    bars_by_symbol: dict[str, list[dict]],
    sector_of: dict[str, str],
    today: date,
    facts: dict,
    market_state: dict[str, Any] | None = None,
    macro_context: dict[str, Any] | None = None,
) -> int:
    """把被业绩闸门否决的标的落台账，供事后核对「拦对了没有」。

    **参考价从板块动量那批日线里取**，而不是重新拉一次：被否决的标的在
    `_apply_screens` 就出局了，从没走到 `fetch_daily_bars`。而 `SectorMetricsAgent`
    早已为**全部成分股**取过 60 日线，其中就包含它们——这一步因此是零额外请求。

    没有分数、没有止损目标、没有打法：这些都产生于打分阶段，而否决发生在那之前。
    存 `None` 而不是编一个，兑现流程只读 `reference_price`（见
    `journal/verify.py`），缺其余字段不影响核对。

    **只记被选中板块里的那些。** 全市场 284 家首亏与这次筛选无关——记进来会让
    否决组的样本远大于入选组，两组不再同源，差值就不再是这道闸门的代价。
    """
    if not verdicts:
        return 0

    digest = facts_hash(facts)
    records: list[Recommendation] = []
    for symbol, verdict in verdicts.items():
        if not getattr(verdict, "is_blocked", False):
            continue
        if symbol not in sector_of:
            continue
        bars = bars_by_symbol.get(symbol) or []
        closes = [bar.get("close") for bar in bars if isinstance(bar.get("close"), (int, float))]
        if not closes or not closes[-1]:
            continue
        forecast = verdict.to_dict().get("forecast") or {}
        records.append(
            Recommendation(
                symbol=symbol,
                trade_date=str(today),
                source="morning",
                sector=sector_of.get(symbol),
                reference_price=float(closes[-1]),
                reference_date=_last_bar_date(bars),
                earnings=forecast,
                reasons=[],
                warnings=list(verdict.blocking),
                market_state=dict(market_state or {}),
                macro_context=dict(macro_context or {}),
                facts_hash=digest,
                kind=KIND_VETOED,
                strategy_version=facts.get("strategy_version") or DEFAULT_STRATEGY_VERSION,
                plan_id=facts.get("plan_id"),
            )
        )
    return journal.record_many(records)


def record_candidate_rows(
    journal: Journal,
    rows: list[dict],
    today: date,
    facts: dict,
    *,
    source: str,
    market_state: dict[str, Any] | None = None,
    kind: str = KIND_RECOMMENDATION,
    macro_context: dict[str, Any] | None = None,
    reference_date: str | None = None,
) -> int:
    """给其他链路用的入口：把 `SelectedSymbol.to_row()` 那种行落台账。

    盘后执行卡（`jobs/post_close.py`）产出的候选行和晨报是同一个形状，此前却从不进
    台账——那张卡说「明天可以做的」，事后没有任何地方核对它做对了没有。来源标签
    必须区别于 `morning`：`journal/stats.py:_screened` 只拿晨报那条链路算入选/对照
    差值，别的来源进总体与分组统计，不混进那个差值。
    """
    return _record_journal(
        journal,
        rows,
        today,
        facts,
        market_state,
        kind=kind,
        macro_context=macro_context,
        reference_date=reference_date,
        source=source,
    )


def _record_journal(
    journal: Journal,
    candidates: list[dict],
    today: date,
    facts: dict,
    market_state: dict[str, Any] | None = None,
    kind: str = KIND_RECOMMENDATION,
    macro_context: dict[str, Any] | None = None,
    reference_date: str | None = None,
    source: str = "morning",
) -> int:
    """把候选落台账。

    参考价与止损/目标都取自**前复权日线**，所以三者同口径，相对百分比有效。
    核对时也用前复权序列，见 `journal/verify.py`。

    `reference_date` 是参考价那根日线的交易日——盘前跑的话是上一交易日，不是
    `today`。每一行自带的 `bar_date` 优先（停牌股的最后一根可能更早），缺了才用
    整批的 `metric_date`。
    """
    digest = facts_hash(facts)
    records: list[Recommendation] = []
    for item in candidates:
        reference = item.get("last_close")
        if not reference:
            continue

        stop_pct = _pct_against(item.get("stop_reference"), reference)
        target_pct = _pct_against(item.get("target_reference"), reference)
        news = item.get("news") or {}
        records.append(
            Recommendation(
                symbol=item["symbol"],
                trade_date=str(today),
                source=source,
                name=item.get("name"),
                sector=item.get("sector"),
                score=item.get("score"),
                reference_price=reference,
                reference_date=item.get("bar_date") or reference_date,
                stop_price=item.get("stop_reference"),
                target_prices=[item["target_reference"]] if item.get("target_reference") else [],
                # 盘前没有分时，买入区间留空；核对时 entered 只对有区间的记录有意义，
                # 执行口径则按计划日开盘价成交（`journal/verify.py:_replay_execution`）。
                stop_pct=stop_pct,
                target_pcts=[target_pct] if target_pct is not None else [],
                rrr=_reward_risk(stop_pct, target_pct),
                trend_state=item.get("trend_state"),
                wyckoff_bias=item.get("wyckoff_bias"),
                playbook=item.get("playbook"),
                setup=item.get("setup") or {},
                news_polarity=news.get("polarity"),
                news_intensity=news.get("intensity"),
                news_doc_count=news.get("doc_count"),
                score_components=item.get("score_components") or {},
                factor_context=item.get("factor_context") or {},
                dip=item.get("dip") or {},
                # 业绩裁决只在这里落地——它刻意不进分数，所以台账是它唯一的
                # 可验证出口。只存 `forecast` 那段（类型/口径/幅度/新鲜度），
                # 否决名单不在这里：被否的标的压根没走到打分，见下面 §对照组。
                earnings=((item.get("earnings") or {}).get("forecast") or {}),
                # 逐笔旁证同理：不进分数、不进排序，台账是它唯一的验证出口。
                trade_flow=item.get("trade_flow") or {},
                market_state=dict(market_state or {}),
                macro_context=dict(macro_context or {}),
                reasons=item.get("reasons") or [],
                warnings=item.get("warnings") or [],
                facts_hash=digest,
                kind=kind,
                strategy_version=facts.get("strategy_version") or DEFAULT_STRATEGY_VERSION,
                plan_id=facts.get("plan_id"),
            )
        )
    return journal.record_many(list(records))
