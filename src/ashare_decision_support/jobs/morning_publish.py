"""渲染盘前简报、组合分节并按通道发布；沿用前序阶段的判定。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.artifacts import MORNING_LATEST_NAME
from ashare_decision_support.jobs.morning_stages import (
    Collected,
    Fusion,
    Ledger,
    Selection,
    Sources,
)
from ashare_decision_support.news import MacroDigest
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier
from ashare_decision_support.notify.base import publish, written_path
from ashare_decision_support.notify.render import (
    DISCIPLINE_HEADING,
    MARKET_READ_HEADING,
    SECTOR_CANDIDATES_HEADING,
    SECTOR_RANKING_HEADING,
    THEME_RADAR_HEADING,
    render_candidate_levels_table,
    render_sector_brief,
    render_sector_ranking_table,
    render_theme_radar,
    sector_mandatory_block,
)
from ashare_decision_support.sectors import LEVEL_CONCEPT
from ashare_decision_support.writer import TemplateWriter, WriteRequest

READ_HEADING = "## 舆情通读"
MACRO_HEADING = "## 宏观联动"


def _execution_banner(facts: dict[str, Any]) -> str:
    if facts.get("execution_ready", True):
        return ""
    reasons = "；".join(facts.get("execution_blocks") or [])
    return (
        "⚠️ **本报告当前不可用于买入。** "
        f"{reasons}。"
        f"选股所用个股日线主要截至 {facts.get('data_date') or '未知'}；"
        "下方结构参考收盘价来自完整日线；盘中快照单独列示，不能替代实时买点确认。\n\n"
    )


def _decorate_execution_markdown(markdown: str, facts: dict[str, Any]) -> str:
    if facts.get("execution_ready", True):
        return markdown
    return _execution_banner(facts) + markdown


def stage_publish(
    sources: Sources,
    collected: Collected,
    fusion: Fusion,
    selection: Selection,
    ledger: Ledger,
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

    # 落最后一笔之后去重，原地改——`notes` 是贯穿全程的那一个列表，下面渲染与
    # payload 两处读的必须是同一份。

    # 重复不是假想：舆情那一路会 `fetch_market` **两次**（首轮 + 板块定向补抓），
    # 两次都把数据源健康报一遍，于是「美国劳工统计局 不可用」「华尔街见闻快讯 1620
    # 条」这类**描述同一次采集**的行各出现两次。读者看到的是「两个源挂了」，
    # 而事实是一个源挂了两次被数了两遍。压缩视图只留前 8 条，重复行还会挤掉真信息。
    notes[:] = list(dict.fromkeys(notes))

    baseline = render_sector_brief(
        trade_date=today,
        outlooks=fusion.selected,
        candidates=candidates,
        market=market,
        position_factor=fusion.effective_factor if facts.get("execution_ready", True) else 0.0,
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
        read_block = f"{READ_HEADING}\n\n{read_block}"
        baseline = _insert_section(baseline, read_block)
    macro_block = _macro_block(fusion.macro_links)
    if macro_block:
        macro_block = f"{MACRO_HEADING}\n\n{macro_block}"
        baseline = _insert_section(baseline, macro_block)
    theme_block = render_theme_radar(facts.get("theme_radar"))
    if theme_block and THEME_RADAR_HEADING not in baseline:
        baseline = _insert_section(baseline, theme_block)
    if debate_block:
        baseline = f"{baseline}\n\n## 多空视角\n\n{debate_block}"

    written = (sources.writer or TemplateWriter()).write(
        WriteRequest(
            title=f"盘前简报 {today}",
            facts=facts,
            fallback_markdown=baseline,
            mandatory_suffix=sector_mandatory_block(facts.get("position_factor", 0.0)),
            verbatim_sections=(SECTOR_RANKING_HEADING, SECTOR_CANDIDATES_HEADING),
        )
    )
    notes.extend(written.notes)
    markdown = written.markdown
    if read_block and READ_HEADING not in markdown:
        # LLM 写稿层可能把这一节改写掉或整段吞掉。通读带的是**原文引用**，被改写
        # 就失去了可核对性，所以按原样补回去，和下面的多空视角同一个处理。
        markdown = _insert_section(markdown, read_block)
    if macro_block and MACRO_HEADING not in markdown:
        markdown = _insert_section(markdown, macro_block)
    if theme_block and THEME_RADAR_HEADING not in markdown:
        markdown = _insert_section(markdown, theme_block)
    markdown = _restore_tables(markdown, fusion.selected, candidates, notes)
    markdown = _decorate_execution_markdown(markdown, facts)
    if debate_block and debate_block not in markdown:
        markdown = f"{markdown}\n\n## 多空视角\n\n{debate_block}"

    title = f"盘前简报 {today}"
    file_notifier = FileNotifier(output_path, slug="morning")
    channels = (
        sources.notifiers if sources.notifiers is not None else [ConsoleNotifier(), file_notifier]
    )
    delivered = publish(channels, title, markdown)
    payload_path = file_notifier.write_payload(facts, MORNING_LATEST_NAME)
    if payload_path is None:
        notes.append("盘前事实包保存失败，后续读取将缺少本轮结果")
    brief_path = written_path(channels, file_notifier)
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
            item.name for item in fusion.selected if item.level != LEVEL_CONCEPT
        ],
        "concept_sectors_selected": [
            item.name for item in fusion.selected if item.level == LEVEL_CONCEPT
        ],
        "candidates": len(candidates),
        "candidate_symbols": [item["symbol"] for item in candidates],
        "execution_ready": facts.get("execution_ready", True),
        "funnel": dict(selection.funnel),
        "diagnostic": dict(selection.diagnostic),
        "diagnostic_mode": bool(selection.diagnostic),
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
        "position_factor": facts.get("position_factor", 0.0),
        "journal_records": ledger.recorded,
        "writer": written.writer,
        "used_fallback": written.used_fallback,
        "delivered": delivered,
        "data_notes": notes,
        "payload_path": str(payload_path) if payload_path is not None else None,
        "plan_id": facts.get("plan_id"),
        "plan_revision": facts.get("plan_revision"),
        "strategy_version": facts.get("strategy_version"),
        "brief_path": brief_path,
        "content": markdown,
    }


def _restore_tables(markdown: str, outlooks: list, candidates: list[dict], notes: list[str]) -> str:
    """板块排序表与候选价位表按原样补回写稿层丢掉的那份。

    2026-09-14 实测：LLM 稿把 18 只候选一只都没点名，价位只剩在 JSON 里——而价位
    正是这份简报里要照着执行的那部分，不能经过改写，也不能让读的人去翻
    `morning_latest.json`。补的是表不是明细卡：叙述交给写稿层，数字表原样保留。
    """
    for heading, table in (
        (SECTOR_RANKING_HEADING, render_sector_ranking_table(outlooks)),
        (SECTOR_CANDIDATES_HEADING, render_candidate_levels_table(candidates)),
    ):
        if heading not in markdown:
            markdown = _insert_section(markdown, table)
            notes.append(f"写稿层未保留「{heading.lstrip('#').strip()}」，已按模板原样补回")
    return markdown


def _insert_section(markdown: str, block: str) -> str:
    """按共用标题锚点插入分节：大盘解读之前，其次纪律提醒之前，最后才追加到末尾。"""
    for anchor in (MARKET_READ_HEADING, DISCIPLINE_HEADING):
        head, sep, tail = markdown.partition(anchor)
        if sep:
            return f"{head}{block}\n\n{sep}{tail}"
    return f"{markdown}\n\n{block}"


def _macro_block(digest: MacroDigest) -> str:
    if not digest.ok or not digest.links:
        return ""
    lines: list[str] = ["以下为新闻自动提取的传导线索；商品报价未独立核验。"]
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
