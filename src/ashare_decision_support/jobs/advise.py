"""On-demand advice for one or more symbols: levels now, scenarios next.

Covers the "问一下这几只该怎么办" workflow. Unlike `morning`, this job takes
the symbols as given — you asked about them, so a risk flag is reported as a
warning rather than silently removing the name from the answer.

四条取数（日线结构、宽基对照、分时结构、个股舆情）互相独立且全是网络等待，所以走
`agents/base.py:run_agents` 并行；串行跑等于把三段延迟加起来，而它们本可以重叠。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.base import float_shares, metadata_names, security_metadata
from ashare_decision_support.adapters.tickflow_intraday import build_intraday_adapter
from ashare_decision_support.advice.adjudicate import LevelAdjudicator
from ashare_decision_support.advice.engine import (
    MIN_RRR,
    AdviceEngine,
    StockAdvice,
    meets_min_rrr,
    refresh_level_risk,
    risk_reward,
)
from ashare_decision_support.agents import (
    AgentContext,
    BenchmarkDailyAgent,
    OrderBookAgent,
    SymbolDailyAgent,
    SymbolIntradayAgent,
    SymbolNewsAgent,
    run_agents,
    timing_note,
)
from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.artifacts import ADVICE_LATEST_NAME
from ashare_decision_support.jobs.progress import progress_reporter
from ashare_decision_support.jobs.sources import fetch_earnings
from ashare_decision_support.journal import Journal
from ashare_decision_support.journal.records import DEFAULT_STRATEGY_VERSION, Recommendation
from ashare_decision_support.monitor.holdings import (
    HoldingView,
    breakeven_lines,
    effective_stop,
    effective_targets,
)
from ashare_decision_support.news import AkshareNewsSource
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier
from ashare_decision_support.notify.base import Notifier, publish, written_path
from ashare_decision_support.notify.render import advice_mandatory_block, render_advice_brief
from ashare_decision_support.schedule.timezone import beijing_naive_now, beijing_today
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
from ashare_decision_support.state.workflow import read_plans
from ashare_decision_support.strategy.playbook import evaluate_playbooks
from ashare_decision_support.symbols import normalize_symbol
from ashare_decision_support.writer import TemplateWriter, WriteRequest
from ashare_decision_support.writer.base import BriefWriter

# 台账里这条链路的来源标签。**必须与 `morning` 的 `"morning"` 区分开**：
# `journal/stats.py` 的 `_selection_edge` / `_veto_cost` 比的是「同日、同筛选
# 流程、只差最后一刀排序」的两组，而 `advise` 的标的是人手点名的、根本没走过
# 那道筛选。混进入选组会把那个差值稀释成一个谁都不像的数。
ADVISE_SOURCE = "advise"


@dataclass(slots=True)
class _Reads:
    """单轮 advise 的行情、舆情与计算结果，供建议生成和发布复用。"""

    snapshots: dict[str, Any]
    wyckoff: dict[str, Any]
    intraday: dict[str, Any]
    books: dict[str, Any]
    names: dict[str, str | None]
    master: dict[str, Any]
    sentiment: dict[str, Any]
    news_events: dict[str, Any]
    volume_price: dict[str, Any]
    relative_strength: dict[str, Any]
    matches: dict[str, Any]
    scores: dict[str, Any]


def _collect(
    *,
    adapter: Any,
    symbols: list[str],
    today: date,
    period: str,
    intraday_adapter: Any | None,
    news_source: Any | None,
    use_news: bool,
    use_order_book: bool,
    snapshot_adapter: Any | None,
    security_master_adapter: Any | None,
    provider: ScoreProvider,
    notes: list[str],
    progress: Callable[[str], Any],
) -> _Reads:
    """① 并行取数 + 纯计算。取不到的那一路写进 notes，不抛。"""
    # 并行采集：日线 || 分时 || 个股舆情
    # 付费档走 TickFlow 批量分钟线，免费档退回新浪。选路只有一处，见
    # `adapters/tickflow_intraday.build_intraday_adapter`。
    source = intraday_adapter or build_intraday_adapter(adapter)
    if source is None:
        notes.append("akshare 不可用，本次无分钟线，价位仅为前复权口径、不可直接挂单")

    news = (news_source if news_source is not None else _default_news()) if use_news else None
    if use_news and news is None:
        notes.append("个股舆情源不可用，本次打分不含个股舆情项")
    elif not use_news:
        notes.append("已关闭个股舆情（--no-news），本次打分不含个股舆情项")

    ctx = AgentContext(
        trade_date=today,
        adapter=adapter,
        intraday_adapter=source,
        news_source=news,
        symbols=list(symbols),
        period=period,
    )
    progress(f"① 正在计算 {len(symbols)} 只标的的日线、分时、盘口和舆情")
    # 证券简称在建 collector 之前先取：`SymbolNewsAgent` 要用它把「只在正文提到
    # 这只票」的行业综述挡在个股舆情之外（见 `news/relevance.py`）。取不到就传空，
    # 那一层自动放行，行为与从前一致。
    master = security_metadata(adapter, symbols, security_master_adapter)
    names = metadata_names(master, symbols)

    collectors = [
        SymbolDailyAgent(),
        BenchmarkDailyAgent(symbols),
        SymbolIntradayAgent(),
    ]
    if use_news:
        collectors.append(SymbolNewsAgent(symbols=list(symbols), names=names or None))
    if use_order_book:
        # 五档是**整批一次**请求，和逐标的的分钟线延迟量级差一个数量级；
        # 放进同一波并行里，它基本不增加这一轮的墙钟时间。
        collectors.append(OrderBookAgent(adapter=snapshot_adapter))
    findings = run_agents(collectors, ctx, max_workers=len(collectors))
    for finding in findings.values():
        notes.extend(finding.notes)
    _timing = timing_note(findings)
    if _timing:
        notes.append(_timing)

    daily = findings["symbol_daily"]
    if not daily.ok:
        raise RuntimeError(f"日线取数失败，无法产出买卖点：{daily.error}")

    snapshots = daily.get("snapshots", {}) or {}
    wyckoff = daily.get("wyckoff", {}) or {}
    usable_daily = daily.get("bars", {}) or {}
    benchmark_daily = findings["benchmark_daily"].get("bars", {}) or {}
    prev_closes = daily.get("prev_closes", {}) or {}
    symbol_news = findings.get("symbol_news")
    sentiment = (symbol_news.get("sentiment", {}) or {}) if symbol_news is not None else {}
    news_events = (symbol_news.get("events", {}) or {}) if symbol_news is not None else {}
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
        symbol: evaluate_playbooks(snapshots[symbol], dips.get(symbol), wyckoff.get(symbol), None)
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

    return _Reads(
        snapshots=snapshots,
        wyckoff=wyckoff,
        intraday=intraday,
        books=books,
        names=names,
        master=master,
        sentiment=sentiment,
        news_events=news_events,
        volume_price=volume_price,
        relative_strength=relative_strength,
        matches=matches,
        scores=scores,
    )


def _build_advices(
    *,
    symbols: list[str],
    reads: _Reads,
    earnings_verdicts: dict[str, Any],
    adjudicator: LevelAdjudicator | None,
    positions: Any | None = None,
    today: date | None = None,
) -> list[StockAdvice]:
    """② 逐只出建议。**这一段做判断**，所以它单独成型、单独可读。"""
    engine = AdviceEngine(adjudicator=adjudicator)
    advices: list[StockAdvice] = []
    for symbol in symbols:
        result = reads.scores.get(symbol)
        advice = engine.build(
            symbol=symbol,
            daily=reads.snapshots.get(symbol),
            intraday=reads.intraday.get(symbol),
            wyckoff=reads.wyckoff.get(symbol),
            name=reads.names.get(symbol),
            score=result.score if result else None,
            score_components=result.components if result else None,
            book=reads.books.get(symbol),
            instrument=reads.master.get(symbol),
            match=reads.matches.get(symbol),
        )
        if result:
            advice.warnings.extend(result.warnings)
        _apply_earnings(advice, earnings_verdicts.get(normalize_symbol(symbol)))
        vp = reads.volume_price.get(symbol)
        intraday_read = reads.intraday.get(symbol)
        snapshot = reads.snapshots.get(symbol)
        wyckoff_read = reads.wyckoff.get(symbol)
        if vp is not None:
            advice.volume_price = vp.to_dict()
            advice.distribution = judge_distribution(
                features=vp,
                range_position_60d=(snapshot.range_position_60d if snapshot else None),
                price_vs_vwap_pct=(intraday_read.price_vs_vwap_pct if intraday_read else None),
                wyckoff_phase=(wyckoff_read.bias.value if wyckoff_read else None),
            ).to_dict()
        relative = reads.relative_strength.get(symbol)
        if relative is not None:
            advice.relative_strength = relative.to_dict()
        event = reads.news_events.get(symbol)
        if event is not None:
            advice.event_evidence = event.to_dict()
        _apply_holding(
            advice, positions.get(symbol) if positions is not None else None, snapshot, today
        )
        advices.append(advice)
    return advices


def _apply_holding(
    advice: StockAdvice, position: Any, daily: Any, today: date | None = None
) -> None:
    """把登记的持仓叠到建议上。**你定的止损与目标优先于系统算的**，口径同监控层。

    分三件事：成本与浮盈进 `holding`（简报单独一行）；`plan_stop` / `plan_target`
    替换系统价位并重算盈亏比，替换过的在 warnings 里标明「你设定的」；被套时补回本
    结构。系统自己算的那份价位仍在 `candidates` 里，替换不丢信息。
    """
    if position is None:
        return
    holding = HoldingView.from_position(position, today)
    advice.holding = {
        "cost_price": holding.cost_price,
        "shares": holding.shares,
        "opened_on": holding.opened_on,
        "held_days": holding.held_days,
        "plan_stop": holding.plan_stop,
        "plan_target": holding.plan_target,
        "unrealised_pct": holding.unrealised_pct(advice.last_price),
        "lines": holding.describe(advice.last_price),
    }
    if not advice.has_actionable_levels:
        return
    manual: list[str] = []
    stop = effective_stop(holding, advice.stop)
    if stop != advice.stop:
        advice.stop = stop
        manual.append(f"止损 {stop:.2f}")
    targets = effective_targets(holding, advice.targets)
    if targets != advice.targets:
        advice.targets = targets
        manual.append(f"目标 {targets[0]:.2f}")
    if manual:
        refresh_level_risk(advice)
        advice.warnings.insert(0, f"你设定的 {'、'.join(manual)} 已替换系统价位，盈亏比按它重算")
    _flag_breached_levels(advice, holding)
    advice.breakeven_lines = breakeven_lines(holding.cost_price, advice.last_price, daily)


def _flag_breached_levels(advice: StockAdvice, holding: HoldingView) -> None:
    """你定的止损已被跌破、或目标已到，直说，不再当成一个待执行的计划。

    2026-09-15 实测：登记止损 61.85、现价 61.57，简报写「止损距离仅 -0.11%，处于
    日内噪声范围内」——一个负数距离不是噪声，是已经破位。带 ⚠️ 前缀的警告会让
    `_risk_qualified_plan` 判为不合格，与监控层「跌破止损」告警是同一件事。
    """
    price = advice.last_price
    if not price:
        return
    breached = False
    if holding.plan_stop and price <= holding.plan_stop:
        breached = True
        advice.warnings.insert(
            0,
            f"⚠️ 现价 {price:.2f} 已跌破你设定的止损 {holding.plan_stop:.2f}，"
            "按纪律应先处置持仓；下面的买入区间对这笔仓位不适用",
        )
    if holding.plan_target and price >= holding.plan_target:
        breached = True
        advice.warnings.insert(
            0,
            f"⚠️ 现价 {price:.2f} 已到你设定的目标 {holding.plan_target:.2f}，"
            "先按计划兑现或上移止损，再谈加仓",
        )
    if breached:
        advice.warnings = [text for text in advice.warnings if not text.startswith("止损距离仅 ")]


@dataclass(slots=True)
class _Published:
    """③ 发布的产物：正文、投递到了哪几条通道、简报写在哪。"""

    markdown: str
    delivered: list[str]
    brief_path: str | None
    writer: str
    used_fallback: bool


def _publish(
    *,
    today: date,
    advices: list[StockAdvice],
    facts: dict[str, Any],
    notes: list[str],
    debate: DebatePanel | None,
    writer: BriefWriter | None,
    notifiers: list[Notifier] | None,
    output_dir: str | Path | None,
) -> _Published:
    """③ 多空辩论（可关）→ 写稿 → 投递。**这一段也不做判断**，只组织与发送。"""
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
    # 写稿层可能把辩论整段吞掉或改写。辩论带的是**两方各自的举证**，被改写就
    # 失去了可核对性，所以按原样补回去——同 `morning` 对通读那一节的处置。
    if debate_block and debate_block not in markdown:
        markdown = f"{markdown}\n\n## 多空视角\n\n{debate_block}"

    title = f"个股买卖点 {today}"
    brief_path = None
    if output_dir is not None:
        file_notifier = FileNotifier(output_dir, slug="advice")
        channels = notifiers if notifiers is not None else [ConsoleNotifier(), file_notifier]
        delivered = publish(channels, title, markdown)
        if file_notifier.write_payload(facts, ADVICE_LATEST_NAME) is None:
            notes.append("个股事实包保存失败，后续读取将缺少本轮结果")
        brief_path = written_path(channels, file_notifier)
    else:
        # 没给输出目录仍然是「给我看看」——只有显式传空 notifier 列表（对话那条路）
        # 才是真的不要输出。
        channels = notifiers if notifiers is not None else [ConsoleNotifier()]
        delivered = publish(channels, title, markdown)
    return _Published(
        markdown=markdown,
        delivered=delivered,
        brief_path=brief_path,
        writer=written.writer,
        used_fallback=written.used_fallback,
    )


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
    journal: Journal | None = None,
    progress: Callable[[str], Any] | None = None,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    positions: Any | None = None,
) -> dict[str, Any]:
    today = trade_date or beijing_today()
    notes: list[str] = []
    provider = score_provider or RuleBasedScoreProvider()

    _progress = progress_reporter(progress)

    reads = _collect(
        adapter=adapter,
        symbols=symbols,
        today=today,
        period=period,
        intraday_adapter=intraday_adapter,
        news_source=news_source,
        use_news=use_news,
        use_order_book=use_order_book,
        snapshot_adapter=snapshot_adapter,
        security_master_adapter=security_master_adapter,
        provider=provider,
        notes=notes,
        progress=_progress,
    )

    # 业绩层在这条链路上**只提示、不移除**。`morning` 是系统主动选票，所以否决；
    # `advise` 是你点名问一只票，答案必须是「它怎么样」而不是把它藏起来——「这只
    # 票我不给你看」正是最没用的回答。和 ST 的处理完全同一条理由，见
    # `Asked about explicitly, so it is answered — loudly.`
    earnings_screen = EarningsScreen(
        fetch_earnings(
            earnings_adapter,
            today,
            use_earnings,
            notes,
            off_note="已关闭业绩读数（--no-earnings），本次不提示业绩风险",
            subject="本次不提示业绩风险",
            describe=False,
        )
    )
    advices = _build_advices(
        symbols=symbols,
        reads=reads,
        earnings_verdicts=earnings_screen.evaluate(symbols),
        adjudicator=adjudicator,
        positions=positions,
        today=today,
    )

    facts = _facts(today, advices, notes, period, reads.sentiment, reads.master)
    facts["strategy_version"] = strategy_version
    facts["strategy_parameters"] = {
        "period": period,
        "use_news": use_news,
        "use_order_book": use_order_book,
        "use_earnings": use_earnings,
        "rrr_basis": "entry_high_to_nearest_target",
        "min_rrr": MIN_RRR,
        "transaction_costs_included": False,
    }
    plans = read_plans(output_dir or "briefs", today, until=beijing_naive_now(), notes=notes)
    if plans:
        facts["plan_id"] = plans[-1]["plan_id"]
    recorded = _record_journal(journal, advices, today, facts) if journal is not None else 0
    _progress("② 结构、买卖点和情景已算完，正在整理简报与辩论")

    published = _publish(
        today=today,
        advices=advices,
        facts=facts,
        notes=notes,
        debate=debate,
        writer=writer,
        notifiers=notifiers,
        output_dir=output_dir,
    )
    _progress("③ 个股分析已完成")

    with_levels = sum(1 for item in advices if item.has_actionable_levels)
    return {
        "trade_date": str(today),
        "requested": len(symbols),
        "analyzed": len(advices),
        "with_levels": with_levels,
        "risk_qualified_plans": sum(1 for item in advices if _risk_qualified_plan(item)),
        "advised": with_levels,  # 兼容旧调用方；仅表示已算出价位，不等于可以买
        "count_note": "with_levels/advised 仅表示生成了价位；risk_qualified_plans 也不是即刻买点",
        "writer": published.writer,
        "used_fallback": published.used_fallback,
        "delivered": published.delivered,
        "data_notes": notes,
        "facts": facts,
        "journal_records": recorded,
        "brief_path": published.brief_path,
        "content": published.markdown,
    }


def _risk_qualified_plan(item: StockAdvice) -> bool:
    """仅回答计划的风险收益是否达标，当前价是否触发由监控和问答再判。"""
    return bool(
        item.has_actionable_levels
        and item.is_current_session
        and item.playbook
        and item.rrr is not None
        and item.rrr >= MIN_RRR
        and meets_min_rrr(risk_reward(item, rounded=False))
        and not (item.book or {}).get("at_limit_up")
        and not any(str(note).startswith("⚠️") for note in item.warnings)
    )


def _record_journal(
    journal: Journal,
    advices: list[StockAdvice],
    today: date,
    facts: dict[str, Any],
) -> int:
    """把这一轮的买卖点落台账，供 `review` 事后核对。

    **接这条线是为了让 `adjudication` 字段真的有值。** 三层裁决的记录格式
    （`journal/records.py:_adjudication_record`）和读它的统计
    （`journal/stats.py:_adjudication_edge`）早就写好了，唯独没有生产写入方：
    `morning` 跑在盘前，那时没有分时，压根不经过裁决层。于是「模型裁决比规则默认
    准不准」——**加这一层时唯一没法在设计阶段回答的问题**——在台账里永远是
    `available: False`。裁决器开不开都要记：不开时 `rule_default` 写下的是规则
    自己的选择（`used_fallback=True`），那正是对照组。

    **只记进过裁决层的那些**（`adjudication is not None`，即候选集算得出来）。
    算不出价位的标的记进来会全部落进 `_adjudication_edge` 的「规则默认」组——
    那个函数的 docstring 说得很清楚，掺进没做过决定的样本，差值就被稀释向零。
    弃权的记录**要留**：它没有买入价，但 `abstained_samples` 单独统计它。

    相邻内容一致时由台账去重；变化后追加判断版本，各自核对后续交易日窗口。
    总体统计只取当日首次判断，各策略版本也只取首次，重复运行不增加独立样本。
    这些日线核对不能证明盘中实际成交；当日过程由每日复盘读取监控日志还原。
    """
    records: list[Recommendation] = []
    for advice in advices:
        if advice.adjudication is None:
            continue
        # 参考价缺失时 `from_advice` 返回 None——没有参考价就算不出相对值。
        record = Recommendation.from_advice(advice, today, source=ADVISE_SOURCE, facts=facts)
        if record is not None:
            records.append(record)
    return journal.record_many(list(records))


def _default_news() -> Any | None:
    try:
        return AkshareNewsSource()
    except Exception:
        return None


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
                "industry": (instruments.get(item.symbol) or {}).get("industry"),
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
                "rrr_basis": "entry_high_to_nearest_target",
                "transaction_costs_included": False,
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
                "holding": item.holding,
                "breakeven_lines": list(item.breakeven_lines),
                "volume_price": (item.volume_price),
                "distribution": item.distribution,
                "relative_strength": item.relative_strength,
                "event_evidence": item.event_evidence,
                "intraday": item.intraday,
                "trade_context": (
                    item.trade_context.to_dict() if item.trade_context is not None else None
                ),
                "shortline": (item.shortline.to_dict() if item.shortline is not None else None),
                # 盘口、候选集、裁决三样都进 facts。不只是为了给简报多点素材——
                # 数字闸门比对的就是这份 facts，候选价不在里面的话，模型引用一个
                # 合法候选反而会被当成编造。
                "order_book": item.book or None,
                "candidates": item.candidates.to_dict() if item.candidates else None,
                "adjudication": (item.adjudication.to_dict() if item.adjudication else None),
                "news": (readings[item.symbol].to_dict() if item.symbol in readings else None),
            }
            for item in advices
        ],
        "data_notes": notes,
    }
