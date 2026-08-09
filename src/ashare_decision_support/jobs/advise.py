"""On-demand advice for one or more symbols: levels now, scenarios next.

Covers the "问一下这几只该怎么办" workflow. Unlike `morning`, this job takes
the symbols as given — you asked about them, so a risk flag is reported as a
warning rather than silently removing the name from the answer.

四条取数（日线结构、宽基对照、分时结构、个股舆情）互相独立且全是网络等待，所以走
`agents/base.py:run_agents` 并行；串行跑等于把三段延迟加起来，而它们本可以重叠。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.base import float_shares, metadata_names, security_metadata
from ashare_decision_support.adapters.earnings import default_earnings_adapter
from ashare_decision_support.adapters.tickflow_intraday import build_intraday_adapter
from ashare_decision_support.advice.adjudicate import LevelAdjudicator
from ashare_decision_support.advice.engine import AdviceEngine, StockAdvice
from ashare_decision_support.agents import (
    AgentContext,
    BenchmarkDailyAgent,
    OrderBookAgent,
    SymbolDailyAgent,
    SymbolIntradayAgent,
    SymbolNewsAgent,
    run_agents,
)
from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.jobs.progress import progress_reporter
from ashare_decision_support.news import AkshareNewsSource
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier
from ashare_decision_support.notify.base import Notifier, publish
from ashare_decision_support.notify.render import advice_mandatory_block, render_advice_brief
from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput, ScoreProvider
from ashare_decision_support.screens import EarningsScreen
from ashare_decision_support.signals.dipbuy import measure_dip
from ashare_decision_support.signals.distribution import judge_distribution
from ashare_decision_support.signals.intraday import IntradayAnalyzer
from ashare_decision_support.signals.relative_strength import (
    benchmark_for,
    measure_relative_strength,
)
from ashare_decision_support.signals.volume_price import measure_volume_price
from ashare_decision_support.strategy.playbook import evaluate_playbooks
from ashare_decision_support.symbols import normalize_symbol
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
    adjudicator: LevelAdjudicator | None = None,
    use_order_book: bool = True,
    snapshot_adapter: Any | None = None,
    earnings_adapter: Any | None = None,
    security_master_adapter: Any | None = None,
    use_earnings: bool = True,
    progress: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    today = trade_date or date.today()
    notes: list[str] = []
    provider = score_provider or RuleBasedScoreProvider()

    _progress = progress_reporter(progress)

    # ---- 并行采集：日线 || 分时 || 个股舆情 --------------------------------
    source = intraday_adapter or _default_intraday(adapter)
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
    _progress(f"① 正在计算 {len(symbols)} 只标的的日线、分时、盘口和舆情")
    # 证券简称在建 collector 之前先取：`SymbolNewsAgent` 要用它把「只在正文提到
    # 这只票」的行业综述挡在个股舆情之外（见 `news/relevance.py`）。取不到就传空，
    # 那一层自动放行，行为与从前一致。
    master = security_metadata(adapter, symbols, security_master_adapter)
    names = metadata_names(master, symbols)

    collectors = [
        SymbolDailyAgent(),
        BenchmarkDailyAgent(symbols),
        SymbolIntradayAgent(),
        SymbolNewsAgent(symbols=list(symbols), names=names or None),
    ]
    if use_order_book:
        # 五档是**整批一次**请求，和逐标的的分钟线延迟量级差一个数量级；
        # 放进同一波并行里，它基本不增加这一轮的墙钟时间。
        collectors.append(OrderBookAgent(adapter=snapshot_adapter))
    findings = run_agents(collectors, ctx, max_workers=len(collectors))
    for finding in findings.values():
        notes.extend(finding.notes)

    daily = findings["symbol_daily"]
    if not daily.ok:
        raise RuntimeError(f"日线取数失败，无法产出买卖点：{daily.error}")

    snapshots = daily.get("snapshots", {}) or {}
    wyckoff = daily.get("wyckoff", {}) or {}
    usable_daily = daily.get("bars", {}) or {}
    benchmark_daily = findings["benchmark_daily"].get("bars", {}) or {}
    prev_closes = daily.get("prev_closes", {}) or {}
    sentiment = findings["symbol_news"].get("sentiment", {}) or {}
    news_events = findings["symbol_news"].get("events", {}) or {}
    books = findings["order_book"].get("readings", {}) if "order_book" in findings else {}

    # 分时的**取数**和日线并行，但 change_pct / 量比要用日线的昨收，所以拿回原始
    # bars 在这里重算一次。重算是纯计算，比让两次 HTTP 串行便宜得多。
    minute_bars = findings["symbol_intraday"].get("bars", {}) or {}
    intraday = IntradayAnalyzer(today=today).analyze_many(minute_bars, prev_closes)

    # 低吸画像算一次，打分与打法判据共用。
    dips = {
        symbol: measure_dip(rows, snapshots[symbol])
        for symbol, rows in usable_daily.items()
        if symbol in snapshots
    }
    shares = float_shares(adapter, symbols)
    volume_price = {
        symbol: measure_volume_price(rows, snapshots[symbol], shares.get(symbol))
        for symbol, rows in usable_daily.items()
        if symbol in snapshots
    }
    relative_strength = {
        symbol: measure_relative_strength(
            symbol,
            rows,
            benchmark_daily.get(resolved[0], []) if (resolved := benchmark_for(symbol)) else [],
        )
        for symbol, rows in usable_daily.items()
    }

    # 打法判据。**不施加情绪周期约束**（`allowed=None`）：你点名问一只票时该看到
    # 完整判断，不该被今天的大盘状态静默截断。周期的影响另有出口——盘前主链路
    # 会拦，这里只负责把「这只票自己能用哪些打法、判据过没过」说清楚。
    matches = {
        symbol: evaluate_playbooks(
            snapshots[symbol], dips.get(symbol), wyckoff.get(symbol), None
        )
        for symbol in snapshots
    }

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
                    dip=dips.get(symbol),
                    volume_price=volume_price.get(symbol),
                )
                for symbol, rows in usable_daily.items()
                if symbol in snapshots
            ]
        )
    }

    # ---- assemble ---------------------------------------------------------
    # 业绩层在这条链路上**只提示、不移除**。`morning` 是系统主动选票，所以否决；
    # `advise` 是你点名问一只票，答案必须是「它怎么样」而不是把它藏起来——「这只
    # 票我不给你看」正是最没用的回答。和上面 ST 的处理完全同一条理由，见下方
    # `Asked about explicitly, so it is answered — loudly.`
    earnings_screen = EarningsScreen(_advise_earnings(earnings_adapter, today, use_earnings, notes))
    earnings_verdicts = earnings_screen.evaluate(symbols)
    engine = AdviceEngine(adjudicator=adjudicator)
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
            score_components=result.components if result else None,
            book=books.get(symbol),
            instrument=master.get(symbol),
            match=matches.get(symbol),
        )
        if result:
            advice.warnings.extend(result.warnings)
        _apply_earnings(advice, earnings_verdicts.get(normalize_symbol(symbol)))
        vp = volume_price.get(symbol)
        intraday_read = intraday.get(symbol)
        snapshot = snapshots.get(symbol)
        wyckoff_read = wyckoff.get(symbol)
        if vp is not None:
            advice.volume_price = vp.to_dict()
            advice.distribution = judge_distribution(
                features=vp,
                range_position_60d=(snapshot.range_position_60d if snapshot else None),
                price_vs_vwap_pct=(intraday_read.price_vs_vwap_pct if intraday_read else None),
                wyckoff_phase=(wyckoff_read.bias.value if wyckoff_read else None),
            ).to_dict()
        relative = relative_strength.get(symbol)
        if relative is not None:
            advice.relative_strength = relative.to_dict()
        event = news_events.get(symbol)
        if event is not None:
            advice.event_evidence = event.to_dict()
        advices.append(advice)

    facts = _facts(today, advices, notes, period, sentiment, master)
    _progress("② 结构、买卖点和情景已算完，正在整理简报与辩论")

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
        delivered = publish(channels, title, markdown)
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
        delivered = publish(channels, title, markdown)
    _progress("③ 个股分析已完成")

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


def _default_intraday(data_adapter: Any = None) -> Any | None:
    # 付费档走 TickFlow 批量分钟线，免费档退回新浪。选路只有一处，见
    # `adapters/tickflow_intraday.build_intraday_adapter`。
    return build_intraday_adapter(data_adapter)


def _default_news() -> Any | None:
    try:
        return AkshareNewsSource()
    except Exception:
        return None


def _advise_earnings(
    adapter: Any | None, today: date, enabled: bool, notes: list[str]
) -> Any:
    """业绩快照。取不到就返回 None——`EarningsScreen(None)` 全部放行且自报未过滤。"""
    if not enabled:
        notes.append("已关闭业绩读数（--no-earnings），本次不提示业绩风险")
        return None
    source = adapter if adapter is not None else default_earnings_adapter()
    if source is None:
        return None
    try:
        snapshot = source.fetch(today)
    except Exception as exc:
        notes.append(f"业绩预告不可用（{type(exc).__name__}），本次不提示业绩风险")
        return None
    notes.extend(snapshot.notes)
    return snapshot


def _apply_earnings(advice: Any, verdict: Any) -> None:
    """把业绩裁决贴到一条建议上。**否决在这里降级成置顶警告，不移除标的。**

    置顶（`insert(0, ...)`）而不是追加：一条「中报首亏」如果排在第七条风险后面，
    等于没说。和 ST 那条用同一个位置，理由也一样——你点名问的票，最该先看到的
    就是这类硬事实。
    """
    if verdict is None:
        return
    for text in getattr(verdict, "blocking", []) or []:
        advice.warnings.insert(0, f"⚠️ 业绩否决：{text}（盘前链路不会选它）")
    for text in getattr(verdict, "warnings", []) or []:
        advice.warnings.append(f"业绩：{text}")
    for text in getattr(verdict, "catalysts", []) or []:
        advice.reasons.append(text)


def _facts(
    today: date,
    advices: list[StockAdvice],
    notes: list[str],
    period: str,
    sentiment: dict[str, Any] | None = None,
    master: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    readings = sentiment or {}
    instruments = master or {}
    return {
        "trade_date": str(today),
        "minute_period": period,
        "symbols": [
            {
                "symbol": item.symbol,
                "name": item.name,
                "industries": list((instruments.get(item.symbol) or {}).get("industries") or []),
                "industry": (instruments.get(item.symbol) or {}).get("industry"),
                "industry_leaf": (instruments.get(item.symbol) or {}).get("industry_leaf"),
                "industry_path": (instruments.get(item.symbol) or {}).get("industry_path"),
                "concepts": list((instruments.get(item.symbol) or {}).get("concepts") or []),
                "score": item.score,
                "score_components": item.score_components,
                "last_price": item.last_price,
                "session_date": item.session_date,
                "is_current_session": item.is_current_session,
                "trend_state": item.trend_state,
                "wyckoff_bias": item.wyckoff_bias,
                # 打法与判据链。进 facts 是为了让写稿层能引用「凭哪条打法」，
                # 也让数字闸门认得判据里的那些读数。
                "playbook": item.playbook,
                "setup": item.setup or None,
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
                "volume_price": (
                    item.volume_price
                ),
                "distribution": item.distribution,
                "relative_strength": item.relative_strength,
                "event_evidence": item.event_evidence,
                "intraday": item.intraday,
                "trade_context": (
                    item.trade_context.to_dict() if item.trade_context is not None else None
                ),
                "shortline": (
                    item.shortline.to_dict() if item.shortline is not None else None
                ),
                # 盘口、候选集、裁决三样都进 facts。不只是为了给简报多点素材——
                # 数字闸门比对的就是这份 facts，候选价不在里面的话，模型引用一个
                # 合法候选反而会被当成编造。
                "order_book": item.book or None,
                "candidates": item.candidates.to_dict() if item.candidates else None,
                "adjudication": (
                    item.adjudication.to_dict() if item.adjudication else None
                ),
                "news": (
                    readings[item.symbol].to_dict() if item.symbol in readings else None
                ),
            }
            for item in advices
        ],
        "data_notes": notes,
    }
