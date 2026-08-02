"""Post-close job: pull daily bars, score the watchlist, publish the brief.

This is the only job that fully works on the TickFlow free tier, which serves
daily bars and nothing else. It runs after the close, when the day's daily bar
is final.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.models.enums import Route
from ashare_decision_support.models.schemas import (
    CandidateBlueprint,
    DailyTechnicalSnapshot,
    DecisionMatrix,
    MarketContext,
    MatrixBuildConfig,
    ResearchInput,
    SectorBlueprint,
)
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier, render_post_close_brief
from ashare_decision_support.notify.base import Notifier
from ashare_decision_support.research import ResearchPipeline, load_research_input
from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput, ScoreProvider
from ashare_decision_support.signals import DailyTechnicalAnalyzer, WyckoffAnalyzer

WATCHLIST_SECTOR = "自选"
MATRIX_LATEST_NAME = "decision_matrix_latest.json"
SNAPSHOT_LATEST_NAME = "snapshots_latest.json"


def run_post_close(
    adapter: Any,
    symbols: list[str],
    output_dir: str | Path,
    days: int = 180,
    strategy_version: str = "rule-v1",
    position_factor: float = 0.5,
    research_input_path: str | Path | None = None,
    score_provider: ScoreProvider | None = None,
    notifiers: list[Notifier] | None = None,
    top_n: int | None = None,
    debate: DebatePanel | None = None,
    trade_date: date | None = None,
) -> dict[str, Any]:
    provider = score_provider or RuleBasedScoreProvider()
    end = trade_date or date.today()
    start = end - timedelta(days=max(days, 1))

    bars_by_symbol = adapter.get_historical_bars(symbols, start, end)
    usable = {symbol: rows for symbol, rows in bars_by_symbol.items() if rows}
    data_notes = _data_notes(adapter, symbols, usable, end)

    snapshots = DailyTechnicalAnalyzer().analyze_many(usable)
    wyckoff_by_symbol = {signal.symbol: signal for signal in WyckoffAnalyzer().analyze_many(usable)}

    results = provider.score_many(
        [
            ScoreInput(
                symbol=symbol,
                bars=rows,
                snapshot=snapshots[symbol],
                wyckoff=wyckoff_by_symbol.get(symbol),
            )
            for symbol, rows in usable.items()
            if symbol in snapshots
        ]
    )
    if top_n is not None:
        results = results[:top_n]

    matrix = _build_matrix(
        results=results,
        snapshots=snapshots,
        usable=usable,
        strategy_version=strategy_version,
        position_factor=position_factor,
        research_input_path=research_input_path,
        fallback_date=end,
    )
    _attach_scores(matrix, results)

    content = render_post_close_brief(matrix, results, snapshots, data_notes)

    # 辩论跑在**已经算完的**结论上，输入只有 facts。文档里写的「盘前与盘后各一次」
    # 到这里才真正成立——在此之前只有盘前跑。
    facts = _facts(matrix, results, snapshots, data_notes)
    if debate is not None:
        debated = debate.run(facts)
        data_notes.extend(debated.notes)
        block = debated.markdown()
        if block:
            content = f"{content}\n\n## 多空视角\n\n{block}"

    title = f"盘后简报 {matrix.trade_date}"

    file_notifier = FileNotifier(output_dir, slug="post-close")
    channels = notifiers if notifiers is not None else [ConsoleNotifier(), file_notifier]
    delivered = [notifier.capabilities.channel for notifier in channels if notifier.send(title, content)]

    # Hand off to the pre-open run.
    file_notifier.write_payload(matrix.model_dump(mode="json"), MATRIX_LATEST_NAME)
    file_notifier.write_payload(
        {symbol: snapshot.model_dump(mode="json") for symbol, snapshot in snapshots.items()},
        SNAPSHOT_LATEST_NAME,
    )

    return {
        "title": title,
        "trade_date": str(matrix.trade_date),
        "scored": len(results),
        "requested": len(symbols),
        "delivered": delivered,
        "data_notes": data_notes,
        "matrix_path": str(Path(output_dir) / MATRIX_LATEST_NAME),
        "brief_path": str(file_notifier.last_path) if file_notifier.last_path else None,
        "content": content,
    }


def _facts(
    matrix: DecisionMatrix,
    results: list,
    snapshots: dict[str, DailyTechnicalSnapshot],
    data_notes: list[str],
) -> dict[str, Any]:
    """辩论层与 LLM 唯一能看到的东西。所有数字都出自确定性层。"""
    by_symbol = {result.symbol: result for result in results}
    return {
        "trade_date": str(matrix.trade_date),
        "strategy_version": matrix.strategy_version,
        "position_band_factor": matrix.position_band_factor,
        "symbols": [
            {
                "symbol": symbol,
                "score": by_symbol[symbol].score if symbol in by_symbol else None,
                "last_close": snapshot.last_close,
                "trend_state": snapshot.trend_state.value,
                "volume_ratio_20d": snapshot.volume_ratio_20d,
                "range_position_60d": snapshot.range_position_60d,
                "stop_reference": snapshot.stop_reference,
                "target_reference": snapshot.target_reference,
                "reasons": by_symbol[symbol].reasons if symbol in by_symbol else [],
                "warnings": by_symbol[symbol].warnings if symbol in by_symbol else [],
            }
            for symbol, snapshot in snapshots.items()
        ],
        "data_notes": data_notes,
    }


def _attach_scores(matrix: DecisionMatrix, results: list) -> None:
    """Carry the score and its warnings into the matrix.

    The pre-open card is what gets read at decision time, so risk flags have to
    survive the handoff rather than living only in the post-close markdown.
    """
    by_symbol = {result.symbol: result for result in results}
    for candidate in matrix.candidates:
        result = by_symbol.get(candidate.symbol)
        if result is None:
            continue
        candidate.research_score = result.score
        candidate.warnings = list(result.warnings)


def _build_matrix(
    results: list,
    snapshots: dict[str, DailyTechnicalSnapshot],
    usable: dict[str, list[dict]],
    strategy_version: str,
    position_factor: float,
    research_input_path: str | Path | None,
    fallback_date: date | None = None,
) -> DecisionMatrix:
    if research_input_path:
        return ResearchPipeline().build_matrix(load_research_input(research_input_path))

    trade_date = _latest_bar_date(usable, fallback_date)
    blueprints = [
        CandidateBlueprint(
            symbol=result.symbol,
            sector=WATCHLIST_SECTOR,
            thesis=("；".join(result.reasons) if result.reasons else "规则打分入选"),
            priority_score=result.score,
        )
        for result in results
    ]

    research_input = ResearchInput(
        trade_date=trade_date,
        strategy_version=strategy_version,
        # No market-breadth feed on the free tier, so the position factor is an
        # explicit operator input rather than something inferred from bad data.
        market_context=MarketContext(
            limit_down_count=0,
            blowup_rate=0.0,
            breadth_score=0.0,
            manual_position_factor=position_factor,
        ),
        sectors=[SectorBlueprint(name=WATCHLIST_SECTOR, route=Route.MAINLINE, policy_score=0.0)],
        candidates=blueprints,
    )
    # A hand-picked watchlist has already passed sector selection by definition,
    # so drop the sector threshold instead of faking a score that clears it.
    config = MatrixBuildConfig(
        mainline_min_sector_score=0.0,
        max_mainline_sectors=1,
        max_candidates_per_sector=max(len(blueprints), 1),
    )
    return ResearchPipeline(config).build_matrix(research_input)


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
    return latest or fallback or date.today()


def _data_notes(
    adapter: Any,
    symbols: list[str],
    usable: dict[str, list[dict]],
    today: date | None = None,
) -> list[str]:
    notes: list[str] = []
    reference = today or date.today()
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
