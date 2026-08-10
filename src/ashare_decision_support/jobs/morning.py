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

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.base import (
    apply_security_metadata_rows,
    fetch_daily_bars,
    float_shares,
    metadata_names,
    security_names,
)
from ashare_decision_support.adapters.broad_index import (
    divergence_verdict,
    read_divergence,
)
from ashare_decision_support.adapters.ths_sector_index import (
    load_all_series,
)
from ashare_decision_support.agents import (
    AgentContext,
    NewsAgent,
    SectorMetricsAgent,
    SymbolNewsAgent,
    run_agents,
    timing_note,
)
from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.jobs.morning_facts import (
    build_facts,
    equal_weight_date,
    market_state,
    news_source_counts,
)
from ashare_decision_support.jobs.progress import progress_reporter
from ashare_decision_support.jobs.sources import (
    default_news_source,
    fetch_broad_index,
    fetch_earnings,
    fetch_limit_pool,
    fetch_market_scan,
    fetch_sector_index,
    fetch_trade_flow,
)
from ashare_decision_support.journal import Journal
from ashare_decision_support.journal.records import (
    KIND_CONTROL,
    KIND_RECOMMENDATION,
    KIND_VETOED,
    Recommendation,
    facts_hash,
)
from ashare_decision_support.news import (
    DailyNewsReader,
    DailyRead,
    LexiconSentiment,
    MacroDigest,
    NewsCache,
    NewsStore,
    SocialSnapshotSource,
    ThemeOutcomeStore,
    freshest,
    macro_context_of,
    rank_news_targets,
    read_macro_links,
    scan_themes,
    sector_coverage_snapshot,
)
from ashare_decision_support.news.llm_classifier import (
    ClassifyResult,
    LabelBackedSentiment,
    LlmNewsClassifier,
    build_client,
)
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier
from ashare_decision_support.notify.base import Notifier, publish
from ashare_decision_support.notify.render import (
    render_sector_brief,
    render_theme_radar,
    sector_mandatory_block,
)
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.scoring import (
    RuleBasedScoreProvider,
    ScoreProvider,
    build_factor_contexts,
)
from ashare_decision_support.screens import (
    AnnouncementScreen,
    EarningsScreen,
    LiquidityScreen,
)
from ashare_decision_support.sectors import (
    CONCEPT_LEVEL,
    SectorPredictor,
    SectorRotationStore,
    TickFlowSwTaxonomy,
    explain_today,
    read_rotation,
    read_seesaw,
    read_turnover,
    reverse_index,
)
from ashare_decision_support.state import SOURCE_SCAN, CandidatePool, WatchEntry
from ashare_decision_support.strategy.positioning import resolve_position_sizing
from ashare_decision_support.strategy.regime import RegimeVerdict, regime_from_limit_pool
from ashare_decision_support.strategy.select import analyze_universe, select_from
from ashare_decision_support.symbols import normalize_symbol
from ashare_decision_support.writer import TemplateWriter, WriteRequest
from ashare_decision_support.writer.base import BriefWriter

MORNING_LATEST_NAME = "morning_latest.json"
CANDIDATE_LOOKBACK_DAYS = 240

# 粗排配额倍数与全局上限，见 `_rank_candidates` 的说明。
NEWS_SHORTLIST_MULTIPLIER = 3

NEWS_MAX_SYMBOLS = 40

# 自选股每只带几条自己的消息进通读。比候选池那批（`SYMBOL_NEWS_LIMIT` 10 条）少，
# 是因为通读要的是「有没有属于它自己的事」，不是情绪聚合——条数少而精更好读。
MAX_WATCHLIST_HEADLINES = 5
ROTATION_LOOKBACK_DAYS = 5

# 每路舆情源取多少条。源的默认值是 200，对**板块归因**够用，对**通读**不够：
# 实测按 200 取，华尔街见闻那 1000+ 条里只有最新的 200 条进来，而它们绝大多数是
# 当日 A 股盘面快讯——海外那一节被压到 21 条、宏观数据只剩 1 条。而海外与宏观正是
# 通读存在的理由（`news/daily_read.py`）。取宽到 600。
WIDE_MARKET_LIMIT = 600

# 送进**板块归因**的条数上限。取宽之后不能把多出来的全丢给分类器：它是按批计费的，
# 而多出来的那批绝大多数是海外与地缘消息，本来就归不到 A 股板块（模型对这类的正确
# 回答是空数组）。所以归因仍按原来的量级取最新的一批，通读拿全量——两层要的东西
# 本来就不同，共用一个语料但各取所需。
CLASSIFY_MAX_DOCS = 360


@dataclass(slots=True)
class MorningConfig:
    """盘前主链路的调参项——全是**取值**，不含任何数据源对象。

    和 `Sources` 分开的理由：这里的字段改了只影响产出的宽窄（选几个板块、每个
    板块几只票、手工仓位压多少），而 `Sources` 的字段改了是**换掉取数通道**。
    两类东西原来混在一个 32 参数的签名里，调用方无从判断改哪个是安全的。
    """

    top_sectors: int = 5
    # 概念板块单独取几个。**和 `top_sectors` 分开数**，因为两层是分开排名的：
    # 合成一个名额会让 346 个概念把 31 个行业整体挤出去（概念数量多，出极值的
    # 机会也多），而行业回答的「这是什么生意」并不因此变得不重要。
    top_concepts: int = 3
    candidates_per_sector: int = 3
    sector_level: str = "SW1"
    position_factor: float | None = None
    max_constituents: int | None = None

    # `use_*` 开关与 `Sources` 里同名的适配器是**一对**，语义不能合并：
    #   适配器 = None  → 「没给，按默认建一个」
    #   开关   = False → 「明确不要这一路」
    # 合成「给了就开、没给就关」会让「用默认源」变得无法表达。
    use_news: bool = True
    use_llm_news: bool = False
    use_limit_pool: bool = True
    use_sector_index: bool = True
    use_market_scan: bool = True
    use_earnings: bool = True
    use_broad_index: bool = True
    use_trade_flow: bool = True
    # 概念板块进不进盘前排序。盘中监控（`sectors/intraday.py`）从 2026-08-09 起
    # 就是行业+概念两层，盘前一直只有行业——同一天同一套分类，两条链路看到的
    # 板块集合不一样，这本身就是一处口径分叉。
    use_concepts: bool = True
    explain: bool = False


@dataclass(slots=True)
class Sources:
    """盘前主链路的全部外部通道：行情、舆情、评分、写稿、推送、台账。

    `adapter` 是唯一必填项——没有行情源这条链路无从开始。其余全部可空，空的含义
    统一是「按默认建一个」；要真正关掉某一路，用 `MorningConfig.use_*`。
    """

    adapter: Any
    news_source: Any | None = None
    social_source: Any | None = None
    news_classifier: Any | None = None
    news_store: Any | None = None
    score_provider: ScoreProvider | None = None
    writer: BriefWriter | None = None
    notifiers: list[Notifier] | None = None
    debate: DebatePanel | None = None
    journal: Journal | None = None
    candidate_pool: CandidatePool | None = None
    limit_pool_adapter: Any | None = None
    sector_index_adapter: Any | None = None
    market_scan_adapter: Any | None = None
    earnings_adapter: Any | None = None
    broad_index_adapter: Any | None = None
    trade_flow_adapter: Any | None = None
    industry_taxonomy: Any | None = None
    concept_taxonomy: Any | None = None


# 下面五个 `_Stage*` 是**阶段之间的交接单**，不是领域模型：字段就是下一阶段真正
# 读到的那些值。它们存在的意义是让「第 3 段依赖第 2 段的哪些东西」变成签名里
# 看得见的事——原来这是 530 行里靠 60 多个局部变量隐式串起来的。
@dataclass(slots=True)
class _Universe:
    """① 板块与成分股。"""

    definitions: list
    all_symbols: list[str]
    metadata: dict[str, dict[str, Any]]


@dataclass(slots=True)
class _Collected:
    """② 并行采集的产物：舆情一路 + 板块动量一路。"""

    source: Any
    metrics: dict
    metric_bars: dict
    market_docs: list
    announcements: list
    net_inflow: dict
    market_source_counts: dict
    news_as_of: Any
    news_targets: list
    coverage: dict
    coverage_diagnostics: dict
    raw_doc_count: int


@dataclass(slots=True)
class _Fusion:
    """③ 融合：舆情归因 → 大盘方向 + 板块排序 + 仓位闸门。"""

    labels: ClassifyResult
    daily_read: DailyRead
    macro_links: MacroDigest
    limit_snapshot: Any
    regime: RegimeVerdict
    sector_index: Any
    rotation_price: Any
    turnover_migration: Any
    seesaw: Any
    market_scan: Any
    earnings: Any
    outlooks: list
    market: Any
    broad_index: Any
    index_verdict: Any
    effective_factor: float
    # 仓位决策的**完整**过程，不只是最终值。台账要能分辨「系统就建议 0.5」与
    # 「系统建议 0.9、被周期上限压到 0.5」——两者的 `effective_factor` 相同，
    # 而它们对「仓位上限这条规则有没有救过我们」是相反的证据。
    sizing: Any
    selected: list
    rotation: Any
    theme_radar: list[Any]
    theme_validation: dict[str, Any]


@dataclass(slots=True)
class _Selection:
    """④ 板块内选股。"""

    candidates: list[dict]
    control: list[dict]
    earnings_verdicts: dict
    selected_sector_of: dict[str, str]
    funnel: dict[str, int]


@dataclass(slots=True)
class _Ledger:
    """⑤ 候选池与台账。"""

    facts: dict
    state: Any
    recorded: int


def run_morning(
    sources: Sources,
    output_dir: str | Path,
    *,
    config: MorningConfig | None = None,
    trade_date: date | None = None,
    progress: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """跑一轮盘前扫描，返回产出摘要。

    六个阶段各自是一个函数，交接靠 `_Universe` / `_Collected` / `_Fusion` /
    `_Selection` / `_Ledger` 五张交接单。`notes` 是**唯一**贯穿全程的可变量：
    每一段都往里追加「这次为什么是这个结果」，最后一并进简报和台账。
    """
    cfg = config or MorningConfig()
    output_path = Path(output_dir)
    today = trade_date or beijing_today()
    notes: list[str] = []
    report = progress_reporter(progress)

    universe = _stage_universe(sources, cfg, output_path, notes, report)
    collected = _stage_collect(sources, cfg, universe, today, output_path, notes, report)
    fusion = _stage_fuse(sources, cfg, universe, collected, today, output_path, notes, report)
    selection = _stage_select(sources, cfg, universe, collected, fusion, today, notes)
    ledger = _stage_record(sources, collected, fusion, selection, today, notes, report)
    return _stage_publish(
        sources, collected, fusion, selection, ledger, today, output_path, notes, report
    )


def _stage_universe(
    sources: Sources,
    cfg: MorningConfig,
    output_path: Path,
    notes: list[str],
    report: Callable[[str], Any],
) -> _Universe:
    """① 板块与成分股。"""
    taxonomy, taxonomy_notes = _industry_taxonomy(sources, cfg, output_path)
    notes.extend(taxonomy_notes)
    industries = taxonomy.list_sectors()
    if not industries:
        raise RuntimeError(f"行情源未返回 {cfg.sector_level} 板块分类，无法执行盘前扫描")

    concepts = _concept_definitions(sources, cfg, taxonomy, industries, notes)
    definitions = [*industries, *concepts]
    report(
        f"① 已读取 {len(industries)} 个 {cfg.sector_level} 板块"
        + (f" + {len(concepts)} 个概念" if concepts else "")
        + "，正在并行采集舆情和板块动量"
    )

    # 成分股取**两层的并集**。概念文件比行业文件多覆盖上百只票（华银电力、
    # 华大智造这类不是还没归类的新股），只按行业循环会让它们整只消失——连概念
    # 板块都进不去，简报里也查不到画像。理由同 `docs/ROADMAP.md` §3.1。
    all_symbols = list(dict.fromkeys(symbol for item in definitions for symbol in item.symbols))
    if cfg.max_constituents is not None:
        # **抽样只按行业分层。** 概念之间成分股大量重叠，拿它们做分层会让重叠区
        # 的票被反复抽中，而抽样存在的意义正是「板块间可比」。
        all_symbols = _sample_across_sectors(industries, cfg.max_constituents)
        notes.append(
            f"本次按板块均匀抽样 {len(all_symbols)} 只成分股（--max-constituents），"
            "板块间可比但读数弱于全量扫描"
        )
    return _Universe(
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
    那是一个没人声明过、也没法从代码里读出来的规则。2026-08-10 实测 31 个 SW1
    行业与 346 个概念只撞了「家用电器」一个，丢掉它的代价远小于让一层读数错位。
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
    universe: _Universe,
    today: date,
    output_path: Path,
    notes: list[str],
    report: Callable[[str], Any],
) -> _Collected:
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
        [news_agent, SectorMetricsAgent(universe.definitions, universe.all_symbols)],
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

    return _Collected(
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
        raw_doc_count=news_finding.get("raw_doc_count", 0)
        + int(supplemented.get("raw_doc_count", 0) or 0),
    )


def _stage_fuse(
    sources: Sources,
    cfg: MorningConfig,
    universe: _Universe,
    collected: _Collected,
    today: date,
    output_path: Path,
    notes: list[str],
    report: Callable[[str], Any],
) -> _Fusion:
    """③ 融合：舆情归因 → 大盘方向 + 板块排序 + 仓位闸门。"""
    definitions = universe.definitions
    market_docs = collected.market_docs

    sector_names = [item.name for item in definitions]
    labels = _classify_news(
        sources.news_classifier,
        market_docs[:CLASSIFY_MAX_DOCS],
        sector_names,
        today,
        output_path,
        cfg.use_llm_news,
        notes,
    )
    analyzer = (
        LabelBackedSentiment(labels.labels, LexiconSentiment()) if labels.ok else LexiconSentiment()
    )

    # 舆情通读：把整份清单交给模型读传导链（地缘→原油→石油石化、韩股存储→A股半导体
    # 这类跨市场因果）。**只读不算**——它不进板块分、不进选股、不写台账，产物只是
    # 简报里的一段。逐条打标签那条路（上面的 `labels`）答不了跨条目的因果，这一层
    # 补的正是那块，见 `news/daily_read.py`。
    daily_read = _read_news(
        market_docs, today, output_path, cfg.use_llm_news, collected.source, notes
    )
    macro_links = read_macro_links(market_docs)
    notes.extend(macro_links.notes)
    theme_radar, theme_validation = _theme_radar(
        sources.social_source, today, market_docs, collected.metrics, notes
    )

    # 涨停池：指数可以全线上涨而封板意愿同时崩塌，这两件事经常同一天发生，
    # 别的读数一个都看不到。
    limit_snapshot = fetch_limit_pool(
        sources.limit_pool_adapter,
        today,
        enabled=cfg.use_limit_pool,
        notes=notes,
        off_note="已关闭涨停池读数（--no-limit-pool）",
    )

    # 情绪周期闸门。**这一步先于选股，也先于仓位**——超短同一套规则在退潮期和
    # 启动期的期望值可以是反号的，所以「今天做不做、允许哪一类打法」必须是一个
    # 独立的判断，不是打完分之后的一个折减系数。
    regime = regime_from_limit_pool(limit_snapshot)
    notes.extend(regime.reasons)
    notes.extend(regime.warnings)

    # 同花顺行业指数：与上面的申万成分股聚合并列的**第二套**板块读数。它答的是
    # 聚合答不了的那个问题——收盘落在当日区间的哪个分位。2026-07-31 实测：半导体
    # +4.23% 收在当日最低，同日白酒 +0.63% 收在当日最高。「涨了多少」两者都是正的。
    sector_index = fetch_sector_index(
        sources.sector_index_adapter, output_path, cfg.use_sector_index, notes
    )

    # 板块轮动（价格序列口径）。**排在 `fetch_sector_index` 之后**：那一步顺带把 90 个
    # 板块的整年日线落了盘，这一步读的就是它。但这里**不受 `--no-sector-index`
    # 约束**——那个开关关的是网络取数，这里只是一次磁盘读，绑在一起等于逼人为了
    # 拿轮动读数而必须付那 10 秒。代价是序列可能是旧的，所以 `_rotation_from_series`
    # 会把它截止到哪一天报出来。
    #
    # 与下面 `SectorRotationStore` 的分工：那个记的是**系统自己的排名**稳不稳，
    # 要连续跑若干天才有答案；这个读的是**指数真实价格**，第一次跑就有当年 140 根，
    # 所以「过去一周到一个月轮动到哪了」当天就能回答。
    rotation_price, turnover_migration, seesaw = _rotation_from_series(today, notes)

    # 全市场扫描：给候选池提供可交易性门槛（流通市值/成交额），顺带一个比申万
    # 成分股样本更硬的宽度读数。
    market_scan = fetch_market_scan(
        sources.market_scan_adapter, cfg.use_market_scan, notes, sources.adapter
    )

    # 业绩预告/快报：这条链路上唯一的基本面输入。取不到一律放行，见 `fetch_earnings`。
    earnings = fetch_earnings(sources.earnings_adapter, today, cfg.use_earnings, notes)

    predictor = SectorPredictor(analyzer)
    outlooks = predictor.predict(
        definitions,
        collected.metrics,
        market_docs,
        collected.net_inflow,
        attribution=labels.attribution(),
        fallback_empty_labels=True,
    )
    # 概念彼此重叠，一只股票可能同时属于十几个概念。拿概念指标计算全市场宽度会
    # 重复计权，并让 300 多个概念淹没 31 个行业；大盘口径因此只使用行业指标。
    industry_metrics = {
        item.name: collected.metrics[item.name]
        for item in definitions
        if item.level != CONCEPT_LEVEL and item.name in collected.metrics
    }
    market = predictor.market_outlook(market_docs, industry_metrics, limit_pool=limit_snapshot)
    notes.extend(market.warnings)

    # 宽基指数（市值加权）。**放在 market_outlook 之后、且只往它的 reasons/warnings
    # 里追加，不改 score**：台账里的待兑现记录都是按现行公式打的，改公式会让新旧
    # 记录不可比，而那批记录正是这套系统用来验证自己的全部样本。
    #
    # 它补的是等权口径的方向性盲区：2026-08-03 等权宽度 76%、系统读数「偏多 88.85」，
    # 同一天科创50 -5.08%、每一个宽基都在跌。两个读数都没算错，但只报前者会在
    # 权重崩塌的日子里给出接近满分的方向读数。
    broad_index = fetch_broad_index(sources.broad_index_adapter, cfg.use_broad_index, notes)
    as_of = equal_weight_date(rotation_price)
    index_reasons, index_warnings = read_divergence(
        broad_index, market.breadth, expected_date=as_of
    )
    index_verdict = divergence_verdict(broad_index, market.breadth, as_of)
    market.reasons.extend(index_reasons)
    market.warnings.extend(index_warnings)
    notes.extend(index_warnings)

    # 情绪周期的仓位上限压在系统建议值之上。上限不是目标值；手工值仍然优先，但
    # 这条优先级的警告口径必须和盘后完全一致。
    sizing = resolve_position_sizing(
        cycle_label=regime.cycle.value,
        cycle_cap=regime.position_cap,
        suggested_factor=market.suggested_position_factor,
        manual_factor=cfg.position_factor,
    )
    notes.extend(sizing.notes)
    effective_factor = sizing.effective_factor

    selected = _select_sectors(outlooks, cfg, notes)
    untrusted = [item for item in outlooks if not item.trusted]
    if untrusted:
        notes.append(
            f"{len(untrusted)}/{len(outlooks)} 个板块成分股覆盖率不足，已排除在选择之外："
            f"{'、'.join(item.name for item in untrusted[:8])}"
            + ("等" if len(untrusted) > 8 else "")
        )
    if not selected:
        notes.append("没有任何板块的覆盖率达标，本次不产出候选")
    report(f"③ 已选出 {len(selected)} 个主攻板块，正在筛候选与落台账")

    rotation = SectorRotationStore().remember(
        today,
        cfg.sector_level,
        [item for item in outlooks if item.level != CONCEPT_LEVEL],
        lookback_days=ROTATION_LOOKBACK_DAYS,
        leader_count=max(cfg.top_sectors, 1),
    )
    if rotation.available_days < 2:
        notes.append("板块轮动记忆样本不足，暂不对主线延续性下判断")
    else:
        notes.append(f"近 {rotation.available_days} 个交易样本里，板块轮动属于{rotation.regime}")

    return _Fusion(
        labels=labels,
        daily_read=daily_read,
        macro_links=macro_links,
        limit_snapshot=limit_snapshot,
        regime=regime,
        sector_index=sector_index,
        rotation_price=rotation_price,
        turnover_migration=turnover_migration,
        seesaw=seesaw,
        market_scan=market_scan,
        earnings=earnings,
        outlooks=outlooks,
        market=market,
        broad_index=broad_index,
        index_verdict=index_verdict,
        effective_factor=effective_factor,
        sizing=sizing,
        selected=selected,
        rotation=rotation,
        theme_radar=theme_radar,
        theme_validation=theme_validation,
    )


def _stage_select(
    sources: Sources,
    cfg: MorningConfig,
    universe: _Universe,
    collected: _Collected,
    fusion: _Fusion,
    today: date,
    notes: list[str],
) -> _Selection:
    """④ 板块内选股。"""
    by_name = {item.name: item for item in universe.definitions}
    selected = fusion.selected
    # 行业与概念、概念与概念之间天然重叠。先去重再取长历史和跑筛选，避免同一只票
    # 命中多个板块时重复取数、重复进入漏斗。
    shortlist = list(
        dict.fromkeys(
            symbol
            for outlook in selected
            for symbol in by_name[outlook.name].symbols
            if collected.metric_bars.get(symbol)
        )
    )
    kept, earnings_verdicts, screen_notes = _apply_screens(
        shortlist,
        collected.announcements,
        fusion.market_scan,
        fusion.earnings,
    )
    notes.extend(screen_notes)

    # `selected` 已按板块强弱排好序，所以这里的并列规则「留第一个」= 一只票同时
    # 属于两个入选板块时，归给更强的那个。规则与理由都在 `reverse_index`。
    selected_sector_of = reverse_index(by_name[outlook.name] for outlook in selected)
    factor_contexts = build_factor_contexts(
        selected,
        selected_sector_of,
        net_inflow_by_symbol=collected.net_inflow,
        market_scan=fusion.market_scan,
        earnings_by_symbol=earnings_verdicts,
    )

    candidates, control = _rank_candidates(
        adapter=sources.adapter,
        symbols=kept,
        sector_of=selected_sector_of,
        factor_contexts=factor_contexts,
        provider=sources.score_provider or RuleBasedScoreProvider(),
        today=today,
        per_sector=cfg.candidates_per_sector,
        metadata_by_symbol=universe.metadata,
        news_source=collected.source,
        announcements=collected.announcements,
        notes=notes,
        regime=fusion.regime,
    )

    # 业绩风险/催化已经通过 `factor_contexts` 参与二级排序；这里再把完整的业绩
    # 裁决挂到候选行，供晨报、台账和后续复盘读取。`EarningsScreen` 的风险否决
    # 仍然先于评分发生。
    _attach_earnings(candidates, earnings_verdicts)
    _attach_earnings(control, earnings_verdicts)
    _attach_theme_context(candidates, fusion.theme_radar)

    # 逐笔资金旁证。**只挂在最终候选上**，不参与打分也不参与排序——它是上一个
    # 交易日的成交明细，回答的是「昨天那波买盘里有没有大单、尾盘是不是在走」，
    # 而候选是按量价结构选出来的。两件事分开：结构决定选不选，逐笔只是在选完
    # 之后多给一眼。
    #
    # 只对候选取数而不是全体入围：一只票要翻约 67 页，28 只并发已经是两分多钟，
    # 铺到粗排那几百只会把盘前链路拖垮。
    trade_flow = fetch_trade_flow(
        sources.trade_flow_adapter,
        [item["symbol"] for item in candidates],
        enabled=cfg.use_trade_flow,
        notes=notes,
        off_note="已关闭逐笔资金旁证（--no-trade-flow），候选行不含大单与尾盘读数",
        subject="候选行",
    )
    _attach_trade_flow(candidates, trade_flow)

    return _Selection(
        candidates=candidates,
        control=control,
        earnings_verdicts=earnings_verdicts,
        selected_sector_of=selected_sector_of,
        funnel={
            "universe": len(universe.all_symbols),
            "selected_sector_constituents": len(shortlist),
            "screened_constituents": len(kept),
            "final_candidates": len(candidates),
            "control_sample": len(control),
        },
    )


def _stage_record(
    sources: Sources,
    collected: _Collected,
    fusion: _Fusion,
    selection: _Selection,
    today: date,
    notes: list[str],
    report: Callable[[str], Any],
) -> _Ledger:
    """⑤ 落候选池与台账。"""
    candidates = selection.candidates
    pool = (
        sources.candidate_pool if sources.candidate_pool is not None else CandidatePool(today=today)
    )
    pool.replace(
        [
            WatchEntry(
                symbol=item["symbol"],
                name=item.get("name"),
                sector=item.get("sector"),
                industries=list(item.get("industries") or []),
                industry=item.get("industry"),
                industry_leaf=item.get("industry_leaf"),
                industry_path=item.get("industry_path"),
                concepts=list(item.get("concepts") or []),
                source=SOURCE_SCAN,
                score=item.get("score"),
                added_on=str(today),
            )
            for item in candidates
        ],
        today,
    )

    facts = build_facts(
        today,
        fusion.selected,
        candidates,
        fusion.market,
        fusion.effective_factor,
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
    )
    state = market_state(
        fusion.market,
        fusion.effective_factor,
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
        candidates,
        today,
        facts,
        state,
        macro_context=macro_context,
    )
    # 对照组和入选组走同一套记录与兑现流程，只是 kind 不同、不推送、不进候选池。
    recorded_control = _record_journal(
        journal,
        selection.control,
        today,
        facts,
        state,
        kind=KIND_CONTROL,
        macro_context=macro_context,
    )
    # 否决组：被业绩闸门移除的那些。它们在打分之前就消失了，所以必须单独从
    # `metric_bars` 里取参考价补记——不补的话，这道**明知有代价还是加了**的闸门
    # 永远无法被自己的台账验证，代价只能停留在设计者的自述里。
    recorded_vetoed = _record_vetoed(
        journal,
        selection.earnings_verdicts,
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
    report(f"④ 候选池共 {len(candidates)} 只，正在整理晨报与多空辩论")
    return _Ledger(facts=facts, state=state, recorded=recorded)


def _stage_publish(
    sources: Sources,
    collected: _Collected,
    fusion: _Fusion,
    selection: _Selection,
    ledger: _Ledger,
    today: date,
    output_path: Path,
    notes: list[str],
    report: Callable[[str], Any],
) -> dict[str, Any]:
    """⑥ 多空辩论（可关）与发布。"""
    facts = ledger.facts
    market = fusion.market
    candidates = selection.candidates

    debate_block = ""
    if sources.debate is not None:
        result = sources.debate.run(facts)
        notes.extend(result.notes)
        debate_block = result.markdown()

    baseline = render_sector_brief(
        trade_date=today,
        outlooks=fusion.selected,
        candidates=candidates,
        market=market,
        position_factor=fusion.effective_factor,
        data_notes=notes,
        market_outlook=market,
        rotation=fusion.rotation.to_dict(),
        rotation_price=facts.get("rotation_price"),
        turnover=facts.get("turnover_migration"),
        earnings=facts.get("earnings"),
        theme_radar=facts.get("theme_radar"),
    )
    # 通读放在板块与候选之前：它讲的是「今天整体是什么行情、哪条链在动」，是读
    # 后面那些排序的前提。放在最后等于让人先看结论再看背景。
    read_block = fusion.daily_read.markdown()
    if read_block:
        baseline = _insert_read(baseline, read_block)
    macro_block = _macro_block(fusion.macro_links)
    if macro_block:
        baseline = _insert_macro(baseline, macro_block)
    theme_block = render_theme_radar(facts.get("theme_radar"))
    if theme_block and "## 主题雷达" not in baseline:
        baseline = _insert_theme(baseline, theme_block)
    if debate_block:
        baseline = f"{baseline}\n\n## 多空视角\n\n{debate_block}"

    written = (sources.writer or TemplateWriter()).write(
        WriteRequest(
            title=f"盘前简报 {today}",
            facts=facts,
            fallback_markdown=baseline,
            mandatory_suffix=sector_mandatory_block(fusion.effective_factor),
        )
    )
    notes.extend(written.notes)
    markdown = written.markdown
    if read_block and "## 舆情通读" not in markdown:
        # LLM 写稿层可能把这一节改写掉或整段吞掉。通读带的是**原文引用**，被改写
        # 就失去了可核对性，所以按原样补回去，和下面的多空视角同一个处理。
        markdown = _insert_read(markdown, read_block)
    if macro_block and "## 宏观联动" not in markdown:
        markdown = _insert_macro(markdown, macro_block)
    if theme_block and "## 主题雷达" not in markdown:
        markdown = _insert_theme(markdown, theme_block)
    if debate_block and debate_block not in markdown:
        markdown = f"{markdown}\n\n## 多空视角\n\n{debate_block}"

    title = f"盘前简报 {today}"
    file_notifier = FileNotifier(output_path, slug="morning")
    channels = (
        sources.notifiers if sources.notifiers is not None else [ConsoleNotifier(), file_notifier]
    )
    delivered = publish(channels, title, markdown)
    file_notifier.write_payload(facts, MORNING_LATEST_NAME)
    # The caller may have supplied its own FileNotifier, in which case the one
    # built here never sent and its `last_path` is None. Report the path that
    # was actually written rather than a null beside a "file" delivery.
    brief_path = next(
        (str(c.last_path) for c in channels if getattr(c, "last_path", None)),
        str(file_notifier.last_path) if file_notifier.last_path else None,
    )
    report("⑤ 盘前简报已完成")

    earnings_verdicts = selection.earnings_verdicts
    earnings = fusion.earnings
    rotation_price = fusion.rotation_price
    turnover_migration = fusion.turnover_migration
    return {
        "title": title,
        "trade_date": str(today),
        "market_direction": market.direction,
        "market_score": market.score,
        "limit_up": market.limit_up_count,
        "blowups": market.blowup_count,
        "blowup_rate": market.blowup_rate,
        "max_streak": market.max_streak,
        "news_attribution": "llm" if fusion.labels.ok else "keyword",
        "news_raw_docs": collected.raw_doc_count,
        "news_docs_after_dedup": len(collected.market_docs),
        "news_source_counts": collected.market_source_counts,
        "news_sector_coverage": collected.coverage,
        "news_sector_diagnostics": collected.coverage_diagnostics,
        "news_supplement_targets": [target.name for target in collected.news_targets],
        "news_read_chains": len(fusion.daily_read.chains),
        "news_read_items": fusion.daily_read.item_count,
        "sectors_ranked": len(fusion.outlooks),
        "sectors_selected": [item.name for item in fusion.selected],
        "industry_sectors_selected": [
            item.name for item in fusion.selected if item.level != CONCEPT_LEVEL
        ],
        "concept_sectors_selected": [
            item.name for item in fusion.selected if item.level == CONCEPT_LEVEL
        ],
        "candidates": len(candidates),
        "candidate_symbols": [item["symbol"] for item in candidates],
        "funnel": dict(selection.funnel),
        "suggested_position_factor": market.suggested_position_factor,
        "rotation_regime": fusion.rotation.regime,
        # 价格口径的轮动结论。和上面那个同名不同源，所以键名点明口径。
        "rotation_switch": (
            rotation_price.switch_label
            if rotation_price is not None and getattr(rotation_price, "ok", False)
            else None
        ),
        # 周度口径的切换标签。和上面那个同源不同窗口——长窗口说这个月趋势有没有
        # 反转，这个说这一周资金有没有掉头，两者反号是常态。
        "rotation_adjacent": (
            rotation_price.adjacent_label
            if rotation_price is not None and getattr(rotation_price, "ok", False)
            else None
        ),
        # 全市场成交额的相对变化。占比口径刻意把它除掉了，所以它必须单独留一格：
        # 存量腾挪和增量入场对仓位的含义相反。
        "turnover_total_change": (
            turnover_migration.total_change
            if turnover_migration is not None and getattr(turnover_migration, "ok", False)
            else None
        ),
        "earnings_period": (
            earnings.period if earnings is not None and getattr(earnings, "ok", False) else None
        ),
        "earnings_blocked": sum(1 for item in earnings_verdicts.values() if item.is_blocked),
        "earnings_catalysts": sum(1 for item in earnings_verdicts.values() if item.catalysts),
        "position_factor": fusion.effective_factor,
        "journal_records": ledger.recorded,
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


def _read_news(
    docs: list,
    today: date,
    output_path: Path,
    enabled: bool,
    source: Any | None,
    notes: list[str],
) -> DailyRead:
    """舆情通读。任何一步失败都返回空读数，简报照出。

    自选股与持仓股的消息单独取一次并带进去，让通读能点名回答「我这几只有没有
    自己的事」。这批标的通常只有几只，比候选池那 40 只便宜得多，而它们恰恰是
    最需要逐只交代的——候选池是系统选的，自选股是人自己盯的。
    """
    if not enabled or not docs:
        return DailyRead()

    try:
        from ashare_decision_support.news.daily_read import build_client as build_read_client

        reader = DailyNewsReader(build_read_client(), cache=NewsCache(output_path))
    except Exception:
        return DailyRead()
    if not reader.configured:
        # 未配 key 的提示由 `_classify_news` 出过一次了，这里不重复刷。
        return DailyRead()

    symbol_docs: dict[str, list] = {}
    names: dict[str, str | None] = {}
    try:
        from ashare_decision_support.state import Watchlist

        book = Watchlist()
        watched = book.symbols()
        if watched and source is not None and hasattr(source, "fetch_for_symbols"):
            symbol_docs, _ = source.fetch_for_symbols(watched, limit=MAX_WATCHLIST_HEADLINES)
            names = {entry.symbol: entry.name for entry in book.entries}
    except Exception:
        symbol_docs, names = {}, {}

    result = reader.read(docs, today, symbol_docs=symbol_docs, names=names)
    notes.extend(result.notes)
    return result


def _insert_read(markdown: str, block: str) -> str:
    """把通读插在「大盘趋势」之前，插不进去就放最前面。

    按标题定位而不是按行号：写稿层换一种措辞、多一行提示，行号就错位了，而
    `## 大盘趋势` 是模板与 LLM 两条路都会保留的锚点（`mandatory_suffix` 之外
    唯一稳定的那个）。
    """
    section = f"## 舆情通读\n\n{block}"
    anchor = "## 大盘趋势"
    head, sep, tail = markdown.partition(anchor)
    if not sep:
        return f"{markdown}\n\n{section}"
    return f"{head}{section}\n\n{sep}{tail}"


def _insert_macro(markdown: str, block: str) -> str:
    section = f"## 宏观联动\n\n{block}"
    anchor = "## 大盘趋势"
    head, sep, tail = markdown.partition(anchor)
    if not sep:
        return f"{markdown}\n\n{section}"
    return f"{head}{section}\n\n{sep}{tail}"


def _insert_theme(markdown: str, block: str) -> str:
    anchor = "## 大盘怎么读"
    head, sep, tail = markdown.partition(anchor)
    if not sep:
        return f"{markdown}\n\n{block}"
    return f"{head}{block}\n\n{sep}{tail}"


def _macro_block(digest: MacroDigest) -> str:
    if not digest.ok or not digest.links:
        return ""
    lines: list[str] = []
    for item in digest.links[:6]:
        targets = "、".join(item.targets) if item.targets else "相关链路"
        label = {"bullish": "偏多", "bearish": "偏空", "mixed": "中性"}.get(
            item.direction, item.direction
        )
        expect = (
            "，超预期"
            if item.expectation == "beat"
            else "，不及预期"
            if item.expectation == "miss"
            else "，符合预期"
            if item.expectation == "inline"
            else ""
        )
        lines.append(f"- {item.trigger} → {item.mechanism} → {targets}，**{label}**{expect}")
    return "\n".join(lines)




def _attach_trade_flow(rows: list[dict], flows: dict[str, dict]) -> None:
    """把逐笔摘要挂到候选行上。原地改，与 `_attach_earnings` 同一处置。"""
    for row in rows:
        payload = flows.get(str(row.get("symbol") or ""))
        if payload:
            row["trade_flow"] = dict(payload)












def _social_docs(source: Any | None, today: date) -> tuple[list[Any], list[str]]:
    """Social data remains a discovery-only side channel, never a score input."""
    resolved = source if source is not None else SocialSnapshotSource.from_env()
    if resolved is None:
        return [], ["未配置社媒热点快照，主题雷达仅依据已验证新闻与行情"]
    try:
        return resolved.fetch_market(today, limit=200)
    except Exception as exc:
        return [], [f"社媒热点快照不可用（{type(exc).__name__}），本次跳过"]


def _theme_radar(
    social_source: Any | None,
    today: date,
    market_docs: list[Any],
    metrics: dict[str, Any],
    notes: list[str],
) -> tuple[list[Any], dict[str, Any]]:
    social_docs, social_notes = _social_docs(social_source, today)
    notes.extend(social_notes)
    signals = scan_themes(market_docs, social_docs, metrics)
    validation = ThemeOutcomeStore().record(today, signals, metrics)
    if not validation["trusted"]:
        notes.append(
            "主题雷达验证样本不足，当前状态只作观察："
            f"已记录 {validation['signals_recorded']} 条，"
            f"其中 {validation['signals_with_5d_outcome']} 条已有5日结果"
        )
    if signals:
        notes.append("主题雷达：" + "；".join(f"{item.name}（{item.stage}）" for item in signals[:5]))
    return signals, validation


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
    symbols: list[str],
    announcements: list[dict],
    scan: Any | None = None,
    earnings: Any | None = None,
) -> tuple[list[str], dict[str, Any], list[str]]:
    notes: list[str] = []

    # ST/退市状态不再是候选池的过滤条件。可交易性仍然是独立门槛：它处理的是
    # 成交额和流通市值不足导致的执行问题，不以证券名称或代码段替代真实流动性。
    kept, illiquid, stats = LiquidityScreen(scan).filter(symbols)
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
        symbol
        for symbol in kept
        if _risk_for(risks, symbol) and _risk_for(risks, symbol).is_blocked
    ]
    if blocked:
        notes.append(f"公告风险否决 {len(blocked)} 只：{'、'.join(blocked[:8])}")
    kept = [symbol for symbol in kept if symbol not in set(blocked)]

    # 业绩否决放在最后：它是这几道闸门里唯一需要**跨报告期**取数的，先让便宜的
    # 几道把范围收小。裁决全量返回（不只被否决的那些），因为催化标注要挂到
    # 活下来的候选上。
    screen = EarningsScreen(earnings)
    kept, verdicts, earnings_stats = screen.filter(kept)
    earnings_note = earnings_stats.note()
    if earnings_note:
        notes.append(earnings_note)
    vetoed = [item for item in verdicts.values() if item.is_blocked]
    if vetoed:
        notes.append(
            "业绩否决示例："
            + "；".join(f"{item.symbol} {item.blocking[0]}" for item in vetoed[:3])
            + ("等" if len(vetoed) > 3 else "")
        )
    return kept, verdicts, notes


def _attach_earnings(rows: list[dict], verdicts: dict[str, Any]) -> None:
    """把业绩裁决挂到候选行上。**只加字段与理由，不动 `score`。**

    理由追加在末尾而不是插到最前：打法判据回答「为什么这只票现在可以买」，
    业绩回答「这只票的基本面背景是什么」，前者才是入场依据。
    """
    if not verdicts:
        return
    for row in rows:
        verdict = verdicts.get(normalize_symbol(str(row.get("symbol") or "")))
        if verdict is None:
            continue
        row["earnings"] = verdict.to_dict()
        row.setdefault("reasons", []).extend(verdict.catalysts)
        row.setdefault("warnings", []).extend(verdict.warnings)


def _attach_theme_context(rows: list[dict], themes: list[Any]) -> None:
    """Attach verified sector themes as context, never as a candidate score input."""
    verified = [item for item in themes if getattr(item, "verified_docs", 0) > 0]
    if not verified:
        return
    for row in rows:
        sector = str(row.get("sector") or "")
        matched = [item for item in verified if sector in getattr(item, "sectors", ())]
        if not matched:
            continue
        row["theme_context"] = [
            {
                "name": item.name,
                "stage": item.stage,
                "price_confirmed": item.price_confirmed,
                "verified_docs": item.verified_docs,
                "invalidation": item.invalidation,
            }
            for item in matched
        ]




def _rotation_from_series(today: date, notes: list[str]) -> tuple[Any, Any, Any]:
    """价格序列口径的板块轮动**与成交额迁移**。读的是 `fetch_sector_index` 落盘的那批日线。

    三个读数共用一次磁盘读：输入是同一批序列（`rotation` 用其中的 `close` 读
    「谁在涨」，`turnover` 用 `amount` 读「谁在被交易」，`seesaw` 用 `close` 的
    逐日收益读「哪两个反着走」），分开读会把 90 个文件读三遍，还会让「序列截止到
    哪一天」这句话在三处各报一次。

    **刻意不受 `--no-sector-index` 约束。** 那个开关关掉的是一次网络取数，而这里
    只是一次磁盘读——把两者绑在一起，等于逼人为了拿轮动读数而必须付那 10 秒，
    而序列昨天就已经在盘上了。

    代价是序列可能是旧的，所以**必须报出它截止到哪一天**：跳过取数时这里读到的
    是上一次跑批留下的数据，把昨天的轮动当成今天的，正是这套系统别处一直在防的
    那类错误。读不到就返回 `(None, None)`——首次运行时目录本来就不存在，那是预期状态。
    """
    try:
        series = load_all_series()
    except Exception as exc:
        notes.append(f"板块指数序列读取失败（{type(exc).__name__}），本次无价格口径轮动读数")
        return None, None, None
    if not series:
        notes.append(
            "板块指数序列尚未落盘，本次无价格口径轮动读数"
            "（首次运行属预期；跑完一次即有当年全部日线）"
        )
        return None, None, None

    read = read_rotation(series)
    notes.extend(read.notes)

    turnover = read_turnover(series)
    notes.extend(turnover.notes)

    # 跷跷板要 61 根（60 个日收益），比轮动的 26 根长得多，所以它常常是三者里
    # 最先算不出来的那个。`trusted` 为假时照样返回对象——它的 `notes` 里写着
    # 为什么，而「没有跷跷板」和「算不出来」在简报里必须长得不一样。
    seesaw = read_seesaw(series)
    notes.extend(seesaw.notes)

    if not read.ok:
        return read, (turnover if turnover.ok else None), seesaw

    stamp = str(read.trade_date)
    expected = today.strftime("%Y%m%d")
    if stamp and stamp < expected:
        notes.append(
            f"⚠️ 板块轮动读数截止 {stamp}，早于本次交易日 {expected}——"
            "序列未更新（通常是跳过了同花顺取数），以下轮动结论属于那一天"
        )
    notes.extend(read.observations)
    if turnover.ok:
        notes.extend(turnover.observations)
    notes.extend(seesaw.observations)
    # 榜上今天真的背着走的那几对。这是跷跷板这一层唯一的用途——把「今天银行涨、
    # 电子跌」这件已经发生的事，和它们近 60 日的关系对一次账。**它不外推明天**，
    # 每一句里都带着这句话本身。
    notes.extend(explain_today(seesaw))
    return read, (turnover if turnover.ok else None), seesaw


def _risk_for(risks: dict, symbol: str):
    return risks.get(symbol.split(".", maxsplit=1)[0])


def _rank_candidates(
    adapter: Any,
    symbols: list[str],
    sector_of: dict[str, str],
    provider: ScoreProvider,
    today: date,
    per_sector: int,
    metadata_by_symbol: dict[str, dict[str, Any]] | None = None,
    news_source: Any | None = None,
    announcements: list[dict] | None = None,
    notes: list[str] | None = None,
    regime: RegimeVerdict | None = None,
    factor_contexts: dict[str, Any] | None = None,
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

    # 冰点：一条打法都不允许，直接不产出候选。这一步必须在取数之前——今天不做，
    # 就不该为了「看看有什么」再拉几百只票的日线。
    if regime is not None and regime.halted:
        sink.append(
            f"情绪周期 {regime.cycle.value}：今日不产出任何买入候选（{'；'.join(regime.reasons)}）"
        )
        return [], []

    bars = fetch_daily_bars(
        adapter, symbols, today - timedelta(days=CANDIDATE_LOOKBACK_DAYS), today
    )
    # 流通股本先取：换手率与筹码分布都靠它，缺了这两项整体弃权而不是算个错的。
    # 和 `security_names` 打的是同一个接口，多取一个字段不多一次请求。
    shares = float_shares(adapter, symbols)
    analysis = analyze_universe(bars, shares)
    if not analysis.bars:
        return [], []
    names = _symbol_names(adapter, list(analysis.bars), metadata_by_symbol)
    if not shares:
        sink.append("未取到流通股本，本次无换手率与筹码分布估算")

    # 粗排：只用量价，不拉舆情。打法门槛在这一步就生效——**趋势决定这只票能用
    # 哪些打法，情绪周期决定今天允许哪些**，交集为空就不选。这取代了改造前那道
    # 「回调过没有」的单一门槛。
    coarse = select_from(
        analysis,
        regime=regime,
        names=names,
        sectors=sector_of,
        contexts=factor_contexts,
        provider=provider,
    )
    sink.extend(coarse.notes)
    if not coarse.selected:
        return [], []

    shortlist = _take_per_sector(coarse.selected, sector_of, per_sector * NEWS_SHORTLIST_MULTIPLIER)
    if len(shortlist) > NEWS_MAX_SYMBOLS:
        sink.append(
            f"个股舆情预算 {NEWS_MAX_SYMBOLS} 只，粗排入围 {len(shortlist)} 只，"
            f"仅对分数最高的 {NEWS_MAX_SYMBOLS} 只拉取舆情"
        )
        shortlist = shortlist[:NEWS_MAX_SYMBOLS]

    sentiment = _symbol_sentiment(news_source, shortlist, announcements or [], today, sink, names)
    # 精排复用同一份结构读数，只重打一遍分。**不取它的 notes**——漏斗那句话
    # 描述的是全体入围到满足判据的收敛，而这一趟的 scope 已经是粗排选出来的
    # 那批，再报一次会得到一个口径不同、数字更小的同名说明。
    fine = select_from(
        analysis,
        regime=regime,
        scope=shortlist,
        names=names,
        sectors=sector_of,
        sentiment=sentiment,
        contexts=factor_contexts,
        provider=provider,
    )
    if len(coarse.selected) > len(shortlist):
        sink.append(
            f"粗排 {len(coarse.selected)} 只，取每板块前 "
            f"{per_sector * NEWS_SHORTLIST_MULTIPLIER} 只"
            f"（共 {len(shortlist)} 只）进入舆情精排，其余不再参与最终排序"
        )

    picked: list[dict] = []
    control: list[dict] = []
    per_sector_count: dict[str, int] = {}
    for item in fine.selected:
        sector = sector_of.get(item.symbol, "未分类")
        over_quota = per_sector_count.get(sector, 0) >= per_sector
        if not over_quota:
            per_sector_count[sector] = per_sector_count.get(sector, 0) + 1
        row = item.to_row()
        row["sector"] = sector
        (control if over_quota else picked).append(row)

    # 对照组取和入选组等量的落选样本。等量是为了让两组的统计噪声量级可比；
    # 取紧挨着的（分数最高的落选者）是因为这才是真正的边缘对照——拿全市场
    # 垫底的票做对照，差出来的是「好票 vs 烂票」，不是「这套规则有没有用」。
    control = control[: len(picked)]
    return (
        apply_security_metadata_rows(picked, metadata_by_symbol or {}, prefer_name=True),
        apply_security_metadata_rows(control, metadata_by_symbol or {}, prefer_name=True),
    )


def _select_sectors(outlooks: list, cfg: MorningConfig, notes: list[str]) -> list:
    """选主攻板块。**行业与概念各自排名、各取各的名额。**

    并成一张榜是错的，理由与 `sectors/intraday.py` 开头那条一样：346 个概念和
    31 个行业放进同一张榜，前者仅凭数量就能包揽前几名——出极值的机会多得多，
    而那不代表它们更值得做。分开取之后，「今天最强的生意」和「今天最强的题材」
    是两个各自成立的答案，简报里也能并排读。

    `level` 为空的当行业算：单层调用方（老测试、只传 `industry_taxonomy` 的
    路径）行为一字不变。
    """
    trusted = [item for item in outlooks if item.trusted]
    concepts = [item for item in trusted if item.level == CONCEPT_LEVEL]
    industries = [item for item in trusted if item.level != CONCEPT_LEVEL]

    picked = industries[: max(cfg.top_sectors, 0)]
    if concepts:
        picked = [*picked, *concepts[: max(cfg.top_concepts, 0)]]
        notes.append(
            f"主攻板块 {len(picked)} 个：行业 {len(picked) - min(len(concepts), max(cfg.top_concepts, 0))} 个"
            f" + 概念 {min(len(concepts), max(cfg.top_concepts, 0))} 个（两层分开排名）"
        )
    # 合并之后再按分数排一次，只为让下游拿到的顺序仍然是「强的在前」——
    # `sectors/base.py:reverse_index` 靠这个顺序决定一只票归给哪个板块。
    return sorted(picked, key=lambda item: item.score, reverse=True)


def _industry_taxonomy(
    sources: Sources,
    cfg: MorningConfig,
    output_path: Path,
) -> tuple[Any, list[str]]:
    static = sources.industry_taxonomy
    if static is not None:
        try:
            definitions = static.list_sectors()
        except Exception as exc:
            notes = [f"静态分类不可用，回退 TickFlow（{type(exc).__name__}）"]
        else:
            if definitions:
                notes = list(getattr(static, "notes", []) or [])
                notes.append("板块分类来源：同花顺静态 taxonomy（核心链路优先走静态分类）")
                return static, notes
            notes = ["静态分类返回空结果，回退 TickFlow"]

    return (
        TickFlowSwTaxonomy(
            getattr(sources.adapter, "client", sources.adapter),
            level=cfg.sector_level,
            cache_dir=output_path,
        ),
        notes if static is not None else [],
    )


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
        industries = list(getattr(profile, "industries", ()) or ())
        concepts = list(getattr(profile, "concepts", ()) or ())
        rows[normalize_symbol(symbol)] = {
            "symbol": normalize_symbol(symbol),
            "name": getattr(profile, "name", None),
            "industries": industries,
            "industry": industries[0] if industries else None,
            "industry_leaf": industries[-1] if industries else None,
            "industry_path": "·".join(item for item in industries if item),
            "concepts": concepts,
        }
    return rows


def _symbol_names(
    adapter: Any,
    symbols: list[str],
    metadata_by_symbol: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str | None]:
    names = metadata_names(metadata_by_symbol or {}, symbols)
    missing = [symbol for symbol in symbols if not names.get(symbol)]
    if missing:
        names.update(security_names(adapter, missing))
    return names


def _take_per_sector(items: list, sector_of: dict[str, str], quota: int) -> list[str]:
    counts: dict[str, int] = {}
    picked: list[str] = []
    for item in items:
        sector = sector_of.get(item.symbol, "未分类")
        if counts.get(sector, 0) >= quota:
            continue
        counts[sector] = counts.get(sector, 0) + 1
        picked.append(item.symbol)
    return picked


def _symbol_sentiment(
    news_source: Any | None,
    symbols: list[str],
    announcements: list[dict],
    today: date,
    notes: list[str],
    names: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    if not symbols:
        return {}
    ctx = AgentContext(trade_date=today, news_source=news_source, symbols=symbols)
    finding = run_agents(
        [SymbolNewsAgent(symbols=symbols, announcements=announcements, names=names)],
        ctx,
        max_workers=1,
    )["symbol_news"]
    notes.extend(finding.notes)
    return finding.get("sentiment", {}) or {}




def _pct_against(value: Any, reference: float) -> float | None:
    """相对参考价的涨跌幅。

    提到模块层而不是留在循环里定义闭包：闭包捕获的是**循环变量本身**，
    今天它恰好每轮都立刻调用所以没出事，但只要有人把调用挪到循环外
    （比如攒起来批量算），所有记录就会一起用上最后一只票的参考价——
    而且不会报错，只会让台账里的百分比整体偏掉。
    """
    return None if not value else round(float(value) / reference - 1.0, 6)


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
        closes = [
            bar.get("close")
            for bar in (bars_by_symbol.get(symbol) or [])
            if isinstance(bar.get("close"), (int, float))
        ]
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
                earnings=forecast,
                reasons=[],
                warnings=list(verdict.blocking),
                market_state=dict(market_state or {}),
                macro_context=dict(macro_context or {}),
                facts_hash=digest,
                kind=KIND_VETOED,
            )
        )
    return journal.record_many(records)


def _record_journal(
    journal: Journal,
    candidates: list[dict],
    today: date,
    facts: dict,
    market_state: dict[str, Any] | None = None,
    kind: str = KIND_RECOMMENDATION,
    macro_context: dict[str, Any] | None = None,
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

        stop_pct = _pct_against(item.get("stop_reference"), reference)
        target_pct = _pct_against(item.get("target_reference"), reference)
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
            )
        )
    return journal.record_many(list(records))




