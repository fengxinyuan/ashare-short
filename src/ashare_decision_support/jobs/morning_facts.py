"""盘前跑批的**产物组装**：把算完的一堆读数摊成 facts / market_state 两份字典。

从 `jobs/morning.py` 里搬出来的，判据是一条：**这一层不做任何判断**。它是纯映射
——每一行都是「某个读数存在就带上，不存在就写 None」，没有一处阈值、没有一处
分支会改变结论。而它有 300 多行，夹在六段判定链路中间，读主链路的人要连着翻过
一整屏字典字面量才能看到下一个真正在做决定的地方。

搬出来之后 `morning.py` 少了约 310 行，而这一层的契约反而清楚了：

- **`facts()`** —— 进写稿层 prompt、进 `ask` / `chat` 上下文、进 `facts_hash`。
  它的每一个键都会被外部消费，所以**加键容易删键难**：`facts_hash` 是台账里
  「当时到底看到了什么」的指纹，键集变了，新旧记录的指纹就不可比。
- **`market_state()`** —— 只进台账。它回答的是「这条建议发出时市场什么样」，
  存在的理由是让几个月后的统计能按情绪周期分层（见 `journal/records.py`）。

两份分开而不是合并，因为**缺失语义不同**：facts 里一个 None 是「今天这一路没
取到」，写稿层据此不提它；market_state 里一个 None 是「这条记录没有这个分层
维度」，统计层据此把它排除出该分组，而不是当成一个中性值算进去。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ashare_decision_support.strategy.regime import RegimeVerdict


def equal_weight_date(rotation_price: Any) -> str | None:
    """等权那一路的数据日期，`YYYY-MM-DD`；判定不出来就返回 None。

    板块动量本身不带 as-of，但它和板块指数序列取的是同一个「最后一个完整交易日」
    的日线，所以用序列的 `trade_date` 当基准。它只用来和宽基的日期比对——宽基走
    实时行情，盘中跑会取到当日的，那时两边不是同一天。

    取不到就 None，`read_divergence` 会因此只报事实、不下背离判断。宁可少一条
    结论，也不要拿今天的指数去和昨天的宽度比。
    """
    stamp = str(getattr(rotation_price, "trade_date", "") or "")
    if len(stamp) == 8 and stamp.isdigit():
        return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
    return None


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
    """建议发出当时的大盘快照，原样存进每一条台账记录。

    冗余存储是刻意的：台账是 JSONL，每条多带七八个数比事后按日期 join 便宜得多，
    而且**记录自带分层维度**意味着任何一段区间的数据都能独立分析，不依赖别的文件
    还在不在。

    这里的每个字段都对应一个「超短线该不该出手」的判断维度，缺了就没法回答
    「这套规则在退潮期到底管不管用」。
    """
    ok = limit_pool is not None and getattr(limit_pool, "ok", False)
    rotation_ok = rotation_price is not None and getattr(rotation_price, "ok", False)
    turnover_ok = turnover is not None and getattr(turnover, "ok", False)
    index_ok = broad_index is not None and getattr(broad_index, "ok", False)
    return {
        "direction": getattr(market, "direction", None),
        "score": getattr(market, "score", None),
        "breadth": getattr(market, "breadth", None),
        "position_factor": position_factor,
        # 仓位是**怎么得到**这个数的，不只是这个数本身。三条记录下来，事后才分得开
        # 三种截然不同的决策——它们的 `position_factor` 可以完全一样：
        #
        #   系统建议 0.5、上限 0.5        → 规则没起作用，是系统自己就保守
        #   系统建议 0.9、被上限压到 0.5  → **上限救了这一次**
        #   系统建议 0.5、手工设成 0.5    → 人接管了，规则只是恰好同值
        #
        # 只存最终值的话，「仓位上限这条规则到底有没有用」永远没有样本，而那是
        # 仓位这一层唯一能被 `review` 验证的方式。
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
        # 情绪周期的判定结论。存标签而不是只存原始读数，是为了让统计分组与当天
        # 实际生效的闸门**用同一个判定**——否则「退潮期这套规则管不管用」的样本
        # 会和真正被闸门拦下的日子对不上，闸门就永远无法被自己的台账验证。
        "regime": regime.cycle.value if regime is not None else None,
        "regime_position_cap": regime.position_cap if regime is not None else None,
        "regime_trusted": regime.trusted if regime is not None else None,
        # ---- 以下三层是「只出读数、不进打分」的那批，存进台账是它们**唯一**能
        # 被验证的途径。不存的话它们永远停在读数状态：`review` 无法回答「高低切换
        # 的日子里这套筛选的兑现率有没有差别」，于是也永远谈不上要不要给它们权重。
        # 存的成本是每条记录多七个数，与上面那批同一条理由（见本函数开头）。
        #
        # 轮动的两个标签都存：它们用的窗口不同，2026-08-03 实测一个「强者恒强」
        # 一个「高低切换」。只存一个的话，事后分组会把两种截然不同的盘面混进同一组。
        "rotation_switch": getattr(rotation_price, "switch_label", None) if rotation_ok else None,
        "rotation_adjacent": getattr(rotation_price, "adjacent_label", None)
        if rotation_ok
        else None,
        "rotation_persistence": getattr(rotation_price, "persistence", None)
        if rotation_ok
        else None,
        # 板块指数序列的交易日。可能早于建议发出的日期（跳过取数时读的是上一次
        # 落盘的），不存的话事后分不清「那天没切换」和「那天读的是前天的数」。
        "rotation_date": str(getattr(rotation_price, "trade_date", "")) or None
        if rotation_ok
        else None,
        # 全场成交额的相对变化。存量腾挪与增量入场下的同一个「高低切换」，对仓位
        # 的含义相反——这是把两者分开的那个字段。
        "turnover_total_change": getattr(turnover, "total_change", None) if turnover_ok else None,
        # 近端窗口**内部**的量能走向。必须和上面那个一起存：块均值对比在 V 形拐点
        # 上会指反（2026-08-07 实测 -1.8% 对 +33.4%），只存前者的话，事后分组会把
        # 「量能刚触底回升」的那几天归进「存量博弈」，而那正是两者含义相反的日子。
        "turnover_total_trend": getattr(turnover, "total_trend", None) if turnover_ok else None,
        # 市值加权口径。`breadth` 是等权的，两者背离时方向读数会系统性偏乐观，
        # 而只有同时存下来，事后才能把「等权强、加权弱」的日子单独拎出来看。
        "index_median_ret": getattr(broad_index, "median_ret", None) if index_ok else None,
        "index_date": getattr(broad_index, "trade_date", None) if index_ok else None,
        # 背离的**判定标签**，不是让统计层拿上面两个数现算。判据的单一真源在
        # `adapters/broad_index.py:divergence_verdict`；阈值以后一定会调，而调完
        # 之后历史记录必须仍按当天生效的那个判定分组，否则分层统计描述的是一个
        # 从未存在过的过去。同一条理由见 `journal/stats.py:_regime_bucket`。
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
) -> dict[str, Any]:
    return {
        "trade_date": str(today),
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
        # 价格序列口径的板块轮动。与下面的 `rotation`（系统排名的记忆）并列而不是
        # 合并：两者的输入和起算日都不同，混成一个字段会让写稿层分不清「这个结论
        # 是从指数价格算的还是从我自己的排名算的」。
        "rotation_price": (
            rotation_price.to_dict()
            if rotation_price is not None and getattr(rotation_price, "ok", False)
            else None
        ),
        # 成交额迁移。与 `rotation_price` 同源（同一批板块指数序列）但口径不同：
        # 那个用 close 回答「谁在涨」，这个用 amount 回答「谁在被交易、以及那是
        # 在买还是在跑」。分成两个字段而不是合并，理由和上面一样——写稿层必须
        # 能分清一个结论是从价格算的还是从成交额算的。
        "turnover_migration": (
            turnover_migration.to_dict()
            if turnover_migration is not None and getattr(turnover_migration, "ok", False)
            else None
        ),
        # 跷跷板。第三个从同一批序列算出来的读数，回答的是**两两之间**的关系，
        # 而上面两个都是「全场一个数」的横截面读数——「今天银行涨、电子跌」这种
        # 分歧，只有这一层答得了「这是结构性的还是偶然的」。
        #
        # **`trusted` 为假时也带出去**，与上面两个的处置刻意不同：那两个用 `ok`
        # 过滤是因为算不出来时它们没什么可说的；这一层算不出来时 `notes` 里写着
        # 原因（序列不够 61 根、板块数不够），而「没有跷跷板」本身是一条结论，
        # 空 `pairs` 加一句 observation 正是它的表达方式。
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
        "theme_radar": [item.to_dict() for item in (theme_radar or [])],
        "theme_validation": theme_validation or {},
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
