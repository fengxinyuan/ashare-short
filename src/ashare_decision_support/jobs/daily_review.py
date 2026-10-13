"""每日复盘的离线编排和按日归档，盘后自动调用，review --daily 可补跑。"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.artifacts import DAILY_REVIEW_LATEST_NAME
from ashare_decision_support.journal import Journal
from ashare_decision_support.news.store import NewsStore
from ashare_decision_support.notify import ConsoleNotifier
from ashare_decision_support.notify.base import Notifier, publish
from ashare_decision_support.research.daily_review import build_daily_review, render_daily_review
from ashare_decision_support.schedule.timezone import beijing_naive_now, to_beijing_naive
from ashare_decision_support.signals.series import parse_bar_date, parse_bar_datetime
from ashare_decision_support.state.store import file_lock, read_json_object, write_json, write_text


def run_daily_review(
    output_dir: str | Path = "briefs",
    *,
    trade_date: date | None = None,
    now: datetime | None = None,
    journal: Journal | None = None,
    news_store: NewsStore | None = None,
    store_root: Path | None = None,
    notifiers: list[Notifier] | None = None,
) -> dict[str, Any]:
    moment = to_beijing_naive(now or beijing_naive_now())
    day = trade_date or moment.date()
    payload = build_daily_review(
        day,
        output_dir,
        now=moment,
        journal=journal,
        news_store=news_store,
        store_root=store_root,
    )
    root = Path(output_dir)
    payload["recent_coverage"] = _recent_coverage(root, day, payload)
    paths = _save(root, day, payload)
    content = render_daily_review(payload)
    title = f"每日复盘 {day}"
    delivered = publish(notifiers if notifiers is not None else [ConsoleNotifier()], title, content)
    return {
        "trade_date": str(day),
        "counts": payload["counts"],
        **paths,
        "delivered": delivered,
        "data_notes": payload["data_notes"],
        "payload": payload,
        "content": content,
    }


def _save(root: Path, day: date, payload: dict) -> dict:
    archive = root / "daily-reviews" / f"{day}.json"
    markdown = archive.with_suffix(".md")
    paths: dict[str, Any] = {"payload_path": None, "brief_path": None, "latest_updated": False}
    try:
        with file_lock(archive):
            if _later(read_json_object(archive), payload):
                payload["data_notes"].append(
                    "已有截止时点或生成时间更晚的同日复盘，保留原档案；本次仅返回回放结果"
                )
                return paths
            write_json(archive, payload)
            paths["payload_path"] = str(archive)
            write_text(markdown, f"# 每日复盘 {day}\n\n{render_daily_review(payload)}\n")
            paths["brief_path"] = str(markdown)
    except (OSError, TypeError, ValueError) as exc:
        payload["data_notes"].append(
            f"每日复盘归档失败（{type(exc).__name__}），以返回的路径判断已保存产物"
        )
    if paths["payload_path"] is not None:
        latest = root / DAILY_REVIEW_LATEST_NAME
        try:
            with file_lock(latest):
                if not _later(read_json_object(latest), payload):
                    write_json(latest, payload)
                    paths["latest_updated"] = True
        except (OSError, TypeError, ValueError) as exc:
            payload["data_notes"].append(
                f"最新复盘索引保存失败（{type(exc).__name__}），同日档案仍可读取"
            )
    return paths


def _later(existing: dict, incoming: dict) -> bool:
    existing_date = parse_bar_date(existing.get("trade_date"))
    if existing_date is None:
        return False
    incoming_date = parse_bar_date(incoming["trade_date"])
    if existing_date != incoming_date:
        return existing_date > incoming_date
    previous, current = (parse_bar_datetime(row.get("as_of")) for row in (existing, incoming))
    if previous != current:
        return previous is not None and current is not None and previous > current
    # 历史补跑的截止都可能是当天23:59:59，仍须防止较早生成的任务后写入。
    previous_run, current_run = (
        parse_bar_datetime(row.get("generated_at")) for row in (existing, incoming)
    )
    return previous_run is not None and current_run is not None and previous_run > current_run


def _recent_coverage(root: Path, day: date, current: dict) -> dict:
    """近20个已归档日期的过程覆盖；同日反复回放始终只计一次。"""
    by_day = {day: current}
    for path in sorted((root / "daily-reviews").glob("*.json"), reverse=True):
        stamp = parse_bar_date(path.stem)
        if stamp is None or stamp >= day:
            continue
        row = read_json_object(path)
        if row.get("schema") != current["schema"] or parse_bar_date(row.get("trade_date")) != stamp:
            continue
        by_day[stamp] = row
        if len(by_day) >= 20:
            break
    rows = list(by_day.values())
    return {
        "sessions": len(rows),
        "from": str(min(by_day)),
        "to": str(day),
        "original_plan_sessions": sum(row.get("original_plan_available") is True for row in rows),
        "by_strategy_version": dict(
            Counter(str(row.get("strategy_version") or "未记录") for row in rows)
        ),
        "meaning": "按归档日期去重统计流程覆盖，不表示策略胜率或实际收益",
    }
