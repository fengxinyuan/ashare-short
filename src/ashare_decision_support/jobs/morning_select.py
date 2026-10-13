"""在 Fusion 选定的板块内执行风险筛选、个股评分和配额选择。"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from ashare_decision_support.adapters.base import (
    apply_security_metadata_rows,
    fetch_daily_bars,
    float_shares,
    metadata_names,
    security_names,
)
from ashare_decision_support.agents import AgentContext, SymbolNewsAgent, run_agents
from ashare_decision_support.jobs.morning_stages import (
    Collected,
    Fusion,
    MorningConfig,
    Selection,
    Sources,
    Universe,
)
from ashare_decision_support.jobs.sources import fetch_trade_flow
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
from ashare_decision_support.sectors import reverse_index
from ashare_decision_support.sectors.overseas_link import BEARISH
from ashare_decision_support.strategy.regime import RegimeVerdict
from ashare_decision_support.strategy.select import analyze_universe, select_from
from ashare_decision_support.symbols import normalize_symbol

CANDIDATE_LOOKBACK_DAYS = 240

# 粗排配额倍数与全局上限，见 `_rank_candidates` 的说明。
NEWS_SHORTLIST_MULTIPLIER = 3

NEWS_MAX_SYMBOLS = 40


def stage_select(
    sources: Sources,
    cfg: MorningConfig,
    universe: Universe,
    collected: Collected,
    fusion: Fusion,
    today: date,
    notes: list[str],
) -> Selection:
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
    _attach_overseas_context(candidates, fusion.overseas)
    _attach_overseas_context(control, fusion.overseas)

    # 逐笔资金旁证。**只挂在最终候选上**，不参与打分也不参与排序——它是上一个
    # 交易日的成交明细，回答的是「昨天那波买盘里有没有大单、尾盘是不是在走」，
    # 而候选是按量价结构选出来的。两件事分开：结构决定选不选，逐笔只是在选完
    # 之后多给一眼。

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

    diagnostic = {}
    if cfg.max_constituents is not None:
        diagnostic = {
            "mode": "sampled",
            "reason": "--max-constituents 仅用于快速诊断，不代表正式全量板块判断",
            "max_constituents": cfg.max_constituents,
            "sampled_symbols": len(universe.all_symbols),
        }

    return Selection(
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
        diagnostic=diagnostic,
    )


def _attach_trade_flow(rows: list[dict], flows: dict[str, dict]) -> None:
    """把逐笔摘要挂到候选行上。原地改，与 `_attach_earnings` 同一处置。"""
    for row in rows:
        payload = flows.get(str(row.get("symbol") or ""))
        if payload:
            row["trade_flow"] = dict(payload)


def _apply_screens(
    symbols: list[str],
    announcements: list[dict],
    scan: Any | None = None,
    earnings: Any | None = None,
) -> tuple[list[str], dict[str, Any], list[str]]:
    notes: list[str] = []

    # ST/退市状态不再是候选的过滤条件。可交易性仍然是独立门槛：它处理的是
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


def _attach_overseas_context(rows: list[dict], overseas: Any) -> None:
    """把指向候选所属板块的外盘链路挂到行上。**不动 `score`，不改排序。**

    利空链路进 `warnings`：它和「跌破止损」一样，是读的人决定要不要下手前必须
    看到的一条。利多链路只进 `reasons` 末尾——外盘涨过了对 A 股是「已兑现的
    预期」还是「还没跟的差价」，这一层答不了，不该以警告的分量出现。两者都完整
    保留在 `overseas_context`，台账事后按方向分组回答「外盘同向那天胜率有没有差」。
    """
    if overseas is None:
        return
    by_sector = overseas.by_sector()
    if not by_sector:
        return
    for row in rows:
        # 按入选板块与同花顺行业各查一次：一只票常以概念入选（「东数西算」），
        # 而映射表指向的是它的行业（「元件」）。**不查概念标签**——一只检测公司挂着
        # 40 个概念，「核电」「芯片概念」都在里面，按概念匹配会把铀价、费半全挂到
        # 它头上，那是噪声不是旁证。
        keys = dict.fromkeys(
            key for key in (str(row.get("sector") or ""), str(row.get("industry") or "")) if key
        )
        links = list(dict.fromkeys(link for key in keys for link in by_sector.get(key, ())))
        if not links:
            continue
        row["overseas_context"] = [item.to_dict() for item in links]
        for link in links:
            line = f"外盘映射：{link.headline}"
            if link.direction == BEARISH:
                row.setdefault("warnings", []).append(line)
            else:
                row.setdefault("reasons", []).append(line)


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
        adapter,
        symbols,
        today - timedelta(days=CANDIDATE_LOOKBACK_DAYS),
        today,
        sink,
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
    thin_reward = 0
    for item in fine.selected:
        sector = sector_of.get(item.symbol, "未分类")
        row = item.to_row()
        row["sector"] = sector
        # 盈亏比门槛在配额之前：一只目标价只比现价高 0.4% 的票（盈亏比 0.06）
        # 此前照样占着板块名额、进晨报候选表，风险栏写着「不划算」。不划算就
        # 不是候选；它退回对照组，事后照样能回答「这道门拦对了没有」。
        if _thin_reward_risk(row):
            thin_reward += 1
            control.append(row)
            continue
        over_quota = per_sector_count.get(sector, 0) >= per_sector
        if not over_quota:
            per_sector_count[sector] = per_sector_count.get(sector, 0) + 1
        (control if over_quota else picked).append(row)
    if thin_reward:
        sink.append(
            f"盈亏比低于 {REWARD_RISK_FLOOR:.1f} 的 {thin_reward} 只不进候选，退回对照组"
            "（止损距离按最小有效止损重算后仍不划算）"
        )

    # 对照组取和入选组等量的落选样本。等量是为了让两组的统计噪声量级可比；
    # 取紧挨着的（分数最高的落选者）是因为这才是真正的边缘对照——拿全市场
    # 垫底的票做对照，差出来的是「好票 vs 烂票」，不是「这套规则有没有用」。
    control = control[: len(picked)]
    sink.extend(_empty_sector_notes(coarse, sector_of, picked, symbols))
    picked = apply_security_metadata_rows(picked, metadata_by_symbol or {}, prefer_name=True)
    control = apply_security_metadata_rows(control, metadata_by_symbol or {}, prefer_name=True)
    _flag_st(picked)
    _flag_st(control)
    return picked, control


# 打分层在盈亏比低于这个值时写的警告句；`monitor/triggers.py` 也按同一句话识别。
# 阈值本身在 `scoring/rule_based.py:_score_risk_reward`，这里不另存一份数字。
REWARD_RISK_FLOOR = 0.8
REWARD_RISK_REJECT_MARK = "风险收益不划算"


def _thin_reward_risk(row: dict) -> bool:
    return any(REWARD_RISK_REJECT_MARK in str(item) for item in row.get("warnings") or [])


def _flag_st(rows: list[dict]) -> None:
    """为名称含 ST 或「退」的候选补充风险提示；该函数不作筛除。"""
    for row in rows:
        name = str(row.get("name") or "").upper()
        if "ST" in name or "退" in name:
            row.setdefault("warnings", []).append(
                "ST/退市风险标的：涨跌幅限制 5%，退市与流动性风险高于普通标的"
            )


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


def _empty_sector_notes(
    coarse: Any, sector_of: dict[str, str], picked: list[dict], scope: list[str]
) -> list[str]:
    """主攻板块一只候选都没出时，说出是在哪一道被挡掉的。

    2026-09-14 实测：简报「先看 元件、培育钻石、汽车整车」，候选表里这三个板块
    一只都没有，读的人只能猜。原因其实很具体——情绪周期「分歧」不允许突破打法，
    而排名第一的板块正处在突破位。这句话不说，「先看元件」就成了空话。
    """
    picked_sectors = {str(row.get("sector") or "") for row in picked}
    in_scope = set(scope)
    notes: list[str] = []
    for sector in dict.fromkeys(sector_of.values()):
        if sector in picked_sectors:
            continue
        members = [s for s, name in sector_of.items() if name == sector and s in in_scope]
        if not members:
            continue
        matches = [coarse.matches[s] for s in members if s in coarse.matches]
        qualified = sum(1 for m in matches if m.ok)
        blocked: dict[str, int] = {}
        reasons: dict[str, int] = {}
        for match in matches:
            if match.ok:
                continue
            for playbook in match.blocked_by_regime:
                blocked[playbook.value] = blocked.get(playbook.value, 0) + 1
            for text in match.reject_reasons:
                key = _reject_reason_key(text)
                reasons[key] = reasons.get(key, 0) + 1
        parts = [f"入围 {len(members)} 只，{qualified} 只满足入场判据"]
        if blocked:
            top = max(blocked.items(), key=lambda kv: kv[1])
            parts.append(f"{top[1]} 只的「{top[0]}」打法被情绪周期挡掉")
        if reasons:
            top = max(reasons.items(), key=lambda kv: kv[1])
            parts.append(f"最常见否决：{top[0]}（{top[1]} 只）")
        if qualified:
            parts.append("满足判据的在盈亏比门槛或精排后落选")
        notes.append(f"主攻板块无候选 {sector}：" + "；".join(parts))
    return notes


def _reject_reason_key(text: str) -> str:
    """否决理由去掉测量值，只留结论句，让同一条规则在不同票上能聚合。

    「60 日分位 0.50（<0.60），尚未到突破位」→「尚未到突破位」；
    「尚未回调（none），不是回踩位置」→「不是回踩位置」。
    """
    stripped = re.sub(r"（[^）]*）", "", text)
    _, _, tail = stripped.partition("，")
    return (tail or stripped).strip()


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
