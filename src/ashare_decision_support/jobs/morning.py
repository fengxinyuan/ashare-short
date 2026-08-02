"""盘前主链路：舆情 → 大盘趋势 → 板块排序 → 板块内选股 → 候选池 → 台账.

自上而下，和 `post_close.py` 相反：那个给你已经选好的票打分，这个从全市场出发，
先判断该不该做、该做哪个方向的板块，再决定看哪些票。

两段取数是免费档能跑通的关键：先用短窗口批量拉全部成分股（几千只）测板块行为，
再只对通过筛选的几十只拉长历史。舆情采集与成分股日线在 agent 层并行，两条最慢的
链路完全重叠。

产出会写进候选池（`state/watchlist.py:CandidatePool`）供盘中监控使用，并落一份
台账（`journal/`）供事后核对——这两件事让盘前结论不再是一次性的。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.agents import (
    AgentContext,
    NewsAgent,
    SectorMetricsAgent,
    SymbolNewsAgent,
    run_agents,
)
from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.journal import Journal
from ashare_decision_support.journal.records import (
    KIND_CONTROL,
    KIND_RECOMMENDATION,
    Recommendation,
    facts_hash,
)
from ashare_decision_support.adapters.limit_pool import LimitPoolAdapter
from ashare_decision_support.adapters.market_scan import MarketScanAdapter
from ashare_decision_support.adapters.tickflow_market import (
    CompositeMarketScanAdapter,
    TickFlowMarketScanAdapter,
)
from ashare_decision_support.adapters.ths_sector_index import ThsSectorIndexAdapter
from ashare_decision_support.news import (
    AkshareNewsSource,
    CompositeNewsSource,
    LexiconSentiment,
    NewsCache,
    NewsStore,
    WallstreetNewsSource,
)
from ashare_decision_support.news.llm_classifier import (
    ClassifyResult,
    LabelBackedSentiment,
    LlmNewsClassifier,
    build_client,
)
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier
from ashare_decision_support.notify.base import Notifier
from ashare_decision_support.notify.render import render_sector_brief, sector_mandatory_block
from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput, ScoreProvider
from ashare_decision_support.screens import AnnouncementScreen, LiquidityScreen, StScreen
from ashare_decision_support.sectors import SectorPredictor, TickFlowSwTaxonomy
from ashare_decision_support.signals import DailyTechnicalAnalyzer, WyckoffAnalyzer
from ashare_decision_support.signals.dipbuy import measure_dip
from ashare_decision_support.state import SOURCE_SCAN, CandidatePool, WatchEntry
from ashare_decision_support.writer import TemplateWriter, WriteRequest
from ashare_decision_support.writer.base import BriefWriter

MORNING_LATEST_NAME = "morning_latest.json"
CANDIDATE_LOOKBACK_DAYS = 240

# 粗排配额倍数与全局上限，见 `_rank_candidates` 的说明。
NEWS_SHORTLIST_MULTIPLIER = 3

# 低吸门槛过滤后至少要剩这么多标的，否则说明全市场都在高位，此时放宽门槛并明说。
MIN_DIP_CANDIDATES = 20
NEWS_MAX_SYMBOLS = 40


def run_morning(
    adapter: Any,
    output_dir: str | Path,
    news_source: Any | None = None,
    top_sectors: int = 5,
    candidates_per_sector: int = 3,
    sector_level: str = "SW1",
    position_factor: float | None = None,
    score_provider: ScoreProvider | None = None,
    writer: BriefWriter | None = None,
    notifiers: list[Notifier] | None = None,
    trade_date: date | None = None,
    max_constituents: int | None = None,
    use_news: bool = True,
    debate: DebatePanel | None = None,
    journal: Journal | None = None,
    candidate_pool: CandidatePool | None = None,
    news_classifier: Any | None = None,
    use_llm_news: bool = True,
    limit_pool_adapter: Any | None = None,
    use_limit_pool: bool = True,
    sector_index_adapter: Any | None = None,
    use_sector_index: bool = True,
    market_scan_adapter: Any | None = None,
    use_market_scan: bool = True,
    news_store: Any | None = None,
) -> dict[str, Any]:
    output_path = Path(output_dir)
    today = trade_date or date.today()
    notes: list[str] = []
    provider = score_provider or RuleBasedScoreProvider()

    # ---- 1. 板块与成分股 --------------------------------------------------
    taxonomy = TickFlowSwTaxonomy(
        getattr(adapter, "client", adapter), level=sector_level, cache_dir=output_path
    )
    definitions = taxonomy.list_sectors()
    if not definitions:
        raise RuntimeError(f"行情源未返回 {sector_level} 板块分类，无法执行盘前扫描")

    all_symbols = list(dict.fromkeys(symbol for item in definitions for symbol in item.symbols))
    if max_constituents is not None:
        all_symbols = _sample_across_sectors(definitions, max_constituents)
        notes.append(
            f"本次按板块均匀抽样 {len(all_symbols)} 只成分股（--max-constituents），"
            "板块间可比但读数弱于全量扫描"
        )

    # ---- 2. 并行采集：舆情 || 板块动量 ------------------------------------
    source = None
    if use_news:
        source = news_source or _default_news_source(output_path)
        if source is None:
            notes.append("akshare 不可用，本次不含舆情与龙虎榜")
    else:
        notes.append("已禁用舆情输入（--no-news），板块排序仅依据量价动量")

    ctx = AgentContext(trade_date=today, adapter=adapter, news_source=source)
    # 语料库跨日累积，所以它归状态目录管，不归 `--output-dir`——后者常常按日期命名，
    # 那样每天会建一个新库，`first_seen` 就永远等于「今天第一次见到」，这个字段
    # 的全部意义都没了。
    corpus = news_store if news_store is not None else (NewsStore() if use_news else None)
    findings = run_agents(
        [NewsAgent(store=corpus), SectorMetricsAgent(definitions, all_symbols)],
        ctx,
        max_workers=2,
    )
    for finding in findings.values():
        notes.extend(finding.notes)

    sector_finding = findings["sector_metrics"]
    if not sector_finding.ok:
        raise RuntimeError(f"板块动量计算失败，无法产出盘前结论：{sector_finding.error}")
    metrics = sector_finding.get("metrics", {})
    metric_bars = sector_finding.get("bars", {})

    news_finding = findings["news"]
    market_docs = news_finding.get("docs", []) or []
    announcements = news_finding.get("announcements", []) or []
    net_inflow = news_finding.get("net_inflow", {}) or {}
    news_as_of = news_finding.get("as_of")
    if news_as_of:
        notes.append(f"舆情最新一条发布于 {news_as_of}，越旧的条目权重按 12 小时半衰期折减")

    # ---- 3. 融合：舆情归因 → 大盘方向 + 板块排序 -------------------------
    sector_names = [item.name for item in definitions]
    labels = _classify_news(
        news_classifier, market_docs, sector_names, today, output_path, use_llm_news, notes
    )
    analyzer = (
        LabelBackedSentiment(labels.labels, LexiconSentiment())
        if labels.ok
        else LexiconSentiment()
    )

    # 涨停池：指数可以全线上涨而封板意愿同时崩塌，这两件事经常同一天发生，
    # 别的读数一个都看不到。
    limit_snapshot = _limit_pool(limit_pool_adapter, today, use_limit_pool, notes)

    # 同花顺行业指数：与上面的申万成分股聚合并列的**第二套**板块读数。它答的是
    # 聚合答不了的那个问题——收盘落在当日区间的哪个分位。2026-07-31 实测：半导体
    # +4.23% 收在当日最低，同日白酒 +0.63% 收在当日最高。「涨了多少」两者都是正的。
    sector_index = _sector_index(sector_index_adapter, output_path, use_sector_index, notes)

    # 全市场扫描：给候选池提供可交易性门槛（流通市值/成交额），顺带一个比申万
    # 成分股样本更硬的宽度读数。
    market_scan = _market_scan(market_scan_adapter, use_market_scan, notes, adapter)

    predictor = SectorPredictor(analyzer)
    outlooks = predictor.predict(
        definitions, metrics, market_docs, net_inflow, attribution=labels.attribution()
    )
    market = predictor.market_outlook(market_docs, metrics, limit_pool=limit_snapshot)
    notes.extend(market.warnings)

    effective_factor = (
        position_factor if position_factor is not None else market.suggested_position_factor
    )
    if position_factor is not None:
        notes.append(
            f"仓位系数使用手工值 {position_factor:.2f}（系统建议 {market.suggested_position_factor:.2f}）"
        )

    selected = [item for item in outlooks if item.trusted][: max(top_sectors, 0)]
    untrusted = [item for item in outlooks if not item.trusted]
    if untrusted:
        notes.append(
            f"{len(untrusted)}/{len(outlooks)} 个板块成分股覆盖率不足，已排除在选择之外："
            f"{'、'.join(item.name for item in untrusted[:8])}"
            + ("等" if len(untrusted) > 8 else "")
        )
    if not selected:
        notes.append("没有任何板块的覆盖率达标，本次不产出候选")

    # ---- 4. 板块内选股 ----------------------------------------------------
    by_name = {item.name: item for item in definitions}
    shortlist = [
        symbol
        for outlook in selected
        for symbol in by_name[outlook.name].symbols
        if metric_bars.get(symbol)
    ]
    kept, screen_notes = _apply_screens(adapter, shortlist, announcements, market_scan)
    notes.extend(screen_notes)

    candidates, control = _rank_candidates(
        adapter=adapter,
        symbols=kept,
        sector_of={
            symbol: outlook.name
            for outlook in selected
            for symbol in by_name[outlook.name].symbols
        },
        provider=provider,
        today=today,
        per_sector=candidates_per_sector,
        news_source=source,
        announcements=announcements,
        notes=notes,
    )

    # ---- 5. 落候选池与台账 -------------------------------------------------
    pool = candidate_pool if candidate_pool is not None else CandidatePool(today=today)
    pool.replace(
        [
            WatchEntry(
                symbol=item["symbol"],
                name=item.get("name"),
                sector=item.get("sector"),
                source=SOURCE_SCAN,
                score=item.get("score"),
                added_on=str(today),
            )
            for item in candidates
        ],
        today,
    )

    facts = _facts(
        today, selected, candidates, market, effective_factor, notes, news_as_of,
        limit_snapshot, sector_index,
    )
    state = _market_state(market, effective_factor, limit_snapshot)
    recorded = _record_journal(journal or Journal(), candidates, today, facts, state)
    # 对照组和入选组走同一套记录与兑现流程，只是 kind 不同、不推送、不进候选池。
    recorded_control = _record_journal(
        journal or Journal(), control, today, facts, state, kind=KIND_CONTROL
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
        )

    # ---- 6. 多空辩论（可关）与发布 -----------------------------------------
    debate_block = ""
    if debate is not None:
        result = debate.run(facts)
        notes.extend(result.notes)
        debate_block = result.markdown()

    baseline = render_sector_brief(
        trade_date=today,
        outlooks=selected,
        candidates=candidates,
        market=market,
        position_factor=effective_factor,
        data_notes=notes,
        market_outlook=market,
    )
    if debate_block:
        baseline = f"{baseline}\n\n## 多空视角\n\n{debate_block}"

    written = (writer or TemplateWriter()).write(
        WriteRequest(
            title=f"盘前简报 {today}",
            facts=facts,
            fallback_markdown=baseline,
            mandatory_suffix=sector_mandatory_block(effective_factor),
        )
    )
    notes.extend(written.notes)
    markdown = written.markdown
    if debate_block and debate_block not in markdown:
        markdown = f"{markdown}\n\n## 多空视角\n\n{debate_block}"

    title = f"盘前简报 {today}"
    file_notifier = FileNotifier(output_path, slug="morning")
    channels = notifiers if notifiers is not None else [ConsoleNotifier(), file_notifier]
    delivered = [c.capabilities.channel for c in channels if c.send(title, markdown)]
    file_notifier.write_payload(facts, MORNING_LATEST_NAME)
    # The caller may have supplied its own FileNotifier, in which case the one
    # built here never sent and its `last_path` is None. Report the path that
    # was actually written rather than a null beside a "file" delivery.
    brief_path = next(
        (str(c.last_path) for c in channels if getattr(c, "last_path", None)),
        str(file_notifier.last_path) if file_notifier.last_path else None,
    )

    return {
        "title": title,
        "trade_date": str(today),
        "market_direction": market.direction,
        "market_score": market.score,
        "limit_up": market.limit_up_count,
        "blowups": market.blowup_count,
        "blowup_rate": market.blowup_rate,
        "max_streak": market.max_streak,
        "news_attribution": "llm" if labels.ok else "keyword",
        "sectors_ranked": len(outlooks),
        "sectors_selected": [item.name for item in selected],
        "candidates": len(candidates),
        "candidate_symbols": [item["symbol"] for item in candidates],
        "suggested_position_factor": market.suggested_position_factor,
        "position_factor": effective_factor,
        "journal_records": recorded,
        "writer": written.writer,
        "used_fallback": written.used_fallback,
        "delivered": delivered,
        "data_notes": notes,
        "payload_path": str(output_path / MORNING_LATEST_NAME),
        "brief_path": brief_path,
        "content": markdown,
    }


# ---- helpers -------------------------------------------------------------


def _classify_news(
    classifier: Any | None,
    docs: list,
    sector_names: list[str],
    today: date,
    output_path: Path,
    enabled: bool,
    notes: list[str],
) -> ClassifyResult:
    """大模型舆情归因。任何一步失败都退回关键词词典，链路不受影响。"""
    if not enabled:
        notes.append("已关闭大模型舆情归因（--no-llm-news），本次按关键词词典归因")
        return ClassifyResult()
    if not docs:
        return ClassifyResult()

    engine = classifier
    if engine is None:
        try:
            engine = LlmNewsClassifier(build_client(), cache=NewsCache(output_path))
        except Exception:
            engine = None
    if engine is None or not getattr(engine, "configured", False):
        notes.append("未配置 LLM key，舆情归因按关键词词典")
        return ClassifyResult()

    result = engine.classify(docs, sector_names, today)
    notes.extend(result.notes)
    return result


def _limit_pool(adapter: Any | None, today: date, enabled: bool, notes: list[str]) -> Any:
    if not enabled:
        notes.append("已关闭涨停池读数（--no-limit-pool）")
        return None
    source = adapter if adapter is not None else LimitPoolAdapter()
    try:
        snapshot = source.fetch_recent(today)
    except Exception as exc:
        notes.append(f"涨停池不可用（{type(exc).__name__}），本次无连板与炸板读数")
        return None
    notes.extend(snapshot.notes)
    if snapshot.ok:
        notes.extend(snapshot.describe())
    return snapshot


def _market_scan(
    adapter: Any | None, enabled: bool, notes: list[str], data_adapter: Any = None
) -> Any:
    """全市场扫描。取不到就退化成「没有可交易性过滤」，链路照跑。

    这一层失败必须是**放行**而不是拦截：拿不到行情去否决标的，等于让数据缺失
    冒充风险判断。`LiquidityScreen` 在 scan 为 None 时全部放行，并把「未经过滤」
    写进 note。
    """
    if not enabled:
        notes.append("已关闭全市场扫描（--no-market-scan），本次无可交易性过滤")
        return None
    source = adapter if adapter is not None else _default_market_scan(data_adapter)
    try:
        scan = source.fetch()
    except Exception as exc:
        notes.append(f"全市场扫描不可用（{type(exc).__name__}），本次无可交易性过滤")
        return None
    notes.extend(scan.notes)
    if scan.ok:
        notes.extend(scan.describe())
    return scan


def _default_market_scan(data_adapter: Any) -> Any:
    """付费档就两路合并，免费档只有新浪一路。

    合并而非替换：TickFlow 更快、带流通股本和真实涨跌停价，但 PE/PB 要
    `financials.*`（Pro 档报「无公司财务数据查询权限」），那两个字段只有新浪有。
    两路 2026-08-02 实测逐字段吻合到新浪的取整精度，可以互为校验。
    """
    sina = MarketScanAdapter()
    client = getattr(data_adapter, "client", None)
    if client is None or not getattr(data_adapter, "supports_realtime", False):
        return sina
    return CompositeMarketScanAdapter(TickFlowMarketScanAdapter(client), sina)


def _sector_index(
    adapter: Any | None, output_path: Path, enabled: bool, notes: list[str]
) -> Any:
    """同花顺行业指数，独立于申万聚合的第二套板块读数。

    刻意**不参与打分**：同花顺 90 个行业 ≈ 申万二级粒度，硬映到申万一级 31 个
    只会造出一个看起来精确的错数。它并列呈现，由人（或写稿层）去读两套读数的
    分歧——指数涨而宽度弱，说的就是权重股在拉抬。
    """
    if not enabled:
        notes.append("已关闭同花顺行业指数读数（--no-sector-index）")
        return None
    source = adapter if adapter is not None else ThsSectorIndexAdapter(cache_dir=output_path)
    try:
        snapshot = source.fetch()
    except Exception as exc:
        notes.append(f"同花顺行业指数不可用（{type(exc).__name__}），本次无板块日内位置读数")
        return None
    notes.extend(snapshot.notes)
    if snapshot.ok:
        notes.extend(snapshot.describe())
    return snapshot


def _default_news_source(output_path: Path) -> Any | None:
    """akshare 三路 + 华尔街见闻宏观一路。

    分开列而不是合成一个源：akshare 那三路覆盖盘面与政策文件，华尔街见闻补的是
    **数据发布**（PMI/CPI/社融）与海外流动性。实测 2026-08-02 全市场 200 条里
    「PMI/统计局/制造业/美债/降准」命中全为 0——不是权重低，是压根没进来。
    """
    sources: list[Any] = []
    try:
        sources.append(AkshareNewsSource(cache=NewsCache(output_path)))
    except RuntimeError:
        pass
    sources.append(WallstreetNewsSource())
    return CompositeNewsSource(*sources) if sources else None


def _sample_across_sectors(definitions: list, budget: int) -> list[str]:
    """Sample every sector at the same *rate*, not to the same count.

    Two wrong ways to spend a constituent budget:
      - truncate the concatenated list  -> the whole budget lands on whichever
        sectors sort first, and the ranking becomes an artefact of the alphabet;
      - take an equal count per sector  -> a 28-name sector reaches full
        coverage while a 427-name one sits at 5%, and only the small sectors
        clear the trust floor.

    Sampling at a uniform rate leaves every sector with the same coverage ratio,
    which is the only version where their scores are comparable.
    """
    if budget <= 0:
        return []
    total = sum(len(item.symbols) for item in definitions)
    if total <= budget:
        return list(dict.fromkeys(symbol for item in definitions for symbol in item.symbols))

    rate = budget / total
    picked: list[str] = []
    for item in definitions:
        symbols = list(item.symbols)
        take = max(2, round(len(symbols) * rate))
        step = max(1, len(symbols) // take)
        picked.extend(symbols[::step][:take])
    return list(dict.fromkeys(picked))


def _apply_screens(
    adapter: Any,
    symbols: list[str],
    announcements: list[dict],
    scan: Any | None = None,
) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    names = _security_names(adapter, symbols)

    kept, rejected = StScreen().filter({symbol: names.get(symbol) for symbol in symbols})
    if rejected:
        notes.append(f"ST/退市/非可交易板块过滤掉 {len(rejected)} 只")

    # 可交易性。放在 ST 之后、公告之前：它最便宜（本地查表），先砍掉一批能让后面
    # 的公告匹配少做无用功。
    kept, illiquid, stats = LiquidityScreen(scan).filter(kept)
    liquidity_note = stats.note()
    if liquidity_note:
        notes.append(liquidity_note)
    if illiquid:
        notes.append(
            "可交易性剔除示例："
            + "；".join(verdict.reason for verdict in illiquid[:3])
            + ("等" if len(illiquid) > 3 else "")
        )

    risks = AnnouncementScreen().evaluate(announcements)
    blocked = [
        symbol for symbol in kept
        if _risk_for(risks, symbol) and _risk_for(risks, symbol).is_blocked
    ]
    if blocked:
        notes.append(f"公告风险否决 {len(blocked)} 只：{'、'.join(blocked[:8])}")
    kept = [symbol for symbol in kept if symbol not in set(blocked)]
    return kept, notes


def _risk_for(risks: dict, symbol: str):
    return risks.get(symbol.split(".")[0])


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


def _rank_candidates(
    adapter: Any,
    symbols: list[str],
    sector_of: dict[str, str],
    provider: ScoreProvider,
    today: date,
    per_sector: int,
    news_source: Any | None = None,
    announcements: list[dict] | None = None,
    notes: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """两段打分：先量价粗排，再对入围票拉个股舆情精排。返回 (入选, 对照组)。

    个股新闻是**每标的一次 HTTP**，对几百只成分股全拉是几分钟的事，为了一个封顶
    ±6 分的读数不值得。所以先用量价把范围收到每个板块 `per_sector × 3` 只，只对
    这批拉舆情再重排。

    3 倍这个系数是拿封顶反推的：舆情最多改变 ±6/76 的原始分，映射到终分约 ±3.9。
    一只在本板块排到 3 倍配额之外的票，需要越过的分差通常大于这个量。落在网外的
    票会计数并写进 note——被截掉多少必须说出来，不能让「只看了前 N 只」读起来像
    「全看过了」。
    """
    sink = notes if notes is not None else []
    if not symbols:
        return [], []

    bars = _fetch_bars(adapter, symbols, today - timedelta(days=CANDIDATE_LOOKBACK_DAYS), today)
    usable = {symbol: rows for symbol, rows in bars.items() if rows}
    if not usable:
        return [], []

    snapshots = DailyTechnicalAnalyzer().analyze_many(usable)
    wyckoff = {signal.symbol: signal for signal in WyckoffAnalyzer().analyze_many(usable)}
    names = _security_names(adapter, list(usable))
    # 低吸画像算一次全程复用：既进打分（回调是否到位、缩量与否），也落台账。
    dips = {
        symbol: measure_dip(rows, snapshots.get(symbol)) for symbol, rows in usable.items()
    }

    def build_inputs(scope: list[str], sentiment: dict[str, Any]) -> list[ScoreInput]:
        return [
            ScoreInput(
                symbol=symbol,
                bars=usable[symbol],
                snapshot=snapshots[symbol],
                wyckoff=wyckoff.get(symbol),
                news=sentiment.get(symbol),
                dip=dips.get(symbol),
            )
            for symbol in scope
            if symbol in snapshots and symbol in usable
        ]

    scope = [symbol for symbol in usable if symbol in snapshots]

    # 低吸门槛：**没有回调的票不是低吸候选，不管它分多高。**
    #
    # 这是门槛不是权重。dip 在打分里占 10/85，而 trend + wyckoff 占 50——靠加权
    # 永远压不过「趋势强、位置高」的票。2026-08-02 实测：全市场随机 278 只里
    # 27% 处在 none 档、29% 在 ideal 档，但打分选出的 15 只有 10 只是 none。
    # 分布差这么远，说明问题出在「要不要它」而不是「给它几分」。
    #
    # 过滤后不足最低数量时**不回填**，宁可少给几只，也不把追高的票混进低吸清单。
    dipped = [symbol for symbol in scope if _is_dip_candidate(dips.get(symbol))]
    filtered_out = len(scope) - len(dipped)
    if len(dipped) >= MIN_DIP_CANDIDATES:
        if filtered_out:
            sink.append(
                f"低吸门槛：{filtered_out}/{len(scope)} 只处在 20 日高附近（未回调），"
                "本次不作为低吸候选"
            )
        scope = dipped
    elif dipped:
        sink.append(
            f"⚠️ 全市场普遍处于高位，仅 {len(dipped)}/{len(scope)} 只有回调，"
            f"少于最低样本 {MIN_DIP_CANDIDATES}，本次放宽低吸门槛并按分数排序——"
            "这些是次优时机，不是好机会"
        )
    else:
        sink.append("⚠️ 入围标的全部处在 20 日高附近，今日没有低吸位置")

    coarse = provider.score_many(build_inputs(scope, {}))

    shortlist = _take_per_sector(coarse, sector_of, per_sector * NEWS_SHORTLIST_MULTIPLIER)
    if len(shortlist) > NEWS_MAX_SYMBOLS:
        sink.append(
            f"个股舆情预算 {NEWS_MAX_SYMBOLS} 只，粗排入围 {len(shortlist)} 只，"
            f"仅对分数最高的 {NEWS_MAX_SYMBOLS} 只拉取舆情"
        )
        shortlist = shortlist[:NEWS_MAX_SYMBOLS]

    sentiment = _symbol_sentiment(news_source, shortlist, announcements or [], today, sink)
    results = provider.score_many(build_inputs(shortlist, sentiment)) if shortlist else coarse
    if len(coarse) > len(shortlist):
        sink.append(
            f"粗排 {len(coarse)} 只，取每板块前 {per_sector * NEWS_SHORTLIST_MULTIPLIER} 只"
            f"（共 {len(shortlist)} 只）进入舆情精排，其余不再参与最终排序"
        )

    picked: list[dict] = []
    control: list[dict] = []
    per_sector_count: dict[str, int] = {}
    for result in results:
        sector = sector_of.get(result.symbol, "未分类")
        over_quota = per_sector_count.get(sector, 0) >= per_sector
        if not over_quota:
            per_sector_count[sector] = per_sector_count.get(sector, 0) + 1

        snapshot = snapshots.get(result.symbol)
        reading = sentiment.get(result.symbol)
        row = {
            "symbol": result.symbol,
            "name": names.get(result.symbol),
            "sector": sector,
            "score": result.score,
            "reasons": result.reasons,
            "warnings": result.warnings,
            "last_close": snapshot.last_close if snapshot else None,
            "trend_state": snapshot.trend_state.value if snapshot else None,
            "stop_reference": snapshot.stop_reference if snapshot else None,
            "target_reference": snapshot.target_reference if snapshot else None,
            "wyckoff_bias": wyckoff[result.symbol].bias.value if result.symbol in wyckoff else None,
            "news": reading.to_dict() if reading is not None else None,
            # 低吸画像。已进打分（见 `scoring/rule_based.py`），这里落台账供事后分层。
            "dip": (dips.get(result.symbol) or measure_dip([])).to_dict(),
        }
        (control if over_quota else picked).append(row)

    # 对照组取和入选组等量的落选样本。等量是为了让两组的统计噪声量级可比；
    # 取紧挨着的（分数最高的落选者）是因为这才是真正的边缘对照——拿全市场
    # 垫底的票做对照，差出来的是「好票 vs 烂票」，不是「这套规则有没有用」。
    control = control[: len(picked)]
    return picked, control


def _is_dip_candidate(dip: Any) -> bool:
    """回调档位在 shallow 及以上才算低吸候选。

    `deep` 也放进来：那一档在打分里已经被扣分和告警覆盖，是否值得买交给分数决定，
    而不是在门槛这里直接判死——深回调里确实有一部分是有效的反转位置。

    档位算不出来（数据不足）时**放行**。门槛的作用是挡掉「明确没回调」的票，
    不是挡掉「不知道」的票——后者该由样本不足的告警去管。
    """
    band = getattr(dip, "pullback_band", None)
    return band != "none"


def _take_per_sector(results: list, sector_of: dict[str, str], quota: int) -> list[str]:
    counts: dict[str, int] = {}
    picked: list[str] = []
    for result in results:
        sector = sector_of.get(result.symbol, "未分类")
        if counts.get(sector, 0) >= quota:
            continue
        counts[sector] = counts.get(sector, 0) + 1
        picked.append(result.symbol)
    return picked


def _symbol_sentiment(
    news_source: Any | None,
    symbols: list[str],
    announcements: list[dict],
    today: date,
    notes: list[str],
) -> dict[str, Any]:
    if not symbols:
        return {}
    ctx = AgentContext(trade_date=today, news_source=news_source, symbols=symbols)
    finding = run_agents(
        [SymbolNewsAgent(symbols=symbols, announcements=announcements)], ctx, max_workers=1
    )["symbol_news"]
    notes.extend(finding.notes)
    return finding.get("sentiment", {}) or {}


def _fetch_bars(adapter: Any, symbols: list[str], start: date, end: date) -> dict[str, list[dict]]:
    batch = getattr(adapter, "get_historical_bars_batch", None)
    if callable(batch):
        return batch(symbols, start, end)
    return adapter.get_historical_bars(symbols, start, end)


def _market_state(market: Any, position_factor: float | None, limit_pool: Any) -> dict[str, Any]:
    """建议发出当时的大盘快照，原样存进每一条台账记录。

    冗余存储是刻意的：台账是 JSONL，每条多带七八个数比事后按日期 join 便宜得多，
    而且**记录自带分层维度**意味着任何一段区间的数据都能独立分析，不依赖别的文件
    还在不在。

    这里的每个字段都对应一个「超短线该不该出手」的判断维度，缺了就没法回答
    「这套规则在退潮期到底管不管用」。
    """
    ok = limit_pool is not None and getattr(limit_pool, "ok", False)
    return {
        "direction": getattr(market, "direction", None),
        "score": getattr(market, "score", None),
        "breadth": getattr(market, "breadth", None),
        "position_factor": position_factor,
        # 情绪周期位置的三个读数。炸板率说今天封板质量，连板高度说有没有资金
        # 愿意接力，跌停家数说恐慌程度。
        "blowup_rate": getattr(limit_pool, "blowup_rate", None) if ok else None,
        "max_streak": getattr(limit_pool, "max_streak", None) if ok else None,
        "limit_up_count": getattr(limit_pool, "limit_up_count", None) if ok else None,
        "limit_down_count": getattr(limit_pool, "limit_down_count", None) if ok else None,
        # 涨停池的数据日期可能早于请求日期（服务端对非交易日返回最近交易日）。
        "limit_pool_date": str(getattr(limit_pool, "trade_date", "")) or None if ok else None,
    }


def _record_journal(
    journal: Journal,
    candidates: list[dict],
    today: date,
    facts: dict,
    market_state: dict[str, Any] | None = None,
    kind: str = KIND_RECOMMENDATION,
) -> int:
    """把候选落台账。

    参考价与止损/目标都取自**前复权日线**，所以三者同口径，相对百分比有效。
    核对时也用前复权序列，见 `journal/verify.py`。
    """
    digest = facts_hash(facts)
    records: list[Recommendation] = []
    for item in candidates:
        reference = item.get("last_close")
        if not reference:
            continue

        def pct(value: Any) -> float | None:
            return None if not value else round(float(value) / reference - 1.0, 6)

        stop_pct = pct(item.get("stop_reference"))
        target_pct = pct(item.get("target_reference"))
        news = item.get("news") or {}
        records.append(
            Recommendation(
                symbol=item["symbol"],
                trade_date=str(today),
                source="morning",
                name=item.get("name"),
                sector=item.get("sector"),
                score=item.get("score"),
                reference_price=reference,
                stop_price=item.get("stop_reference"),
                target_prices=[item["target_reference"]] if item.get("target_reference") else [],
                # 盘前没有分时，买入区间留空；核对时 entered 只对有区间的记录有意义。
                stop_pct=stop_pct,
                target_pcts=[target_pct] if target_pct is not None else [],
                trend_state=item.get("trend_state"),
                wyckoff_bias=item.get("wyckoff_bias"),
                news_polarity=news.get("polarity"),
                news_intensity=news.get("intensity"),
                news_doc_count=news.get("doc_count"),
                dip=item.get("dip") or {},
                market_state=dict(market_state or {}),
                reasons=item.get("reasons") or [],
                warnings=item.get("warnings") or [],
                facts_hash=digest,
                kind=kind,
            )
        )
    return journal.record_many(list(records))


def _facts(
    today: date,
    outlooks: list,
    candidates: list[dict],
    market,
    position_factor: float,
    notes: list[str],
    news_as_of: str | None = None,
    limit_pool: Any = None,
    sector_index: Any = None,
) -> dict[str, Any]:
    return {
        "trade_date": str(today),
        "news_as_of": news_as_of,
        "sector_index": (
            {
                "source": "同花顺行业指数",
                "trade_date": sector_index.readings[0].trade_date,
                "sectors": len(sector_index.readings),
                # 只带这一层独有的字段。涨跌幅申万那边已经有了，重复带进来只会
                # 让写稿层在两个口径的数字之间挑一个说，而它们本来就不该相等。
                "gainers": [
                    {
                        "name": item.name,
                        "ret_1d": item.ret_1d,
                        "close_position": item.close_position,
                        "volume_ratio": item.volume_ratio,
                        "off_20d_high": item.off_20d_high,
                        "shape": item.shape,
                    }
                    for item in sector_index.top("ret_1d", 8)
                ],
                "losers": [
                    {
                        "name": item.name,
                        "ret_1d": item.ret_1d,
                        "close_position": item.close_position,
                        "off_20d_high": item.off_20d_high,
                        "shape": item.shape,
                    }
                    for item in sector_index.top("ret_1d", 8, reverse=False)
                ],
                "faded": [
                    {
                        "name": item.name,
                        "ret_1d": item.ret_1d,
                        "close_position": item.close_position,
                    }
                    for item in sorted(
                        (
                            r
                            for r in sector_index.readings
                            if r.ret_1d is not None
                            and r.ret_1d > 0
                            and r.close_position is not None
                            and r.close_position <= 20
                        ),
                        key=lambda r: r.close_position or 0,
                    )
                ],
            }
            if sector_index is not None and getattr(sector_index, "ok", False)
            and sector_index.readings
            else None
        ),
        "limit_pool": (
            {
                "trade_date": str(limit_pool.trade_date),
                "limit_up": limit_pool.limit_up_count,
                "blowups": limit_pool.blowup_count,
                "limit_down": limit_pool.limit_down_count,
                "blowup_rate": limit_pool.blowup_rate,
                "max_streak": limit_pool.max_streak,
                "streaks": limit_pool.streak_distribution,
            }
            if limit_pool is not None and limit_pool.ok
            else None
        ),
        "market": {
            "direction": market.direction,
            "score": market.score,
            "trusted": market.trusted,
            "breadth": market.breadth,
            "median_ret_1d": market.median_ret_1d,
            "median_ret_5d": market.median_ret_5d,
            "above_ma20": market.above_ma20,
            "sectors_trusted": market.sectors_trusted,
            "sectors_total": market.sectors_total,
            "constituents_covered": market.constituents_covered,
            "reasons": market.reasons,
            "warnings": market.warnings,
        },
        "position_factor": position_factor,
        "suggested_position_factor": market.suggested_position_factor,
        "position_factor_reasons": market.sentiment.reasons,
        "sectors": [
            {
                "name": item.name,
                "score": item.score,
                "direction": item.direction,
                "ret_1d": item.metrics.ret_1d,
                "ret_5d": item.metrics.ret_5d,
                "breadth": item.metrics.breadth,
                "strong_count": item.metrics.strong_count,
                "volume_ratio": item.metrics.volume_ratio,
                "covered": item.metrics.covered,
                "constituents": item.metrics.constituents,
                "net_inflow": item.net_inflow,
                "reasons": item.reasons,
                "warnings": item.warnings,
                "headlines": item.headlines,
            }
            for item in outlooks
        ],
        "candidates": candidates,
        "data_notes": notes,
    }
