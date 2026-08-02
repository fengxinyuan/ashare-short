"""On-demand advice for one or more symbols: levels now, scenarios next.

Covers the "问一下这几只该怎么办" workflow. Unlike `morning`, this job takes
the symbols as given — you asked about them, so a risk flag is reported as a
warning rather than silently removing the name from the answer.

三条取数（日线结构、分时结构、个股舆情）互相独立且全是网络等待，所以走
`agents/base.py:run_agents` 并行；串行跑等于把三段延迟加起来，而它们本可以重叠。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.advice.engine import AdviceEngine, StockAdvice
from ashare_decision_support.adapters.sina_intraday import SinaIntradayAdapter
from ashare_decision_support.agents import (
    AgentContext,
    SymbolDailyAgent,
    SymbolIntradayAgent,
    SymbolNewsAgent,
    run_agents,
)
from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.news import AkshareNewsSource
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier
from ashare_decision_support.notify.base import Notifier
from ashare_decision_support.notify.render import advice_mandatory_block, render_advice_brief
from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput, ScoreProvider
from ashare_decision_support.screens import StScreen
from ashare_decision_support.signals.intraday import IntradayAnalyzer
from ashare_decision_support.writer import TemplateWriter, WriteRequest
from ashare_decision_support.writer.base import BriefWriter

ADVICE_LATEST_NAME = "advice_latest.json"
DAILY_LOOKBACK_DAYS = 240


def run_advise(
    adapter: Any,
    symbols: list[str],
    output_dir: str | Path | None = None,
    period: str = "5",
    intraday_adapter: Any | None = None,
    score_provider: ScoreProvider | None = None,
    writer: BriefWriter | None = None,
    notifiers: list[Notifier] | None = None,
    trade_date: date | None = None,
    news_source: Any | None = None,
    use_news: bool = True,
    debate: DebatePanel | None = None,
) -> dict[str, Any]:
    today = trade_date or date.today()
    notes: list[str] = []
    provider = score_provider or RuleBasedScoreProvider()

    # ---- 并行采集：日线 || 分时 || 个股舆情 --------------------------------
    source = intraday_adapter or _default_intraday()
    if source is None:
        notes.append("akshare 不可用，本次无分钟线，价位仅为前复权口径、不可直接挂单")

    news = (news_source if news_source is not None else _default_news()) if use_news else None
    if use_news and news is None:
        notes.append("个股舆情源不可用，本次打分不含个股舆情项")

    ctx = AgentContext(
        trade_date=today,
        adapter=adapter,
        intraday_adapter=source,
        news_source=news,
        symbols=list(symbols),
        period=period,
    )
    findings = run_agents(
        [SymbolDailyAgent(), SymbolIntradayAgent(), SymbolNewsAgent(symbols=list(symbols))],
        ctx,
        max_workers=3,
    )
    for finding in findings.values():
        notes.extend(finding.notes)

    daily = findings["symbol_daily"]
    if not daily.ok:
        raise RuntimeError(f"日线取数失败，无法产出买卖点：{daily.error}")

    snapshots = daily.get("snapshots", {}) or {}
    wyckoff = daily.get("wyckoff", {}) or {}
    usable_daily = daily.get("bars", {}) or {}
    prev_closes = daily.get("prev_closes", {}) or {}
    sentiment = findings["symbol_news"].get("sentiment", {}) or {}

    # 分时的**取数**和日线并行，但 change_pct / 量比要用日线的昨收，所以拿回原始
    # bars 在这里重算一次。重算是纯计算，比让两次 HTTP 串行便宜得多。
    minute_bars = findings["symbol_intraday"].get("bars", {}) or {}
    intraday = IntradayAnalyzer(today=today).analyze_many(minute_bars, prev_closes)

    scores = {
        result.symbol: result
        for result in provider.score_many(
            [
                ScoreInput(
                    symbol=symbol,
                    bars=rows,
                    snapshot=snapshots[symbol],
                    wyckoff=wyckoff.get(symbol),
                    news=sentiment.get(symbol),
                )
                for symbol, rows in usable_daily.items()
                if symbol in snapshots
            ]
        )
    }

    # ---- assemble ---------------------------------------------------------
    names = _security_names(adapter, symbols)
    screen = StScreen()
    engine = AdviceEngine()
    advices: list[StockAdvice] = []
    for symbol in symbols:
        result = scores.get(symbol)
        advice = engine.build(
            symbol=symbol,
            daily=snapshots.get(symbol),
            intraday=intraday.get(symbol),
            wyckoff=wyckoff.get(symbol),
            name=names.get(symbol),
            score=result.score if result else None,
        )
        if result:
            advice.warnings.extend(result.warnings)
        verdict = screen.check(symbol, names.get(symbol))
        if not verdict.passed:
            # Asked about explicitly, so it is answered — loudly.
            advice.warnings.insert(0, f"⚠️ {verdict.reason}")
        advices.append(advice)

    facts = _facts(today, advices, notes, period, sentiment)

    # ---- 多空辩论（可关）--------------------------------------------------
    debate_block = ""
    if debate is not None:
        result = debate.run(facts)
        notes.extend(result.notes)
        debate_block = result.markdown()

    baseline = render_advice_brief(today, advices, notes)
    if debate_block:
        baseline = f"{baseline}\n\n## 多空视角\n\n{debate_block}"
    written = (writer or TemplateWriter()).write(
        WriteRequest(
            title=f"个股买卖点 {today}",
            facts=facts,
            fallback_markdown=baseline,
            mandatory_suffix=advice_mandatory_block(),
        )
    )
    notes.extend(written.notes)
    markdown = written.markdown
    if debate_block and debate_block not in markdown:
        markdown = f"{markdown}\n\n## 多空视角\n\n{debate_block}"

    delivered: list[str] = []
    brief_path = None
    title = f"个股买卖点 {today}"
    if output_dir is not None:
        file_notifier = FileNotifier(output_dir, slug="advice")
        channels = notifiers if notifiers is not None else [ConsoleNotifier(), file_notifier]
        delivered = [c.capabilities.channel for c in channels if c.send(title, markdown)]
        file_notifier.write_payload(facts, ADVICE_LATEST_NAME)
        # 调用方可能自带 FileNotifier，这时内部这个从未发送、last_path 为 None。
        # 报实际写出的那条路径，而不是在 "file" 已投递旁边放一个 null。
        brief_path = next(
            (str(c.last_path) for c in channels if getattr(c, "last_path", None)),
            str(file_notifier.last_path) if file_notifier.last_path else None,
        )
    else:
        # No output directory still means "show me" — only an explicit empty
        # notifier list (the chat path) suppresses output.
        channels = notifiers if notifiers is not None else [ConsoleNotifier()]
        delivered = [c.capabilities.channel for c in channels if c.send(title, markdown)]

    return {
        "trade_date": str(today),
        "requested": len(symbols),
        "advised": sum(1 for item in advices if item.has_actionable_levels),
        "writer": written.writer,
        "used_fallback": written.used_fallback,
        "delivered": delivered,
        "data_notes": notes,
        "facts": facts,
        "brief_path": brief_path,
        "content": markdown,
    }


def _default_intraday() -> Any | None:
    try:
        return SinaIntradayAdapter()
    except Exception:
        return None


def _default_news() -> Any | None:
    try:
        return AkshareNewsSource()
    except Exception:
        return None


def _security_names(adapter: Any, symbols: list[str]) -> dict[str, str | None]:
    getter = getattr(adapter, "get_security_master", None)
    if not callable(getter):
        return {}
    try:
        master = getter(symbols)
    except Exception:
        return {}
    return {
        symbol: (row.get("name") if isinstance(row, dict) else None)
        for symbol, row in master.items()
    }


def _facts(
    today: date,
    advices: list[StockAdvice],
    notes: list[str],
    period: str,
    sentiment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    readings = sentiment or {}
    return {
        "trade_date": str(today),
        "minute_period": period,
        "symbols": [
            {
                "symbol": item.symbol,
                "name": item.name,
                "score": item.score,
                "last_price": item.last_price,
                "session_date": item.session_date,
                "is_current_session": item.is_current_session,
                "trend_state": item.trend_state,
                "wyckoff_bias": item.wyckoff_bias,
                "entry_low": item.entry_low,
                "entry_high": item.entry_high,
                "stop": item.stop,
                "targets": item.targets,
                "rrr": item.rrr,
                "scenarios": [
                    {
                        "name": scenario.name,
                        "trigger": scenario.trigger,
                        "implication": scenario.implication,
                        "level": scenario.level,
                    }
                    for scenario in item.scenarios
                ],
                "reasons": item.reasons,
                "warnings": item.warnings,
                "intraday": item.intraday,
                "news": (
                    readings[item.symbol].to_dict() if item.symbol in readings else None
                ),
            }
            for item in advices
        ],
        "data_notes": notes,
    }
