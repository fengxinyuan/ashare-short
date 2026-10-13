"""用可控的计划、行情和新闻时间线验证每日复盘，所有路径离线。"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

from ashare_decision_support import cli
from ashare_decision_support.artifacts import DAILY_REVIEW_LATEST_NAME
from ashare_decision_support.ask.context import assemble
from ashare_decision_support.ask.plan import STEP_JOURNAL, build_plan
from ashare_decision_support.ask.question import parse_question
from ashare_decision_support.jobs.daily_review import run_daily_review
from ashare_decision_support.jobs.pre_open import run_pre_open
from ashare_decision_support.news import NewsDoc
from ashare_decision_support.news.store import NewsStore
from ashare_decision_support.research.daily_review import build_daily_review
from ashare_decision_support.sectors.intraday import SectorRead, SectorSnapshot, append, store_path
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import append_jsonl, read_json_object, write_json
from ashare_decision_support.state.workflow import archive_plan, record_event

DAY = date(2026, 8, 3)
NOW = datetime(2026, 8, 3, 16)


def facts(day=DAY, sector="电子", symbols=("600519.SH", "000001.SZ")):
    return {
        "trade_date": str(day),
        "strategy_version": "test-v2",
        "strategy_parameters": {"top_sectors": 3},
        "market": {"score": 70, "direction": "偏多"},
        "sectors": [
            {"name": sector, "level": "THS_INDUSTRY", "score": 80, "ret_1d": 0.01, "ret_5d": 0.03}
        ],
        "candidates": [{"symbol": symbol, "sector": sector} for symbol in symbols],
    }


def snapshot(at, *, sector="电子", breadth=0.7, excess=0.8, share=0.11):
    row = SectorRead(sector, "THS_INDUSTRY", excess, excess, breadth, 100, share, 20, 20)
    append(SectorSnapshot(at, [row]), store_path(str(at.date())))


def stock_poll(at, *, price=100, trigger=None, symbol="600519.SH", source_at=None, **extra):
    observation = {
        "symbol": symbol,
        "price": price,
        "source_at": source_at or at.isoformat(),
        "entry_low": 99,
        "entry_high": 101,
        "stop": 95,
        "targets": [110],
        "armed": True,
    } | extra
    alerts = (
        [{"symbol": symbol, "trigger": trigger, "headline": f"测试{trigger}"}] if trigger else []
    )
    return record_event(
        at.date(),
        "stock_poll",
        {
            "watch_symbols": [symbol],
            "health": {"usable": True},
            "observations": [observation],
            "alerts": alerts,
        },
        at,
    )


def test_original_plan_is_preserved_and_events_follow_the_version_available_then(tmp_path):
    first = archive_plan(tmp_path, facts(), now=NOW.replace(hour=9))
    second = archive_plan(
        tmp_path,
        facts(sector="医药生物", symbols=("600519.SH", "300750.SZ")),
        now=NOW.replace(hour=11),
    )
    # 最新晨报已被别的日期覆盖，也不能污染原日回放。
    write_json(tmp_path / "morning_latest.json", facts(day=DAY + timedelta(days=1)))
    stock_poll(NOW.replace(hour=10), trigger="entry_confirmed")
    stock_poll(NOW.replace(hour=14), price=94, trigger="stop_break")
    for hour, minute, share in ((9, 35, 0.09), (10, 0, 0.10), (14, 50, 0.11)):
        snapshot(NOW.replace(hour=hour, minute=minute), share=share)

    result = run_daily_review(tmp_path, trade_date=DAY, now=NOW, notifiers=[])
    review = result["payload"]
    assert review["original_plan_id"] == first["plan_id"]
    assert [row["strategy_parameters"] for row in review["plans"]] == [{"top_sectors": 3}] * 2
    assert review["sector_confirmation"]["results"][0]["matched_members"] == ["电子"]
    assert review["sector_confirmation"]["results"][0]["status"] == "已确认"
    alerts = [row for row in review["timeline"] if row["kind"] == "alert"]
    assert [row["plan_id"] for row in alerts] == [first["plan_id"], second["plan_id"]]
    candidates = {row["symbol"]: row for row in review["candidates"]}
    assert candidates["600519.SH"]["actual_execution"] == "未记录"
    assert candidates["600519.SH"]["observed_hits"]["stop"][0]["price"] == 94
    assert candidates["000001.SZ"]["observations"] == 0
    assert candidates["000001.SZ"]["attribution"] == "监控覆盖不足"
    assert result["payload_path"] and result["brief_path"]
    assert any("没有收盘截面" in note for note in review["data_notes"])
    assert "实际成交" not in " ".join(row["attribution"] for row in review["candidates"])


def test_news_uses_first_seen_and_only_matches_later_price_evidence(tmp_path):
    plan = archive_plan(tmp_path, facts(), now=NOW.replace(hour=9))
    corpus = NewsStore(tmp_path / "news.sqlite3")
    news = NewsDoc(
        title="电子产业链新增订单",
        source="测试源",
        published_at="2026-08-03 10:00:00",
        url="https://example.test/news",
    )
    corpus.ingest([news], DAY, now=NOW.replace(hour=10, minute=1))
    corpus.label(news, sectors=["电子"])
    corpus.ingest([NewsDoc(title="未来消息", source="测试源")], DAY, now=NOW + timedelta(days=1))
    snapshot(NOW.replace(hour=9, minute=45))
    snapshot(NOW.replace(hour=11))
    snapshot(NOW.replace(hour=15))
    review = build_daily_review(DAY, tmp_path, now=NOW.replace(hour=12), news_store=corpus)
    assert len(review["news"]) == 1
    row = review["news"][0]
    assert row["plan_id"] == plan["plan_id"]
    assert row["source"] == "测试源" and row["url"] == news.url
    assert [item["as_of"] for item in row["price_evidence"]] == ["2026-08-03T11:00:00"]
    assert "不证明因果" in row["follow_up"]
    assert review["coverage"]["sector_snapshots"] == 2


@pytest.mark.parametrize("by", ["first_seen", "last_seen"])
def test_news_upper_bound_is_applied_before_the_limit(tmp_path, by):
    corpus = NewsStore(tmp_path / "news.sqlite3")
    corpus.ingest([NewsDoc(title="当前时点已知消息", source="test")], DAY, now=NOW)
    for index in range(3):
        corpus.ingest(
            [NewsDoc(title=f"下一日消息{index}", source="test")],
            DAY,
            now=NOW + timedelta(days=1, minutes=index),
        )
    rows = corpus.recent(now=NOW.replace(tzinfo=timezone(timedelta(hours=8))), by=by, limit=1)
    assert [row.title for row in rows] == ["当前时点已知消息"]


@pytest.mark.parametrize("kind", ["missing", "legacy", "after_close", "backfill", "other_date"])
def test_missing_or_late_plan_never_becomes_a_verified_preopen_prediction(tmp_path, kind):
    if kind == "legacy":
        write_json(tmp_path / "morning_latest.json", facts())
    elif kind in {"after_close", "backfill"}:
        archive_plan(
            tmp_path, facts(), now=NOW if kind == "after_close" else NOW + timedelta(days=1)
        )
    elif kind == "other_date":
        write_json(tmp_path / "morning_latest.json", facts(day=DAY - timedelta(days=1)))
    stock_poll(NOW.replace(hour=10), trigger="entry_confirmed")
    review = build_daily_review(DAY, tmp_path, now=NOW)
    assert review["original_plan_available"] is False
    assert review["original_plan_id"] is None
    alerts = [row for row in review["timeline"] if row["kind"] == "alert"]
    assert alerts[0]["plan_id"] is None
    assert any(row["subject"] == "盘前计划" for row in review["next_checks"])


def test_legacy_and_midday_plans_are_not_confirmed_by_earlier_snapshots(tmp_path):
    archive_plan(tmp_path, facts(), now=NOW.replace(hour=13))
    for minute, share in ((35, 0.08), (40, 0.10), (45, 0.12)):
        snapshot(NOW.replace(hour=9, minute=minute), share=share)
    result = build_daily_review(DAY, tmp_path, now=NOW)
    assert result["sector_confirmation"]["snapshot_count"] == 0
    assert result["sector_confirmation"]["results"][0]["status"] == "等待数据"
    assert result["sector_confirmation"]["results"][0]["attribution"] == "仅事后对照"


@pytest.mark.parametrize(
    "source_at", ["2026-08-02T10:00:00", "2026-08-03T11:00:00", "2026-08-03T09:00:00", "bad"]
)
def test_invalid_quote_timestamps_cannot_validate_entry_or_exit(tmp_path, source_at):
    archive_plan(tmp_path, facts(), now=NOW.replace(hour=9))
    stock_poll(NOW.replace(hour=10), source_at=source_at)
    review = build_daily_review(DAY, tmp_path, now=NOW)
    candidate = next(row for row in review["candidates"] if row["symbol"] == "600519.SH")
    assert candidate["observations"] == 0
    assert candidate["observed_hits"] == {"entry": [], "stop": [], "target": []}


def test_corrupt_event_shapes_and_bad_plan_sections_yield_a_partial_report(tmp_path):
    archive_plan(
        tmp_path, facts() | {"sectors": ["bad"], "sector_clusters": 42}, now=NOW.replace(hour=9)
    )
    record_event(
        DAY,
        "stock_poll",
        {"watch_symbols": 8, "alerts": "bad", "health": 3, "observations": {}},
        NOW.replace(hour=10),
    )
    append_jsonl(state_path("events", f"{DAY}.jsonl"), {"at": None, "event_id": {}})
    stock_poll(NOW.replace(hour=11), targets=13)
    review = build_daily_review(DAY, tmp_path, now=NOW)
    assert review["coverage"]["unusable_polls"] == 1
    assert review["counts"]["observed_candidates"] == 1
    assert any("板块字段无法解析" in note for note in review["data_notes"])


def test_replaying_an_old_date_does_not_replace_the_latest_review(tmp_path):
    newer = DAY + timedelta(days=1)
    run_daily_review(tmp_path, trade_date=newer, now=NOW + timedelta(days=1), notifiers=[])
    old = run_daily_review(tmp_path, trade_date=DAY, now=NOW + timedelta(days=1), notifiers=[])
    assert old["payload_path"]
    assert old["latest_updated"] is False
    assert read_json_object(tmp_path / DAILY_REVIEW_LATEST_NAME)["trade_date"] == str(newer)
    assert old["payload"]["recent_coverage"]["sessions"] == 1


def test_an_earlier_same_day_replay_keeps_the_later_archive(tmp_path):
    run_daily_review(tmp_path, trade_date=DAY, now=NOW, notifiers=[])
    old = run_daily_review(tmp_path, trade_date=DAY, now=NOW.replace(hour=10), notifiers=[])
    assert old["payload_path"] is None
    assert read_json_object(tmp_path / "daily-reviews" / f"{DAY}.json")["as_of"].startswith(
        "2026-08-03T16:"
    )


def test_historical_runs_with_the_same_cutoff_preserve_the_newer_generation(tmp_path):
    later = NOW + timedelta(days=2)
    earlier = NOW + timedelta(days=1)
    run_daily_review(tmp_path, trade_date=DAY, now=later, notifiers=[])
    old = run_daily_review(tmp_path, trade_date=DAY, now=earlier, notifiers=[])
    assert old["payload_path"] is None
    saved = read_json_object(tmp_path / DAILY_REVIEW_LATEST_NAME)
    assert saved["generated_at"].startswith("2026-08-05T16:")
    assert saved["as_of"].startswith("2026-08-03T23:59:59")


def test_archive_write_failure_reports_unsaved_result_and_keeps_old_file(tmp_path, monkeypatch):
    run_daily_review(tmp_path, trade_date=DAY, now=NOW, notifiers=[])
    path = tmp_path / "daily-reviews" / f"{DAY}.json"
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("ashare_decision_support.jobs.daily_review.write_json", fail)
    result = run_daily_review(
        tmp_path, trade_date=DAY, now=NOW + timedelta(minutes=1), notifiers=[]
    )
    assert result["payload_path"] is None and result["brief_path"] is None
    assert path.read_bytes() == before
    assert any("归档失败" in note for note in result["data_notes"])


def test_next_day_handoff_and_ask_use_the_review_without_resolved_outcomes(tmp_path):
    archive_plan(tmp_path, facts(), now=NOW.replace(hour=9))
    review = run_daily_review(tmp_path, trade_date=DAY, now=NOW, notifiers=[])["payload"]
    write_json(
        tmp_path / "post_close_latest.json",
        {"trade_date": str(DAY), "actionable": [], "watch_only": []},
    )
    preopen = run_pre_open(tmp_path / "post_close_latest.json", notifiers=[])
    assert preopen["next_checks"] == review["next_checks"]
    assert "次日复查" in preopen["content"]
    ctx = assemble(moment=NOW + timedelta(days=1), output_dir=tmp_path)
    assert ctx.blocks["每日归因复盘"]["original_plan_id"] == review["original_plan_id"]
    plan = build_plan(parse_question("复盘一下"), ctx.blocks, [], phase="post_close")
    step = next(step for step in plan.steps if step.key == STEP_JOURNAL)
    assert step.supported
    assert "每日归因复盘" in step.facts


def test_facts_do_not_use_a_review_after_the_question_time(tmp_path):
    run_daily_review(tmp_path, trade_date=DAY, now=NOW, notifiers=[])
    ctx = assemble(moment=NOW.replace(hour=10), output_dir=tmp_path)
    assert "每日归因复盘" not in ctx.blocks
    assert any("未使用未来结果" in note for note in ctx.notes)


def test_cli_daily_review_runs_without_constructing_a_market_adapter(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise AssertionError("离线复盘不应构造行情适配器")

    monkeypatch.setattr(cli, "_tickflow", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ashare-ds", "review", "--daily", str(DAY), "--output-dir", str(tmp_path), "--quiet"],
    )
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["trade_date"] == str(DAY)
    assert (tmp_path / "daily-reviews" / f"{DAY}.json").is_file()


@pytest.mark.parametrize("day", ["not-a-date", "2099-01-01"])
def test_cli_daily_review_rejects_invalid_or_future_dates(tmp_path, day, capsys, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["ashare-ds", "review", "--daily", day, "--output-dir", str(tmp_path), "--quiet"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "错误" in capsys.readouterr().err
