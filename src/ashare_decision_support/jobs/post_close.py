"""盘后：给传入的标的（省略即持仓）打分，产出次日执行卡和过程复盘。

与盘前共用 `strategy/select.py`。打法门槛只标注、不剔除用户指定标的，
没有可用打法时仍保留分析并说明原因。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.base import (
    apply_security_metadata_rows,
    fetch_daily_bars,
    float_shares,
    metadata_names,
    security_metadata,
)
from ashare_decision_support.adapters.broad_index import default_broad_index_adapter
from ashare_decision_support.adapters.global_market import default_global_market_adapter
from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.artifacts import (
    MORNING_LATEST_NAME,
    POST_CLOSE_LATEST_NAME,
    SNAPSHOT_LATEST_NAME,
)
from ashare_decision_support.jobs.daily_review import run_daily_review
from ashare_decision_support.jobs.morning_facts import market_state
from ashare_decision_support.jobs.morning_record import record_candidate_rows
from ashare_decision_support.jobs.sources import (
    fetch_earnings,
    fetch_lhb_seats,
    fetch_limit_pool,
    fetch_trade_flow,
)
from ashare_decision_support.journal import Journal
from ashare_decision_support.journal.records import DEFAULT_STRATEGY_VERSION
from ashare_decision_support.news.macro_links import read_macro_links
from ashare_decision_support.news.wallstreet_source import WallstreetNewsSource
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier, render_post_close_brief
from ashare_decision_support.notify.base import Notifier, publish, written_path
from ashare_decision_support.research.daily_review import render_next_checks
from ashare_decision_support.research.sector_confirmation import build_sector_confirmation
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.scoring import (
    RuleBasedScoreProvider,
    ScoreProvider,
    build_factor_contexts,
)
from ashare_decision_support.scoring.liquidity_factor import calculate_liquidity_factor
from ashare_decision_support.screens import EarningsScreen
from ashare_decision_support.sectors.ladder_cycle import analyze_ladder_and_cycle
from ashare_decision_support.signals.series import bar_date
from ashare_decision_support.strategy.positioning import resolve_position_sizing
from ashare_decision_support.strategy.regime import regime_from_limit_pool
from ashare_decision_support.strategy.select import evaluate_universe
from ashare_decision_support.symbols import normalize_symbol

# 台账里这条链路的来源标签。和 `jobs/advise.py:ADVISE_SOURCE` 同一条理由：
# `journal/stats.py:_screened` 只拿晨报那条链路算入选/对照差值，盘后卡的标的是
# 人手点名的持仓与自选，从没进过板块排序，混进去会把那个差值稀释成谁都不像的数。
POST_CLOSE_SOURCE = "post_close"


def _calculate_sector_funds(limit_snapshot: Any) -> list[dict[str, Any]]:
    if not limit_snapshot or not getattr(limit_snapshot, "limit_up", None):
        return []
    sector_funds: dict[str, float] = {}
    for s in limit_snapshot.limit_up:
        sec = getattr(s, "sector", None) or "其他"
        amt = (getattr(s, "amount", 0.0) or 0.0) + (getattr(s, "seal_fund", 0.0) or 0.0)
        sector_funds[sec] = sector_funds.get(sec, 0.0) + amt
    return [
        {"sector": sec, "fund_yi": round(amt / 1e8, 2)}
        for sec, amt in sorted(sector_funds.items(), key=lambda kv: -kv[1])[:5]
        if amt > 0
    ]


def _extract_ladder_and_liquidity(
    limit_snapshot: Any,
    broad_adapter: Any | None = None,
    enabled: bool = True,
) -> tuple[
    dict[str, Any] | None, dict[str, Any], list[dict[str, Any]], list[str], dict[str, Any] | None
]:
    notes: list[str] = []
    broad_read = None
    if enabled:
        source = broad_adapter if broad_adapter is not None else default_broad_index_adapter()
        if source is not None:
            try:
                broad_read = source.fetch()
            except Exception as exc:
                notes.append(f"宽基指数不可用（{type(exc).__name__}）")
    ladder_dict: dict[str, Any] | None = None
    if limit_snapshot and limit_snapshot.limit_up:
        ladder_report = analyze_ladder_and_cycle(today=limit_snapshot)
        notes.extend(ladder_report.notes)
        ladder_dict = {
            "max_streak": ladder_report.max_streak,
            "has_fault_line": ladder_report.has_fault_line,
            "cycle_stage": ladder_report.cycle_stage,
            "emotion_score": ladder_report.emotion_score,
            "action_advice": ladder_report.action_advice,
            "top_sectors_by_funds": _calculate_sector_funds(limit_snapshot),
            "tiers": [
                {
                    "streak": t.streak,
                    "count": t.count,
                    "symbols": list(t.symbols),
                    "names": list(t.names),
                }
                for t in ladder_report.tiers
            ],
            "progressions": [
                {
                    "from_streak": p.from_streak,
                    "to_streak": p.to_streak,
                    "yesterday_count": p.yesterday_count,
                    "today_count": p.today_count,
                    "rate": p.rate,
                }
                for p in ladder_report.progressions
            ],
        }

    market_turnover_yi: float | None = None
    indices_summary = None
    if broad_read and broad_read.ok:
        sh_amt = next(
            (q.amount for q in broad_read.quotes if q.code == "sh000001" and q.amount), 0.0
        )
        sz_amt = next(
            (q.amount for q in broad_read.quotes if q.code == "sz399001" and q.amount), 0.0
        )
        if sh_amt or sz_amt:
            market_turnover_yi = round((sh_amt + sz_amt) / 1e8, 1)
        indices_summary = {
            "summary": broad_read.summary(),
            "turnover_yi": market_turnover_yi,
            "quotes": [q.to_dict() for q in broad_read.quotes if q.ret is not None],
        }

        sh_ret = next(
            (q.ret for q in broad_read.quotes if q.code == "sh000001" and q.ret is not None),
            None,
        )
    else:
        sh_ret = None

    liquidity_report = calculate_liquidity_factor(market_turnover_yi, index_ret=sh_ret)
    notes.extend(liquidity_report.notes)
    liquidity_dict = {
        "turnover_yi": liquidity_report.turnover_yi,
        "regime": liquidity_report.regime,
        "scaling_factor": liquidity_report.scaling_factor,
        "defense_bias": liquidity_report.defense_bias,
        "position_cap_mult": liquidity_report.position_cap_mult,
    }

    top_stocks = (
        [
            {
                "symbol": s.symbol,
                "name": s.name,
                "streak": s.streak,
                "seal_fund": round((s.seal_fund or 0) / 1e8, 2),
                "first_seal": s.first_seal,
                "sector": s.sector,
            }
            for s in sorted(
                limit_snapshot.limit_up, key=lambda x: (x.streak, x.seal_fund or 0), reverse=True
            )[:10]
        ]
        if limit_snapshot and limit_snapshot.limit_up
        else []
    )

    return ladder_dict, liquidity_dict, top_stocks, notes, indices_summary


def _build_global_insights(
    a50: dict[str, Any] | None,
    nq_fut: dict[str, Any] | None,
    usdcnh: dict[str, Any] | None,
    wti: dict[str, Any] | None,
    gold: dict[str, Any] | None,
) -> list[str]:
    insights: list[str] = []
    if a50 and a50.get("change_pct") is not None:
        pct = a50["change_pct"]
        if pct <= -0.8:
            insights.append(
                f"富时A50期指走弱（{pct:+.2f}%），外资承接谨慎，次日早盘权重蓝筹集合竞价面临低开下探压力，防范早盘惯性杀跌。"
            )
        elif pct >= 0.8:
            insights.append(
                f"富时A50期指盘后走强（{pct:+.2f}%），外资风险偏好抬升，有助于次日早盘核心资产企稳或高开冲高。"
            )
        else:
            insights.append(
                f"A50期指盘后偏稳（{pct:+.2f}%），结合日内放量杀跌，次日早盘大概率经历平开或微幅低开后的弱势震荡筑底。"
            )

    if nq_fut and nq_fut.get("change_pct") is not None:
        pct = nq_fut["change_pct"]
        if pct <= -0.8:
            insights.append(
                f"纳指期货下挫（{pct:+.2f}%），海外科技成长风险偏好承压，映射次日A股算力与芯片题材需谨防开盘分歧。"
            )
        elif pct >= 0.8:
            insights.append(
                f"纳指期货走强（{pct:+.2f}%），海外科技溢价向好，映射次日A股半导体与AI成长板块具备开盘共振动能。"
            )
        else:
            insights.append(
                f"美股科技期指微幅震荡（纳指期指 {pct:+.2f}%），外部扰动较小，科技主线回归国内自主题材与业绩逻辑。"
            )

    if usdcnh and usdcnh.get("change_pct") is not None:
        pct, last = usdcnh["change_pct"], usdcnh["last"]
        if pct >= 0.15:
            insights.append(
                f"离岸人民币小幅贬值（现价 {last}，{pct:+.2f}%），警惕北向资金流出对两市成交额的边际压制。"
            )
        elif pct <= -0.15:
            insights.append(
                f"离岸人民币升值（现价 {last}，{pct:+.2f}%），本币流动性预期改善，利好核心资产估值修复。"
            )
        else:
            insights.append(
                f"离岸人民币汇率平稳（现价 {last}），外资流动性预期稳定，未见汇率端系统性利空。"
            )

    insights.extend(_commodity_insight(wti, gold))
    return insights


def _commodity_insight(wti: dict[str, Any] | None, gold: dict[str, Any] | None) -> list[str]:
    res: list[str] = []
    if wti and wti.get("change_pct") is not None and abs(wti["change_pct"]) >= 1.2:
        direction = "大涨" if wti["change_pct"] > 0 else "下挫"
        res.append(
            f"WTI国际原油{direction}（{wti['change_pct']:+.2f}%），次日石油石化、油运及能化板块面临输入性价格脉冲。"
        )
    if gold and gold.get("change_pct") is not None and abs(gold["change_pct"]) >= 0.8:
        res.append(
            f"COMEX黄金异动（{gold['change_pct']:+.2f}%），关注贵金属与防御避险板块日内对冲动能。"
        )
    return res


def default_macro_news_source() -> Any | None:
    return WallstreetNewsSource(lookback_hours=36, max_pages=2)


def _filter_macro_headlines(docs: list[Any]) -> list[dict[str, str]]:
    categories = {
        "rates_fed": (
            "美联储",
            "降息",
            "加息",
            "鲍威尔",
            "缩表",
            "利率决议",
            "欧洲央行",
            "日本央行",
        ),
        "inflation_cpi": ("CPI", "PPI", "通胀", "非农", "PCE", "失业率", "超预期"),
        "china_liquidity": (
            "央行",
            "买断式逆回购",
            "逆回购",
            "降准",
            "MLF",
            "社融",
            "特别国债",
            "财政",
            "稳增长",
        ),
        "geopolitics": ("关税", "制裁", "出口管制", "实体清单", "中美", "中东"),
    }
    headlines: list[dict[str, str]] = []
    seen: set[str] = set()
    for doc in docs:
        txt = f"{doc.title} {getattr(doc, 'text', '') or ''}"
        for cat, kws in categories.items():
            if any(kw in txt for kw in kws):
                title_clean = doc.title.strip().replace("\n", " ")
                if title_clean not in seen:
                    seen.add(title_clean)
                    headlines.append(
                        {
                            "category": cat,
                            "title": title_clean,
                            "published_at": str(getattr(doc, "published_at", "") or ""),
                        }
                    )
                break
        if len(headlines) >= 5:
            break
    return headlines


def _extract_macro_context(
    source: Any | None = None,
    enabled: bool = True,
    trade_date: date | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not enabled:
        return None, []
    news_source = source if source is not None else default_macro_news_source()
    if news_source is None:
        return None, []

    notes: list[str] = []
    try:
        tdate = trade_date or beijing_today()
        docs, fetch_notes = news_source.fetch_market(tdate, limit=60)
        notes.extend(fetch_notes)
        if not docs:
            return None, notes

        digest = read_macro_links(docs)
        headlines = _filter_macro_headlines(docs)
        transmissions = [
            f"{item.trigger}（传导：{'/'.join(item.targets)}，方向：{'利好' if item.direction == 'bullish' else '利空'}）"
            for item in digest.links[:4]
        ]
        macro_dict = {
            "headlines": headlines,
            "transmissions": transmissions,
            "coverage": digest.coverage,
        }
        return macro_dict, notes
    except Exception as exc:
        notes.append(f"获取宏观要闻异常跳过：{type(exc).__name__}")
        return None, notes


def _extract_global_market(
    global_adapter: Any | None = None,
    enabled: bool = True,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not enabled:
        return None, []
    source = global_adapter if global_adapter is not None else default_global_market_adapter()
    if source is None:
        return None, []
    notes: list[str] = []
    try:
        snap = source.fetch_snapshot()
        if not snap.ok:
            return None, ["外盘实时快照暂未获取到有效数据"]

        reading_map = {r.key: r for r in snap.readings}

        def _get_item(key: str) -> dict[str, Any] | None:
            r = reading_map.get(key)
            if not r or r.last is None:
                return None
            return {
                "key": r.key,
                "name": r.name,
                "last": r.last,
                "change_pct": r.change_pct,
                "as_of": r.as_of,
            }

        a50, nq_fut, es_fut = _get_item("a50"), _get_item("nq_future"), _get_item("es_future")
        hsi, kospi, nikkei = _get_item("hsi"), _get_item("kospi"), _get_item("nikkei")
        wti, brent, gold = _get_item("wti"), _get_item("brent"), _get_item("gold")
        usdcnh, dxy, sox = _get_item("usdcnh"), _get_item("dxy"), _get_item("sox")

        insights = _build_global_insights(a50, nq_fut, usdcnh, wti, gold)

        global_dict = {
            "a50": a50,
            "nq_future": nq_fut,
            "es_future": es_fut,
            "hsi": hsi,
            "kospi": kospi,
            "nikkei": nikkei,
            "wti": wti,
            "brent": brent,
            "gold": gold,
            "usdcnh": usdcnh,
            "dxy": dxy,
            "sox": sox,
            "insights": insights,
        }
        return global_dict, notes
    except Exception as exc:
        notes.append(f"获取外盘快照异常跳过：{type(exc).__name__}")
        return None, notes


def run_post_close(
    adapter: Any,
    symbols: list[str],
    output_dir: str | Path,
    days: int = 180,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    position_factor: float | None = None,
    score_provider: ScoreProvider | None = None,
    notifiers: list[Notifier] | None = None,
    top_n: int | None = None,
    debate: DebatePanel | None = None,
    trade_date: date | None = None,
    limit_pool_adapter: Any | None = None,
    use_limit_pool: bool = True,
    detail_adapter: Any | None = None,
    security_master_adapter: Any | None = None,
    use_trade_flow: bool = True,
    lhb_seat_adapter: Any | None = None,
    use_lhb_seats: bool = True,
    journal: Journal | None = None,
    broad_index_adapter: Any | None = None,
    use_broad_index: bool = True,
    global_market_adapter: Any | None = None,
    use_global_market: bool = True,
    macro_news_source: Any | None = None,
    use_macro_news: bool = True,
    earnings_adapter: Any | None = None,
    use_earnings: bool = True,
) -> dict[str, Any]:
    provider = score_provider or RuleBasedScoreProvider()
    end = trade_date or beijing_today()
    start = end - timedelta(days=max(days, 1))

    data_notes: list[str] = []

    limit_snapshot = fetch_limit_pool(
        limit_pool_adapter,
        end,
        enabled=use_limit_pool,
        notes=data_notes,
        off_note="已关闭涨停池读数，本次无情绪周期判定",
    )
    regime = regime_from_limit_pool(limit_snapshot)
    data_notes.extend([*regime.reasons, *regime.warnings])

    earnings = fetch_earnings(
        earnings_adapter,
        end,
        enabled=use_earnings if earnings_adapter is not None else (use_earnings and use_limit_pool),
        notes=data_notes,
        describe=False,
    )
    earnings_screen = EarningsScreen(earnings)

    ladder_dict, liquidity_dict, top_stocks, aux_notes, indices_summary = (
        _extract_ladder_and_liquidity(
            limit_snapshot,
            broad_index_adapter,
            enabled=use_broad_index
            if broad_index_adapter is not None
            else (use_broad_index and use_limit_pool),
        )
    )
    global_market_dict, global_notes = _extract_global_market(
        global_market_adapter,
        enabled=use_global_market
        if global_market_adapter is not None
        else (use_global_market and use_limit_pool),
    )
    macro_news_dict, macro_notes = _extract_macro_context(
        macro_news_source,
        enabled=use_macro_news
        if macro_news_source is not None
        else (use_macro_news and use_limit_pool),
        trade_date=end,
    )
    data_notes.extend([*aux_notes, *global_notes, *macro_notes])

    eval_symbols = list(symbols)
    if not eval_symbols and limit_snapshot and limit_snapshot.limit_up:
        streak_ordered = sorted(limit_snapshot.limit_up, key=lambda s: s.streak, reverse=True)
        eval_symbols = [s.symbol for s in streak_ordered[:8]]

    bars_by_symbol = fetch_daily_bars(adapter, eval_symbols, start, end, data_notes)
    usable = {symbol: rows for symbol, rows in bars_by_symbol.items() if rows}
    data_notes.extend(_data_notes(adapter, eval_symbols, usable, end))
    trade_flow, factor_contexts, metadata, names, shares = _prepare_metadata_and_trade_flow(
        adapter, usable, detail_adapter, use_trade_flow, security_master_adapter, data_notes
    )
    result = evaluate_universe(
        usable,
        float_shares=shares,
        regime=regime,
        names=names,
        contexts=factor_contexts,
        provider=provider,
        # 见模块 docstring：传进来的票只标注不剔除。
        require_playbook=False,
    )
    data_notes.extend(result.notes)

    actionable, watch_only = (
        result.selected[:top_n] if top_n is not None else result.selected,
        result.rejected,
    )

    sizing = resolve_position_sizing(
        cycle_label=regime.cycle.value,
        cycle_cap=regime.position_cap,
        manual_factor=position_factor,
    )
    data_notes.extend(sizing.notes)

    trade_date_resolved = _latest_bar_date(usable, end)
    lhb_seats = fetch_lhb_seats(
        lhb_seat_adapter, list(usable), trade_date_resolved, enabled=use_lhb_seats, notes=data_notes
    )
    sector_confirmation = _load_sector_confirmation(output_dir, trade_date_resolved, data_notes)
    actionable_rows, watch_only_rows = (
        _decorate_rows(items, metadata, trade_flow, lhb_seats) for items in (actionable, watch_only)
    )
    actionable_rows, watch_only_rows = _apply_earnings_screen(
        actionable_rows, watch_only_rows, earnings_screen, data_notes
    )
    payload = {
        "trade_date": str(trade_date_resolved),
        "kind": "post_close",
        "strategy_version": strategy_version,
        "regime": regime.to_dict(),
        "position_factor": sizing.effective_factor,
        "actionable": actionable_rows,
        "watch_only": watch_only_rows,
        "sector_confirmation": sector_confirmation,
        "data_notes": data_notes,
        "ladder_cycle": ladder_dict,
        "liquidity_regime": liquidity_dict,
        "top_limit_stocks": top_stocks,
        "broad_indices": indices_summary,
        "global_market": global_market_dict,
        "macro_news": macro_news_dict,
    }
    # 「明天可以做的」那几行进台账，事后才有地方核对这张卡做对了没有。
    recorded = _record_ledger(
        journal,
        actionable_rows,
        trade_date_resolved,
        payload,
        market_state(None, sizing.effective_factor, limit_snapshot, regime, sizing=sizing),
        data_notes,
    )

    daily_review = _daily_review(output_dir, trade_date_resolved, data_notes)
    if daily_review is not None:
        payload["daily_review"] = daily_review
        if daily_review.get("original_plan_available"):
            payload["sector_confirmation"] = daily_review["sector_confirmation"]

    content = render_post_close_brief(payload)
    if daily_review is not None:
        content += f"\n\n{render_next_checks(daily_review)}"

    # 辩论跑在**已经算完的**结论上，输入只有 facts。
    content = _append_debate(content, payload, debate, data_notes)

    title = f"盘后简报 {trade_date_resolved}"
    # 文件名必须跟行情所属交易日走，而不是跟命令执行日走。15:00 前、休市日或
    # 数据源晚一日时二者会不同；按执行日命名会让次日盘前拿错复盘归属。
    file_notifier = FileNotifier(
        output_dir,
        slug="post-close",
        stamp=trade_date_resolved.strftime("%Y%m%d"),
    )
    channels = notifiers if notifiers is not None else [ConsoleNotifier(), file_notifier]
    delivered = publish(channels, title, content)

    # 交给次日盘前那一步。
    payload_path = _save_handoff(file_notifier, payload, result, data_notes)

    return {
        "title": title,
        "trade_date": str(trade_date_resolved),
        "scored": len(actionable) + len(watch_only),
        "actionable": len(actionable),
        "watch_only": len(watch_only),
        "requested": len(symbols),
        "regime": regime.cycle.value,
        "position_factor": sizing.effective_factor,
        "journal_records": recorded,
        "delivered": delivered,
        "data_notes": data_notes,
        "payload_path": str(payload_path) if payload_path is not None else None,
        "daily_review_path": (daily_review or {}).get("payload_path"),
        "brief_path": written_path(channels, file_notifier),
        "content": content,
    }


def _prepare_metadata_and_trade_flow(
    adapter: Any,
    usable: dict[str, Any],
    detail_adapter: Any | None,
    use_trade_flow: bool,
    security_master_adapter: Any | None,
    notes: list[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str], dict[str, float]]:
    trade_flow = fetch_trade_flow(
        detail_adapter,
        list(usable),
        enabled=use_trade_flow,
        notes=notes,
        off_note="已关闭逐笔资金旁证，本次盘后卡不含主动买卖摘要",
        subject="本次盘后卡",
    )
    factor_contexts = build_factor_contexts([], {}, trade_flow_by_symbol=trade_flow)
    metadata = security_metadata(adapter, list(usable), security_master_adapter)
    names = metadata_names(metadata, list(usable))
    shares = float_shares(adapter, list(usable))
    if not shares:
        notes.append("未取到流通股本，本次无换手率与筹码分布估算")
    return trade_flow, factor_contexts, metadata, names, shares


def _append_debate(
    content: str, payload: dict, debate: DebatePanel | None, notes: list[str]
) -> str:
    if debate is None:
        return content
    debated = debate.run(payload)
    notes.extend(debated.notes)
    block = debated.markdown()
    return f"{content}\n\n## 多空视角\n\n{block}" if block else content


def _decorate_rows(
    items: list[Any],
    metadata: dict[str, dict[str, Any]],
    trade_flow: dict[str, dict[str, Any]],
    lhb_seats: dict[str, Any],
) -> list[dict[str, Any]]:
    """选股行 → 补名称与分类 → 挂逐笔旁证 → 挂龙虎榜席位。可做与只能看两批走同一条路。"""
    rows = apply_security_metadata_rows([item.to_row() for item in items], metadata)
    return _attach_lhb_seats(_attach_trade_flow(rows, trade_flow), lhb_seats)


def _apply_earnings_screen(
    actionable_rows: list[dict[str, Any]],
    watch_only_rows: list[dict[str, Any]],
    screen: Any,
    notes: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """业绩排雷：若入围买入候选的标的命中严重预亏或持续亏损，移入只能看不能做。"""
    if screen is None or not getattr(screen, "active", False):
        return actionable_rows, watch_only_rows
    kept_actionable: list[dict[str, Any]] = []
    transferred_watch: list[dict[str, Any]] = []
    verdicts = screen.evaluate([str(row.get("symbol") or "") for row in actionable_rows])
    for row in actionable_rows:
        sym = normalize_symbol(str(row.get("symbol") or ""))
        verdict = verdicts.get(sym)
        if verdict and verdict.is_blocked:
            block_msg = f"业绩否决：{'; '.join(verdict.blocking)}"
            row_setup = row.setdefault("setup", {})
            row_setup.setdefault("rejected", []).append(
                {"playbook": row.get("playbook"), "reasons": [block_msg]}
            )
            transferred_watch.append(row)
            notes.append(f"{row.get('symbol')} {block_msg}，已被移出买入候选")
        else:
            if verdict:
                if verdict.catalysts:
                    row.setdefault("reasons", []).extend(verdict.catalysts)
                if verdict.warnings:
                    row.setdefault("warnings", []).extend(verdict.warnings)
            kept_actionable.append(row)
    return kept_actionable, [*watch_only_rows, *transferred_watch]


def _record_ledger(
    journal: Journal | None,
    rows: list[dict],
    day: date,
    payload: dict,
    state: dict[str, Any],
    notes: list[str],
) -> int:
    """只记过了打法门槛的 actionable；watch_only 没有可执行计划，记进去只会污染统计。

    参考价是当日收盘（行里的 `bar_date`），核对窗口从下一个交易日开始。没给台账
    就不记：测试与只读场景不该悄悄写状态目录。
    """
    if journal is None or not rows:
        return 0
    recorded = record_candidate_rows(
        journal, rows, day, payload, source=POST_CLOSE_SOURCE, market_state=state
    )
    notes.append(f"{recorded} 条次日执行卡候选已落台账（来源 post_close），将在 T+1..T+5 核对")
    return recorded


def _save_handoff(
    notifier: FileNotifier, payload: dict, result: Any, notes: list[str]
) -> Path | None:
    path = notifier.write_payload(payload, POST_CLOSE_LATEST_NAME)
    if path is None:
        notes.append("盘后执行卡保存失败，次日交接无法读取本轮结果")
    snapshots = {
        symbol: snapshot.model_dump(mode="json")
        for symbol, snapshot in _snapshots_of(result).items()
    }
    if notifier.write_payload(snapshots, SNAPSHOT_LATEST_NAME) is None:
        notes.append("盘后价位快照保存失败，次日交接缺少本轮价位")
    return path


def _daily_review(output_dir: str | Path, day: date, notes: list[str]) -> dict | None:
    try:
        result = run_daily_review(output_dir, trade_date=day, notifiers=[])
    except (OSError, ValueError, TypeError) as exc:
        notes.append(f"每日归因复盘未完成（{type(exc).__name__}: {exc}），本次仍保留盘后执行卡")
        return None
    review = result["payload"]
    if result["payload_path"] is None:
        notes.append("每日复盘未归档，次日复查项仅保存在本次盘后结果中")
    return {
        **{
            key: review.get(key)
            for key in (
                "schema",
                "trade_date",
                "as_of",
                "original_plan_available",
                "original_plan_id",
                "strategy_version",
                "sector_confirmation",
                "counts",
                "next_checks",
                "data_notes",
            )
        },
        "payload_path": result["payload_path"],
    }


def _snapshots_of(result: Any) -> dict[str, Any]:
    return {item.symbol: item.snapshot for item in list(result.selected) + list(result.rejected)}


def _load_sector_confirmation(
    output_dir: str | Path,
    trade_date: date,
    data_notes: list[str],
) -> dict[str, Any] | None:
    """盘后轻量验证盘前板块思路。

    没有盘前产物时直接跳过；有产物但盘中截面不足时，把 `sector_confirmation`
    自己给出的 data_status 渲染出来，不把盘后卡打断。
    """
    if not (Path(output_dir) / MORNING_LATEST_NAME).exists():
        return None
    try:
        confirmation = build_sector_confirmation(output_dir)
    except Exception as exc:
        data_notes.append(f"盘前板块验证失败（{type(exc).__name__}: {exc}）")
        return None
    target = confirmation.get("target_session")
    if target and str(target) != str(trade_date):
        data_notes.append(
            f"盘前板块验证归属 {target}，与本次盘后交易日 {trade_date} 不一致，已跳过"
        )
        return None
    return confirmation


def _attach_trade_flow(
    rows: list[dict[str, Any]],
    trade_flow: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    attached: list[dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        symbol = str(row.get("symbol") or "")
        if symbol and symbol in trade_flow:
            copied["trade_flow"] = dict(trade_flow[symbol])
        attached.append(copied)
    return attached


def _attach_lhb_seats(
    rows: list[dict[str, Any]], readings: dict[str, dict]
) -> list[dict[str, Any]]:
    return [
        {**row, "lhb_seats": dict(readings[row["symbol"]])}
        if row.get("symbol") in readings
        else dict(row)
        for row in rows
    ]


def _latest_bar_date(usable: dict[str, list[dict]], fallback: date | None = None) -> date:
    return max(
        (stamp for rows in usable.values() for row in rows if (stamp := bar_date(row)) is not None),
        default=fallback or beijing_today(),
    )


def _data_notes(
    adapter: Any,
    symbols: list[str],
    usable: dict[str, list[dict]],
    today: date | None = None,
) -> list[str]:
    notes: list[str] = []
    reference = today or beijing_today()
    try:
        health = adapter.health_check()
    except Exception:
        health = {}

    mode = health.get("mode")
    capabilities = health.get("capabilities", {})
    if mode:
        notes.append(f"行情源模式：{mode}")
    if mode == "free" or not capabilities.get("supports_intraday", True):
        notes.append("免费档仅提供日线，盘中监控与做 T 提示需要付费档实时/分钟数据")

    missing = [symbol for symbol in symbols if symbol not in usable]
    if missing:
        notes.append(f"无可用日线的标的：{'、'.join(missing)}")

    trade_date = _latest_bar_date(usable, reference) if usable else None
    if trade_date and trade_date != reference:
        notes.append(f"最新日线为 {trade_date}，非当日数据（休市或数据尚未更新）")

    notes.append("日线默认前复权，可用于结构与均线；涨跌停、封单与价格档位判断需改用不复权数据")
    return notes
