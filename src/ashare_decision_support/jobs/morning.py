"""盘前流程编排与板块、舆情采集。

先用成分股短窗口日线计算板块动量，再为入围标的获取长历史。
融合、选股、台账与发布分别由 morning_fuse/select/record/publish 负责，
通过 morning_stages 的数据结构交接。候选进入报告和台账，监控名单由用户指定。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.agents import (
    AgentContext,
    NewsAgent,
    SectorMetricsAgent,
    run_agents,
    timing_note,
)
from ashare_decision_support.jobs.morning_facts import metric_bar_freshness, news_source_counts
from ashare_decision_support.jobs.morning_fuse import stage_fuse
from ashare_decision_support.jobs.morning_publish import stage_publish
from ashare_decision_support.jobs.morning_record import stage_record
from ashare_decision_support.jobs.morning_select import stage_select
from ashare_decision_support.jobs.morning_stages import (
    Collected,
    MorningConfig,
    Sources,
    Universe,
)
from ashare_decision_support.jobs.progress import progress_reporter
from ashare_decision_support.jobs.sources import default_news_source
from ashare_decision_support.news import (
    NewsStore,
    freshest,
    rank_news_targets,
    sector_coverage_snapshot,
)
from ashare_decision_support.schedule.clock import AUCTION_OPEN
from ashare_decision_support.schedule.timezone import beijing_naive_now, beijing_today
from ashare_decision_support.sectors import SectorTaxonomyError, ThsIndustryTaxonomy
from ashare_decision_support.symbols import normalize_symbol

# 每路舆情源取多少条。源的默认值是 200，对**板块归因**够用，对**通读**不够：
# 实测按 200 取，华尔街见闻那 1000+ 条里只有最新的 200 条进来，而它们绝大多数是
# 当日 A 股盘面快讯——海外那一节被压到 21 条、宏观数据只剩 1 条。而海外与宏观正是
# 通读存在的理由（`news/daily_read.py`）。取宽到 600。
WIDE_MARKET_LIMIT = 600
LIVE_QUOTE_MAX_AGE_SECONDS = 300


def run_morning(
    sources: Sources,
    output_dir: str | Path,
    *,
    config: MorningConfig | None = None,
    trade_date: date | None = None,
    progress: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """跑一轮盘前扫描，返回产出摘要。

    六个阶段各自是一个函数，交接靠 `Universe` / `Collected` / `Fusion` /
    `Selection` / `Ledger` 五张交接单。`notes` 是**唯一**贯穿全程的可变量：
    每一段都往里追加「这次为什么是这个结果」，最后一并进简报和台账。
    """
    cfg = config or MorningConfig()
    output_path = Path(output_dir)
    today = trade_date or beijing_today()
    notes: list[str] = []
    report = progress_reporter(progress)

    universe = _stage_universe(sources, cfg, notes, report)
    collected = _stage_collect(sources, cfg, universe, today, output_path, notes, report)
    fusion = stage_fuse(sources, cfg, universe, collected, today, output_path, notes, report)
    selection = stage_select(sources, cfg, universe, collected, fusion, today, notes)
    live_count = _attach_live_quotes(selection.candidates, fusion.market_scan, today)
    metric_date, current_share = metric_bar_freshness(collected.metric_bars, today)
    # Once auction starts, yesterday's complete daily bars remain useful for
    # research, but must not become today's executable recommendations.
    execution_blocks: list[str] = []
    if today == beijing_today():
        # After the auction starts, yesterday's completed bars cannot support a
        # current-day entry, including at lunch and after the closing bell.
        if beijing_naive_now().time() >= AUCTION_OPEN and current_share < 0.8:
            execution_blocks.append(
                f"选股结构日线主要截至 {metric_date or '未知'}，当日完整日线覆盖率 "
                f"{current_share:.0%}，不足 80%；实时快照覆盖候选 {live_count}/{len(selection.candidates)} 只"
            )
        if fusion.market_scan is None or not getattr(fusion.market_scan, "ok", False):
            execution_blocks.append("全市场扫描缺失，可交易性过滤没有可靠输入")
        elif not getattr(fusion.market_scan, "complete", True):
            execution_blocks.append("全市场扫描仅部分成功，宽度与可交易性覆盖不完整")
    if execution_blocks:
        notes.append("；".join(execution_blocks) + "；候选仅供观察，本轮不写入可执行建议台账")
    ledger = stage_record(
        sources,
        collected,
        fusion,
        selection,
        today,
        notes,
        report,
        config=cfg,
        output_path=output_path,
        actionable=not execution_blocks,
        execution_blocks=execution_blocks,
        metric_date=metric_date,
    )
    return stage_publish(
        sources, collected, fusion, selection, ledger, today, output_path, notes, report
    )


def _attach_live_quotes(
    candidates: list[dict], scan: Any, today: date, *, now: datetime | None = None
) -> int:
    """把盘中实时快照附到历史结构候选，不拿未收盘报价冒充完整日线。"""
    stamp = getattr(scan, "as_of", None)
    if not getattr(scan, "ok", False) or not getattr(scan, "session_in_progress", False):
        return 0
    if stamp is None or stamp.date() != today:
        return 0
    age = ((now or beijing_naive_now()) - stamp).total_seconds()
    if not 0 <= age <= LIVE_QUOTE_MAX_AGE_SECONDS:
        return 0
    count = 0
    for candidate in candidates:
        row = scan.get(candidate["symbol"])
        price = getattr(row, "price", None)
        if isinstance(price, (int, float)) and price > 0:
            candidate["live_price"] = float(price)
            candidate["live_quote_as_of"] = stamp.isoformat(timespec="seconds")
            count += 1
    return count


def _stage_universe(
    sources: Sources,
    cfg: MorningConfig,
    notes: list[str],
    report: Callable[[str], Any],
) -> Universe:
    """① 板块与成分股。"""
    taxonomy = sources.industry_taxonomy
    if taxonomy is None:
        taxonomy = ThsIndustryTaxonomy()
    try:
        industries = taxonomy.list_sectors()
    except Exception as exc:
        raise SectorTaxonomyError(f"同花顺行业分类不可用，无法执行盘前扫描：{exc}") from exc
    if not industries:
        raise SectorTaxonomyError("同花顺行业分类为空，无法执行盘前扫描")
    notes.extend(list(getattr(taxonomy, "notes", []) or []))
    notes.append("板块分类来源：同花顺（第三方静态快照）")

    concepts = _concept_definitions(sources, cfg, taxonomy, industries, notes)
    definitions = [*industries, *concepts]
    report(
        f"① 已读取 {len(industries)} 个同花顺行业板块"
        + (f" + {len(concepts)} 个概念" if concepts else "")
        + "，正在并行采集舆情和板块动量"
    )

    # 行业与概念取并集，保留只有概念信息的股票。
    all_symbols = list(dict.fromkeys(symbol for item in definitions for symbol in item.symbols))
    if cfg.max_constituents is not None:
        # **抽样只按行业分层。** 概念之间成分股大量重叠，拿它们做分层会让重叠区
        # 的票被反复抽中，而抽样存在的意义正是「板块间可比」。
        all_symbols = _sample_across_sectors(industries, cfg.max_constituents)
        notes.append(
            f"诊断模式：本次按板块均匀抽样 {len(all_symbols)} 只成分股"
            f"（--max-constituents={cfg.max_constituents}），"
            "只用于快速检查链路；覆盖率、主攻板块和候选不可视为正式全量结论"
        )
    return Universe(
        definitions=definitions,
        all_symbols=all_symbols,
        metadata=_taxonomy_metadata(taxonomy, all_symbols),
    )


def _concept_definitions(
    sources: Sources,
    cfg: MorningConfig,
    industry_taxonomy: Any,
    industries: list,
    notes: list[str],
) -> list:
    """概念板块的定义表。取不到就返回空表，**不拖垮行业那条主路**。

    概念这一路是加进来的第二个维度，不是必需项：静态文件挂掉时盘前照样要出得来
    一份只有行业的排序。所以这里吞掉异常并留 note，而 `_industry_taxonomy` 那条
    取不到是要抛的——没有行业分类，这条链路根本无从开始。

    **同名的概念直接丢掉。** 板块的各层读数（动量、舆情、龙虎榜净额）全部按
    `name` 索引，重名会让两层的数据互相覆盖，而覆盖的方向取决于字典写入顺序——
    因此保留同名行业、跳过概念，并在 notes 中记录被跳过的名称。
    """
    if not cfg.use_concepts:
        notes.append("已关闭概念板块（--no-concepts），本次只排行业")
        return []
    taxonomy = sources.concept_taxonomy
    if taxonomy is None:
        # 默认两层来自同一份同花顺画像。复用 source 后，adapter 的实例缓存保证
        # 两边看到同一快照且只下载一次；自定义 taxonomy 没有 source 时仍保持
        # 行业单层，不凭空替换用户提供的数据口径。
        shared_source = getattr(industry_taxonomy, "source", None)
        if shared_source is None:
            notes.append("概念分类未提供，本次只排行业")
            return []
        from ashare_decision_support.sectors import ThsConceptTaxonomy

        taxonomy = ThsConceptTaxonomy(shared_source)
    try:
        concepts = taxonomy.list_sectors()
    except Exception as exc:
        notes.append(f"概念板块不可用，本次板块排序只有行业维度（{type(exc).__name__}）")
        return []

    taken = {item.name for item in industries}
    kept = [item for item in concepts if item.name not in taken]
    if len(kept) != len(concepts):
        clashed = sorted({item.name for item in concepts} & taken)
        notes.append(
            f"{len(concepts) - len(kept)} 个概念与行业重名已跳过（{'、'.join(clashed)}）："
            "板块读数按名字索引，重名会让两层互相覆盖"
        )
    if kept:
        notes.append(f"概念板块已进入盘前排序：{len(kept)} 个，与行业分开排名、分开取名额")
    return kept


def _stage_collect(
    sources: Sources,
    cfg: MorningConfig,
    universe: Universe,
    today: date,
    output_path: Path,
    notes: list[str],
    report: Callable[[str], Any],
) -> Collected:
    """② 并行采集：舆情 || 板块动量。两条最慢的链路在这里完全重叠。"""
    source = None
    if cfg.use_news:
        source = sources.news_source or default_news_source(output_path)
        if source is None:
            notes.append("akshare 不可用，本次不含舆情与龙虎榜")
    else:
        notes.append("已禁用舆情输入（--no-news），板块排序仅依据量价动量")

    ctx = AgentContext(trade_date=today, adapter=sources.adapter, news_source=source)
    # 语料库跨日累积，所以它归状态目录管，不归 `--output-dir`——后者常常按日期命名，
    # 那样每天会建一个新库，`first_seen` 就永远等于「今天第一次见到」，这个字段
    # 的全部意义都没了。
    corpus = (
        sources.news_store
        if sources.news_store is not None
        else (NewsStore() if cfg.use_news else None)
    )
    news_agent = NewsAgent(store=corpus, market_limit=WIDE_MARKET_LIMIT)
    findings = run_agents(
        [
            news_agent,
            SectorMetricsAgent(
                universe.definitions,
                universe.all_symbols,
                reuse_completed=not cfg.force_refresh_daily,
            ),
        ],
        ctx,
        max_workers=2,
    )
    for finding in findings.values():
        notes.extend(finding.notes)
    _timing = timing_note(findings)
    if _timing:
        notes.append(_timing)
    report("② 舆情与板块读数已到位，开始融合大盘方向、涨停池和行业指数")

    sector_finding = findings["sector_metrics"]
    if not sector_finding.ok:
        raise RuntimeError(f"板块动量计算失败，无法产出盘前结论：{sector_finding.error}")
    metrics = sector_finding.get("metrics", {})
    metric_bars = sector_finding.get("bars", {})

    news_finding = findings["news"]
    market_docs = news_finding.get("docs", []) or []
    announcements = news_finding.get("announcements", []) or []
    net_inflow = news_finding.get("net_inflow", {}) or {}
    market_source_counts = news_finding.get("market_source_counts", {}) or {}
    news_as_of = news_finding.get("as_of")
    if news_as_of:
        notes.append(f"舆情最新一条发布于 {news_as_of}，越旧的条目权重按 12 小时半衰期折减")

    # 先从行情反查舆情盲区：持续上涨、广度和量比都不差，但首轮快讯几乎
    # 没有对应行业词的板块，做一次更宽的定向补抓。目标排序只决定采集优先级，
    # 不直接给板块加分。
    news_targets = rank_news_targets(universe.definitions, metrics, market_docs, net_inflow)
    supplemented, supplement_notes = news_agent.supplement_market(ctx, news_targets, market_docs)
    notes.extend(supplement_notes)
    supplement_docs = supplemented.get("docs", []) or []
    if supplement_docs:
        market_docs = [*supplement_docs, *market_docs]
        newest = freshest(market_docs)
        news_as_of = newest.isoformat(timespec="minutes") if newest else news_as_of
    notes.append(
        f"行情反查出 {len(news_targets)} 个板块进入定向舆情补抓："
        + "、".join(target.name for target in news_targets)
        if news_targets
        else "没有板块同时满足趋势强度与舆情覆盖不足条件，未启动定向补抓"
    )
    coverage_diagnostics = sector_coverage_snapshot(universe.definitions, metrics, market_docs)
    coverage = {name: item["doc_count"] for name, item in coverage_diagnostics.items()}
    uncovered = [target.name for target in news_targets if coverage.get(target.name, 0) == 0]
    if uncovered:
        notes.append(f"板块定向补抓目标仍无匹配消息：{'、'.join(uncovered)}")
    blind = [
        name for name, item in coverage_diagnostics.items() if item["status"] == "行情强但新闻不足"
    ]
    if blind:
        notes.append("行业覆盖诊断：行情强但新闻不足 " + "、".join(blind[:8]))
    market_source_counts = dict(market_source_counts)
    market_source_counts["weighted"] = news_source_counts(market_docs)
    market_source_counts["supplemented"] = len(supplement_docs)

    return Collected(
        source=source,
        metrics=metrics,
        metric_bars=metric_bars,
        market_docs=market_docs,
        announcements=announcements,
        net_inflow=net_inflow,
        market_source_counts=market_source_counts,
        news_as_of=news_as_of,
        news_targets=news_targets,
        coverage=coverage,
        coverage_diagnostics=coverage_diagnostics,
        corpus=corpus,
        raw_doc_count=news_finding.get("raw_doc_count", 0)
        + int(supplemented.get("raw_doc_count", 0) or 0),
    )


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


def _taxonomy_metadata(
    taxonomy: Any,
    symbols: list[str],
) -> dict[str, dict[str, Any]]:
    snapshot = getattr(taxonomy, "snapshot", None)
    profiles = getattr(snapshot, "profiles", None)
    if not isinstance(profiles, dict):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        profile = profiles.get(normalize_symbol(symbol))
        if profile is None:
            continue
        concepts = list(getattr(profile, "concepts", ()) or ())
        rows[normalize_symbol(symbol)] = {
            "symbol": normalize_symbol(symbol),
            "name": getattr(profile, "name", None),
            "industry": getattr(profile, "industry", None),
            "concepts": concepts,
        }
    return rows
