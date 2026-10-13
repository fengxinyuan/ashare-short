"""把盘前读数组装成事实包与台账市场快照。

build_facts 的字段由写稿、问答和事实指纹消费；market_state 保留判断时的分层维度。
缺失值保留 None，分别表示未取得事实或缺少统计维度，不补成中性读数。
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from ashare_decision_support.sectors.clusters import cluster_ranked_sectors
from ashare_decision_support.signals.series import bar_date, parse_bar_date
from ashare_decision_support.strategy.regime import RegimeVerdict


def metric_bar_freshness(bars_by_symbol: dict, expected: date) -> tuple[str | None, float]:
    """Return the modal constituent bar date and today's share of dated symbols."""
    latest = [day for rows in bars_by_symbol.values() if rows if (day := bar_date(rows[-1]))]
    if not latest:
        return None, 0.0
    modal = Counter(latest).most_common(1)[0][0]
    return modal.isoformat(), sum(day == expected for day in latest) / len(latest)


def equal_weight_date(rotation_price: Any) -> str | None:
    """以板块指数序列日期作为等权日线的比较基准，缺失时返回 None。

    返回 YYYY-MM-DD，供宽基快照检查跨日差异。
    """
    day = parse_bar_date(getattr(rotation_price, "trade_date", None))
    return day.isoformat() if day is not None else None


def market_state(
    market: Any,
    position_factor: float | None,
    limit_pool: Any,
    regime: RegimeVerdict | None = None,
    rotation_price: Any = None,
    turnover: Any = None,
    broad_index: Any = None,
    index_verdict: str | None = None,
    sizing: Any = None,
) -> dict[str, Any]:
    """将判断时的市场读数保存在每条台账中，支持独立回放和分层统计。"""
    ok = limit_pool is not None and getattr(limit_pool, "ok", False)
    rotation_ok = rotation_price is not None and getattr(rotation_price, "ok", False)
    turnover_ok = turnover is not None and getattr(turnover, "ok", False)
    index_ok = broad_index is not None and getattr(broad_index, "ok", False)
    return {
        "direction": getattr(market, "direction", None),
        "score": getattr(market, "score", None),
        "breadth": getattr(market, "breadth", None),
        "position_factor": position_factor,
        # 分别记录原始建议、周期约束结果和手工值，供复盘判断约束是否生效。
        "suggested_factor": getattr(sizing, "suggested_factor", None),
        "capped_suggested_factor": getattr(sizing, "capped_suggested_factor", None),
        "manual_factor": getattr(sizing, "manual_factor", None),
        # 情绪周期位置的三个读数。炸板率说今天封板质量，连板高度说有没有资金
        # 愿意接力，跌停家数说恐慌程度。
        "blowup_rate": getattr(limit_pool, "blowup_rate", None) if ok else None,
        "max_streak": getattr(limit_pool, "max_streak", None) if ok else None,
        "limit_up_count": getattr(limit_pool, "limit_up_count", None) if ok else None,
        "limit_down_count": getattr(limit_pool, "limit_down_count", None) if ok else None,
        # 涨停池的数据日期可能早于请求日期（服务端对非交易日返回最近交易日）。
        "limit_pool_date": str(getattr(limit_pool, "trade_date", "")) or None if ok else None,
        # 保存当时的周期标签，避免阈值调整改变历史分组。
        "regime": regime.cycle.value if regime is not None else None,
        "regime_position_cap": regime.position_cap if regime is not None else None,
        "regime_trusted": regime.trusted if regime is not None else None,
        # 研究读数用于台账分层；轮动的两组窗口分别记录。
        "rotation_switch": getattr(rotation_price, "switch_label", None) if rotation_ok else None,
        "rotation_adjacent": getattr(rotation_price, "adjacent_label", None)
        if rotation_ok
        else None,
        "rotation_persistence": getattr(rotation_price, "persistence", None)
        if rotation_ok
        else None,
        # 保存序列日期，区分当前读数和沿用历史缓存。
        "rotation_date": str(getattr(rotation_price, "trade_date", "")) or None
        if rotation_ok
        else None,
        # 全场成交额变化用于区分总量变化与板块间占比迁移。
        "turnover_total_change": getattr(turnover, "total_change", None) if turnover_ok else None,
        # 窗口内走向与窗口均值变化分别保存，拐点时两者可能异号。
        "turnover_total_trend": getattr(turnover, "total_trend", None) if turnover_ok else None,
        # 与等权 breadth 并存，供复盘比较两种口径。
        "index_median_ret": getattr(broad_index, "median_ret", None) if index_ok else None,
        "index_date": getattr(broad_index, "trade_date", None) if index_ok else None,
        # 沿用 divergence_verdict 的当日标签，不按新阈值重算历史。
        "index_divergence": index_verdict,
    }


def build_facts(
    today: date,
    outlooks: list,
    candidates: list[dict],
    market,
    position_factor: float,
    notes: list[str],
    news_as_of: str | None = None,
    limit_pool: Any = None,
    sector_index: Any = None,
    daily_read: Any = None,
    macro_links: Any = None,
    rotation: Any = None,
    rotation_price: Any = None,
    earnings: Any = None,
    turnover_migration: Any = None,
    seesaw: Any = None,
    market_source_counts: dict[str, Any] | None = None,
    news_coverage: dict[str, int] | None = None,
    news_targets: list[Any] | None = None,
    news_diagnostics: dict[str, Any] | None = None,
    theme_radar: list[Any] | None = None,
    theme_validation: dict[str, Any] | None = None,
    sector_forecast_validation: dict[str, Any] | None = None,
    sector_news_memory: list[dict[str, Any]] | None = None,
    overseas: Any = None,
    *,
    metric_date: str | None,
) -> dict[str, Any]:
    return {
        "trade_date": str(today),
        # 个股行情日期来自成分股日线，用于时效检查，独立于计划日和板块指数日期。
        "data_date": metric_date,
        "news_as_of": news_as_of,
        "news_coverage": {
            "sources": market_source_counts or {},
            "sectors": news_coverage or {},
            "diagnostics": news_diagnostics or {},
            "targets": [
                target.to_dict() if hasattr(target, "to_dict") else target
                for target in (news_targets or [])
            ],
        },
        # 价格轮动与系统排名记忆分别输出，保留各自的输入与起算日期。
        "rotation_price": (
            rotation_price.to_dict()
            if rotation_price is not None and getattr(rotation_price, "ok", False)
            else None
        ),
        # 与价格轮动同源，但单独保留成交额口径。
        "turnover_migration": (
            turnover_migration.to_dict()
            if turnover_migration is not None and getattr(turnover_migration, "ok", False)
            else None
        ),
        # 即使证据不足也保留反向关系读数及原因，区分未发现关系和无法计算。
        "seesaw": seesaw.to_dict() if seesaw is not None else None,
        # 业绩层。只带聚合读数与被否决/被标注的名单，不带全市场那 1874 家——
        # facts 要进写稿层的 prompt，塞全量会把上下文挤爆而没有额外信息。
        "earnings": (
            {
                "period": earnings.period,
                "disclosed": len(earnings),
                "notes": list(earnings.notes),
                "summary": earnings.describe(),
            }
            if earnings is not None and getattr(earnings, "ok", False)
            else None
        ),
        # 通读的结论。放进 facts 是为了让写稿层能读到它并组织语言；每条都带
        # `evidence` 原文，所以写稿层引用它不会引入新数字。
        "news_read": (
            daily_read.to_dict()
            if daily_read is not None and getattr(daily_read, "ok", False)
            else None
        ),
        # 跨日记忆保留消息首次出现与持续传播情况。
        "sector_news_memory": sector_news_memory or [],
        "theme_radar": [item.to_dict() for item in (theme_radar or [])],
        # 外盘 → 板块映射。候选行上已各自挂了指向本板块的那几条；这里是全量，
        # 写稿层与外部 AI 据此回答「今天哪些板块有隔夜外盘背景」。
        "overseas_links": overseas.to_dict() if overseas is not None else None,
        "theme_validation": theme_validation or {},
        "sector_forecast_validation": sector_forecast_validation or {},
        "sector_clusters": cluster_ranked_sectors(outlooks),
        "macro_links": (
            macro_links.to_dict()
            if macro_links is not None and getattr(macro_links, "ok", False)
            else None
        ),
        "sector_index": (
            {
                "source": "同花顺行业指数",
                "trade_date": sector_index.readings[0].trade_date,
                "sectors": len(sector_index.readings),
                # 指数涨跌与成分股中位涨跌保留各自口径；指数额外提供日内收位。
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
            if sector_index is not None
            and getattr(sector_index, "ok", False)
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
        "rotation": rotation.to_dict()
        if rotation is not None and getattr(rotation, "ok", False)
        else None,
        "position_factor": position_factor,
        "suggested_position_factor": market.suggested_position_factor,
        "position_factor_reasons": market.sentiment.reasons,
        "sectors": [
            {
                "name": item.name,
                "level": item.level,
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
                "news_doc_count": item.news_doc_count,
                "news_event_types": list(item.news_event_types),
                "news_hits": list(item.news_hits),
                "news_risk_hits": list(item.news_risk_hits),
                "reasons": item.reasons,
                "warnings": item.warnings,
                "headlines": item.headlines,
            }
            for item in outlooks
        ],
        "candidates": candidates,
        "data_notes": notes,
    }


def news_source_counts(docs: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for doc in docs:
        source = str(getattr(doc, "source", "") or "unknown").strip()
        counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items()))
