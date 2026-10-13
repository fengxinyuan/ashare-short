"""盘前融合：舆情归因、大盘方向、板块排序与仓位约束。

读取 Collected 和 Universe，生成供选股与台账使用的 Fusion。
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
    fetch_overseas,
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
    LEVEL_CONCEPT,
    LEVEL_INDUSTRY,
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

# 限制每只持仓进入通读的标题数量。
MAX_HOLDING_HEADLINES = 5
ROTATION_LOOKBACK_DAYS = 5

# 分类按最新文档限制模型调用规模；通读使用更宽的语料。
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

    # 通读解释跨新闻传导链，不改变评分或候选排序。
    daily_read = _read_news(
        market_docs, today, output_path, cfg.use_llm_news, collected.source, notes
    )
    macro_links = read_macro_links(market_docs)
    notes.extend(macro_links.notes)
    theme_radar, theme_validation = _theme_radar(
        sources.social_source, today, market_docs, collected.metrics, notes
    )

    limit_snapshot = fetch_limit_pool(
        sources.limit_pool_adapter,
        today,
        enabled=cfg.use_limit_pool,
        notes=notes,
        off_note="已关闭涨停池读数（--no-limit-pool）",
    )

    # 情绪周期先约束打法和仓位，再进行选股。
    regime = regime_from_limit_pool(limit_snapshot)
    notes.extend(regime.reasons)
    notes.extend(regime.warnings)

    # 行业指数的价格位置与成分股聚合读数并列呈现。
    sector_index = fetch_sector_index(
        sources.sector_index_adapter, output_path, cfg.use_sector_index, notes
    )

    # 先刷新指数再读历史；关闭网络刷新时仍可读缓存，并保留截止日期。
    rotation_price, turnover_migration, seesaw = _rotation_from_series(today, notes)

    # 全市场扫描：给候选提供流通市值/成交额门槛，并统计全市场宽度。
    market_scan = fetch_market_scan(
        sources.market_scan_adapter, cfg.use_market_scan, notes, sources.adapter
    )

    earnings = fetch_earnings(sources.earnings_adapter, today, cfg.use_earnings, notes)

    # 外盘映射补充候选理由与风险，不参与排序。
    overseas = fetch_overseas(sources.global_market_adapter, notes)

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
    # 概念成分重叠，全市场宽度只使用行业指标，避免重复计权。
    industry_metrics = {
        item.name: collected.metrics[item.name]
        for item in definitions
        if item.level != LEVEL_CONCEPT and item.name in collected.metrics
    }
    market = predictor.market_outlook(market_docs, industry_metrics, limit_pool=limit_snapshot)
    notes.extend(market.warnings)

    # 宽基补充等权宽度的方向说明，不修改评分或仓位。
    broad_index, as_of, index_verdict = _index_context(sources, cfg, market, rotation_price, notes)

    # 仓位优先级与盘后共用：周期限制系统建议，手工值优先。
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
        LEVEL_INDUSTRY,
        [item for item in outlooks if item.level != LEVEL_CONCEPT],
        lookback_days=ROTATION_LOOKBACK_DAYS,
        leader_count=max(cfg.top_sectors, 1),
    )
    if rotation.available_days < 2:
        notes.append("板块轮动记忆样本不足，暂不对主线延续性下判断")
    else:
        notes.append(f"近 {rotation.available_days} 个交易样本里，板块轮动属于{rotation.regime}")

    sector_forecast_validation = SectorForecastOutcomeStore().record(
        today,
        [item for item in outlooks if item.level != LEVEL_CONCEPT],
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
        overseas=overseas,
    )


def _index_context(
    sources: Sources,
    cfg: MorningConfig,
    market: Any,
    rotation_price: Any,
    notes: list[str],
) -> tuple[Any, str | None, Any]:
    """宽基指数快照与等权/加权背离判定。只往 `market` 的 reasons/warnings 追加，不改 score。"""
    broad_index = fetch_broad_index(sources.broad_index_adapter, cfg.use_broad_index, notes)
    as_of = equal_weight_date(rotation_price)
    index_reasons, index_warnings = read_divergence(
        broad_index, market.breadth, expected_date=as_of
    )
    index_verdict = divergence_verdict(broad_index, market.breadth, as_of)
    market.reasons.extend(index_reasons)
    market.warnings.extend(index_warnings)
    notes.extend(index_warnings)
    return broad_index, as_of, index_verdict


# 限制跨日记忆的时间窗与每板块标题数，控制事实包大小。
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
    """将本轮评分采用的板块归因写入语料库，供跨日热度与原文检索复用。"""
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
    """读取选中板块的跨日舆情。

    fresh_curve 按 first_seen 统计新文档，still_developing 按 last_seen 保留持续传播的标题。
    重复转发不增加新故事数量。
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
                # 保留绝对时间戳，避免跨日读取时相对时间失真。
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
        notes.append(
            "主题雷达：" + "；".join(f"{item.name}（{item.stage}）" for item in signals[:5])
        )
    return signals, validation


def _rotation_from_series(today: date, notes: list[str]) -> tuple[Any, Any, Any]:
    """共用一次历史读取，计算价格轮动、成交额迁移和板块反向关系。

    仅从磁盘读取已保存日线，独立于网络刷新开关；结果标明数据日期与覆盖限制。
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

    # 反向关系需要更长历史；证据不足时仍返回对象以保留原因。
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
    elif health.degraded:
        notes.append(f"{health.describe()}，本次不使用该轮动、成交额和跷跷板读数")
        return None, None, None
    notes.extend(read.observations)
    if turnover.ok:
        notes.extend(turnover.observations)
    notes.extend(seesaw.observations)
    # 仅对照今日变化与历史关系，不外推下一交易日。
    notes.extend(explain_today(seesaw))
    return read, (turnover if turnover.ok else None), seesaw


def _select_sectors(outlooks: list, cfg: MorningConfig, notes: list[str]) -> list:
    """从可信板块中分别取行业与概念配额；未指定 level 的板块按行业处理。"""
    trusted = [item for item in outlooks if item.trusted]
    concepts = [item for item in trusted if item.level == LEVEL_CONCEPT]
    industries = [item for item in trusted if item.level != LEVEL_CONCEPT]

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
