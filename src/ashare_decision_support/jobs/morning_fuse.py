"""③ 融合：舆情归因 → 大盘方向 → 板块排序 → 仓位闸门。

从 `jobs/morning.py` 按判定阶段搬出来的一段。判据和上一轮拆 `notify/render.py`、
`monitor/triggers.py`、`advice/engine.py` 是同一条：**按判定阶段切，不按行数切**。
这一段回答的是「今天该不该做、该做哪个方向」，它读采集那一段的产物，交出一张
`Fusion`；下一段（选股）只认这张单子，不认这里的任何中间量。

搬动不改判定顺序，一行判据都没动。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.broad_index import divergence_verdict, read_divergence
from ashare_decision_support.adapters.ths_sector_index import load_all_series
from ashare_decision_support.jobs.morning_facts import equal_weight_date
from ashare_decision_support.jobs.morning_stages import (
    Collected,
    Fusion,
    MorningConfig,
    Sources,
    Universe,
)
from ashare_decision_support.jobs.sources import (
    fetch_broad_index,
    fetch_earnings,
    fetch_limit_pool,
    fetch_market_scan,
    fetch_sector_index,
)
from ashare_decision_support.news import (
    DailyNewsReader,
    DailyRead,
    DocAttribution,
    LexiconSentiment,
    NewsCache,
    SocialSnapshotSource,
    ThemeOutcomeStore,
    read_macro_links,
    scan_themes,
)
from ashare_decision_support.news.dedup import dedup_key
from ashare_decision_support.news.llm_classifier import (
    ClassifyResult,
    LabelBackedSentiment,
    LlmNewsClassifier,
    build_client,
)
from ashare_decision_support.runhealth import STATUS_STALE, HealthLedger
from ashare_decision_support.sectors import (
    CONCEPT_LEVEL,
    SectorForecastOutcomeStore,
    SectorPredictor,
    SectorRotationStore,
    explain_today,
    read_rotation,
    read_seesaw,
    read_turnover,
)
from ashare_decision_support.strategy.positioning import resolve_position_sizing
from ashare_decision_support.strategy.regime import regime_from_limit_pool

# 持仓每只带几条自己的消息进通读。比板块归因那批（`SYMBOL_NEWS_LIMIT` 10 条）少，
# 是因为通读要的是「有没有属于它自己的事」，不是情绪聚合——条数少而精更好读。
MAX_HOLDING_HEADLINES = 5
ROTATION_LOOKBACK_DAYS = 5

# 送进**板块归因**的条数上限。取宽之后不能把多出来的全丢给分类器：它是按批计费的，
# 而多出来的那批绝大多数是海外与地缘消息，本来就归不到 A 股板块（模型对这类的正确
# 回答是空数组）。所以归因仍按原来的量级取最新的一批，通读拿全量——两层要的东西
# 本来就不同，共用一个语料但各取所需。
CLASSIFY_MAX_DOCS = 360


def stage_fuse(
    sources: Sources,
    cfg: MorningConfig,
    universe: Universe,
    collected: Collected,
    today: date,
    output_path: Path,
    notes: list[str],
    report: Callable[[str], Any],
) -> Fusion:
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

    # 全市场扫描：给候选提供可交易性门槛（流通市值/成交额），顺带一个比申万
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
    _persist_news_labels(collected.corpus, predictor, definitions, market_docs, labels, notes)
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

    sector_forecast_validation = SectorForecastOutcomeStore().record(
        today,
        [item for item in outlooks if item.level != CONCEPT_LEVEL],
        industry_metrics,
        data_date=date.fromisoformat(as_of) if as_of else today,
        top=max(cfg.top_sectors * 2, 5),
    )
    if not sector_forecast_validation["trusted"]:
        samples = next(
            item["samples"]
            for item in sector_forecast_validation["horizons"]
            if item["horizon"] == 5
        )
        notes.append(
            f"板块预测验证样本不足：5日结果 {samples}/"
            f"{sector_forecast_validation['minimum_calibration_samples']}，当前排名分数不是概率"
        )

    return Fusion(
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
        sector_forecast_validation=sector_forecast_validation,
        sector_news_memory=_sector_news_memory(collected.corpus, selected, notes=notes),
    )

# 选中板块各取几条跨日舆情记忆。上限压得低是刻意的：这一段是**记忆**不是清单，
# 一个板块超过三条就没人会读第三条以后的，而 facts 每多一条都要进写稿层 prompt。
SECTOR_MEMORY_HOURS = 48.0
SECTOR_MEMORY_HEADLINES = 3

def _persist_news_labels(
    corpus: Any,
    predictor: SectorPredictor,
    definitions: list,
    docs: list,
    labels: ClassifyResult,
    notes: list[str],
) -> None:
    """把「这条消息算在哪个板块头上」写进语料库。

    **不写的话，跨日热度永远查不到。** `doc_sectors` 此前在生产里是空表：入库
    （`ingest`）一直在跑，归因（`label`）一个生产调用方都没有，于是
    `sector_heat(sector=...)` / `recent(sector=...)` 按板块查出来的永远是空——
    一个建好、有测试、每天都在写入一半数据的表，另一半从来没写过。

    写的是**这一轮真正用于打分的那份归因**（`predictor.attribute_docs`），不是
    重新按词表匹配一遍：同一条规则有两份实现，它们会各自演化，而事后没人能分清
    「这条消息当时算在哪个板块」用的是哪一份。
    """
    if corpus is None or not getattr(corpus, "ok", False) or not docs:
        return
    docs_by_sector = predictor.attribute_docs(
        definitions, docs, labels.attribution(), fallback_empty_labels=True
    )
    model_labels = labels.labels if labels.ok else {}

    attribution: dict[str, DocAttribution] = {}
    for sector, matched in docs_by_sector.items():
        for doc in matched:
            key = dedup_key(doc)
            if not key:
                continue
            current = attribution.get(key)
            sectors = (*current.sectors, sector) if current else (sector,)
            model = model_labels.get(key)
            attribution[key] = DocAttribution(
                sectors=sectors,
                # 方向与事件类型只有模型给得出，词表那条路留空——留空是「没这个
                # 读数」，写一个猜的值是另一回事。
                direction=getattr(model, "direction", None),
                event_type=getattr(model, "event", None),
                mechanism=getattr(model, "why", None),
            )
    if not attribution:
        return
    written = corpus.label_many(attribution)
    notes.append(
        f"{written} 条舆情已按板块归因入库（{'大模型' if labels.ok else '关键词词典'}），"
        "跨日热度与「还在发酵的那条」按这份归因回查"
    )


def _sector_news_memory(
    corpus: Any,
    selected: list,
    now: Any = None,
    notes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """选中板块的**跨日**舆情记忆：新故事流入速度 + 仍在发酵的标题。

    当天那一批读数（`news_doc_count` / `headlines`）回答的是「今天有多少条」；
    语料库跨日累积的那一维此前没有任何读取点——于是「这个板块的消息是今天突然
    冒出来的，还是已经发酵两天」在事实包里没有答案，而这两种情况对第二天的含义
    完全不同。

    两个读数各答一件事，都只描述已经发生的事：

    - `fresh_curve` 数**第一次出现**的文档（`sector_heat`）。一条消息被四个源
      转发不代表热度翻四倍，那只是同一个故事。
    - `still_developing` 按 `last_seen` 取（`recent(by="last_seen")`）。一个两天
      前爆出、至今仍在被提的故事，正是现在推动板块的那个——按 `first_seen` 过滤
      会把它整个滤掉。

    语料库不可用（`--no-news`、库文件坏了）时返回空列表：这一层是旁证，不该让
    盘前主链路少一节结论。`now` 只有测试会传——窗口按**真实当下**切，盘前跑批
    在早上八点看的就该是最近 48 小时。
    """
    if corpus is None or not getattr(corpus, "ok", False):
        return []
    memory: list[dict[str, Any]] = []
    for outlook in selected:
        name = getattr(outlook, "name", "")
        if not name:
            continue
        curve = corpus.sector_heat(name, within_hours=SECTOR_MEMORY_HOURS, now=now)
        developing = corpus.recent(
            within_hours=SECTOR_MEMORY_HOURS,
            sector=name,
            limit=SECTOR_MEMORY_HEADLINES,
            by="last_seen",
            now=now,
        )
        if not curve and not developing:
            continue
        memory.append(
            {
                "sector": name,
                "level": getattr(outlook, "level", None),
                "window_hours": SECTOR_MEMORY_HOURS,
                # 时间桶 → 这一桶里第一次出现的文档数 / 覆盖到的源数。
                "fresh_curve": [
                    {
                        "bucket_start": point.bucket_start.isoformat(timespec="minutes"),
                        "fresh_docs": point.fresh_docs,
                        "sources": point.sources,
                    }
                    for point in curve
                ],
                "fresh_docs_total": sum(point.fresh_docs for point in curve),
                # 带 first_seen / last_seen 而不是算一个「多久之前」：后者要一个
                # 当下时钟，而这份 facts 会被落盘、被隔天读到——那时算出来的
                # 「3 小时前」就成了假的。两个时间戳原样给出，读的人自己减。
                "still_developing": [
                    {
                        "title": doc.title,
                        "source": doc.source,
                        "first_seen": doc.first_seen_at.isoformat(timespec="minutes"),
                        "last_seen": doc.last_seen_at.isoformat(timespec="minutes"),
                        "source_count": doc.source_count,
                    }
                    for doc in developing
                ],
            }
        )
    # 语料库读失败时返回的是空列表——那和「这几个板块最近确实没有新故事」长得
    # 一模一样，而后者会被当成一条读数用进判断。所以读不到必须说出口。
    read_error = getattr(corpus, "read_error", None)
    if read_error and notes is not None:
        notes.append(f"板块舆情记忆读取失败（{read_error}），本次事实包不含跨日热度")
    return memory

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

    持仓的消息单独取一次并带进去，让通读能点名回答「我这几只有没有自己的事」。
    这批标的通常只有几只，而它们恰恰是最需要逐只交代的——盘前候选是系统当天挑
    出来的，持仓是你已经承担了风险的。
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

    # 单独给持仓补一轮个股舆情：通读要回答的是「今天整体什么行情」，而你手上
    # 那几只的公告与新闻属于**必须被看到**的一类，不能只靠全市场快讯撞上。
    symbol_docs: dict[str, list] = {}
    names: dict[str, str | None] = {}
    try:
        from ashare_decision_support.state import Positions

        book = Positions()
        held = book.symbols()
        if held and source is not None and hasattr(source, "fetch_for_symbols"):
            symbol_docs, _ = source.fetch_for_symbols(held, limit=MAX_HOLDING_HEADLINES)
            names = {entry.symbol: entry.name for entry in book.entries}
    except Exception:
        symbol_docs, names = {}, {}

    result = reader.read(docs, today, symbol_docs=symbol_docs, names=names)
    notes.extend(result.notes)
    return result

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
    health = HealthLedger(today=today).stamped("板块轮动", stamp)
    if health.status == STATUS_STALE:
        notes.append(
            f"⚠️ 板块轮动读数截止 {stamp}，早于本次交易日 {today:%Y%m%d}——"
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
