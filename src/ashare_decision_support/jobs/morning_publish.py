"""⑥ 发布：渲染简报、拼接各分节、按通道推送。

从 `jobs/morning.py` 搬出来的一段。这一段**不做任何判断**：进来什么就印什么，
分节的插入位置是排版问题。判定全在前面四段。
"""

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
    render_sector_brief,
    render_theme_radar,
    sector_mandatory_block,
)
from ashare_decision_support.sectors import CONCEPT_LEVEL
from ashare_decision_support.writer import TemplateWriter, WriteRequest


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
            item.name for item in fusion.selected if item.level != CONCEPT_LEVEL
        ],
        "concept_sectors_selected": [
            item.name for item in fusion.selected if item.level == CONCEPT_LEVEL
        ],
        "candidates": len(candidates),
        "candidate_symbols": [item["symbol"] for item in candidates],
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
