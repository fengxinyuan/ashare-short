"""实际任务入口的计划、盘中证据和次日交接；所有数据源使用已有离线夹具。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ashare_decision_support.advice.engine import StockAdvice
from ashare_decision_support.jobs.morning import MorningConfig, Sources, run_morning
from ashare_decision_support.jobs.post_close import run_post_close
from ashare_decision_support.jobs.pre_open import run_pre_open
from ashare_decision_support.journal import Journal, Recommendation
from ashare_decision_support.monitor import MonitorSession
from ashare_decision_support.monitor.sector_session import SectorSession
from ashare_decision_support.notify.local import FileNotifier
from ashare_decision_support.research.daily_review import build_daily_review
from ashare_decision_support.sectors.intraday import SectorRead, SectorSnapshot
from ashare_decision_support.state import Positions
from ashare_decision_support.state.store import read_json_object
from ashare_decision_support.state.workflow import archive_plan, read_events, read_plans
from tests.test_automation import RISE, ScriptedIntraday, StubDaily, minute_bars
from tests.test_daily_jobs import FakeAdapter
from tests.test_daily_review import facts
from tests.test_writer_and_sector_job import (
    FakeNewsSource,
    FakeSectorAdapter,
    FakeStaticTaxonomy,
    limit_pool,
    pool_adapter,
)

DAY = date(2026, 8, 3)
MORNING = datetime(2026, 8, 3, 9)
CLOSE = datetime(2026, 8, 3, 16)


def _morning(output_dir):
    return run_morning(
        Sources(
            adapter=FakeSectorAdapter(),
            industry_taxonomy=FakeStaticTaxonomy(),
            news_source=FakeNewsSource(),
            notifiers=[],
            limit_pool_adapter=pool_adapter(limit_pool(60, 6, 5, 1)),
        ),
        output_dir,
        trade_date=DAY,
        config=MorningConfig(
            strategy_version="workflow-v3",
            top_sectors=1,
            candidates_per_sector=2,
            use_sector_index=False,
            use_market_scan=False,
            use_earnings=False,
            use_broad_index=False,
            use_trade_flow=False,
            use_concepts=False,
        ),
    )


def test_morning_archives_parameters_and_links_all_journal_groups(tmp_path, monkeypatch):
    monkeypatch.setattr("ashare_decision_support.state.workflow.beijing_naive_now", lambda: MORNING)
    result = _morning(tmp_path)
    rows = read_plans(tmp_path, DAY, until=CLOSE, notes=[])
    assert len(rows) == 1
    assert result["plan_id"] == rows[0]["plan_id"]
    assert rows[0]["facts"]["strategy_parameters"]["candidates_per_sector"] == 2
    records = Journal().recommendations(DAY, DAY)
    assert records
    assert {row["plan_id"] for row in records} == {rows[0]["plan_id"]}
    assert {row["strategy_version"] for row in records} == {"workflow-v3"}


def test_morning_still_publishes_when_plan_archive_fails(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("ashare_decision_support.jobs.morning_record.archive_plan", fail)
    result = _morning(tmp_path)
    assert result["payload_path"]
    assert result["plan_id"] is None
    assert any("计划归档失败" in note for note in result["data_notes"])
    assert Journal().recommendations(DAY, DAY)


def test_advice_conversion_carries_plan_and_strategy_metadata():
    advice = StockAdvice(symbol="600519.SH", last_price=100)
    row = Recommendation.from_advice(
        advice, DAY, "advise", facts={"plan_id": "p", "strategy_version": "manual-v2"}
    )
    assert row.plan_id == "p"
    assert row.strategy_version == "manual-v2"


def _session():
    bars = minute_bars(RISE)
    session = MonitorSession(StubDaily(), ScriptedIntraday([bars]), DAY)
    positions = Positions()
    positions.open("600519", 100, plan_stop=103)
    session.prime(positions.symbols())
    session.load_positions(positions)
    return session, bars[-1]["timestamp"] + timedelta(minutes=1)


def test_monitor_records_prices_pinned_or_manual_levels_alerts_and_suspensions(tmp_path):
    session, moment = _session()
    result = session.poll(["600519"], moment)
    assert any(row.trigger == "stop_break" for row in result.alerts)
    session.poll(["600519"], moment + timedelta(hours=1))
    session.poll([], moment + timedelta(hours=2))
    events = read_events(DAY, until=CLOSE, notes=[])
    assert len(events) == 3
    assert events[0]["observations"][0]["stop"] == 103
    assert events[0]["observations"][0]["holding"] is True
    assert events[0]["alerts"][0]["symbol"] == "600519.SH"
    assert events[1]["health"]["usable"] is False
    assert events[2]["watch_symbols"] == []
    review = build_daily_review(DAY, tmp_path, now=CLOSE)
    assert review["counts"]["observed_candidates"] == 1
    assert review["coverage"]["unusable_polls"] == 2


def test_event_write_failure_does_not_drop_generated_stock_alerts(monkeypatch):
    session, moment = _session()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("ashare_decision_support.monitor.session.record_event", fail)
    result = session.poll(["600519"], moment)
    assert any(row.trigger == "stop_break" for row in result.alerts)
    assert any("过程记录保存失败" in note for note in result.notes)


def test_sector_session_records_snapshot_reference_and_failure_notes(tmp_path, monkeypatch):
    moment = MORNING.replace(hour=10)
    row = SectorRead("电子", "THS_INDUSTRY", 1, 0.5, 0.7, 100, 0.1, 20, 20)
    monkeypatch.setattr(
        "ashare_decision_support.monitor.sector_session.intraday.take_snapshot",
        lambda *args, **kwargs: SectorSnapshot(moment, [row]),
    )
    session = SectorSession(DAY, taxonomies={}, store_root=tmp_path)
    assert session.poll(moment).ok

    def fail(*args, **kwargs):
        raise OSError("feed down")

    monkeypatch.setattr(
        "ashare_decision_support.monitor.sector_session.intraday.take_snapshot", fail
    )
    assert session.poll(moment + timedelta(minutes=10)).ok is False
    events = read_events(DAY, until=CLOSE, notes=[])
    assert len(events) == 2
    assert events[0]["snapshot_ref"] == str(session.path.resolve())
    assert events[1]["tradable"] is False
    assert any("取数失败" in note for note in events[1]["notes"])


def test_post_close_automatically_saves_review_and_preopen_reuses_it(tmp_path):
    archive_plan(tmp_path, facts(), now=MORNING)
    bars = [{"date": str(DAY), "open": 100, "close": 101, "high": 102, "low": 99, "volume": 10000}]
    adapter = FakeAdapter({"600519.SH": bars})
    result = run_post_close(
        adapter,
        ["600519.SH"],
        tmp_path,
        trade_date=DAY,
        notifiers=[],
        use_limit_pool=False,
        use_trade_flow=False,
        use_lhb_seats=False,
    )
    assert adapter.calls == 1
    payload = read_json_object(tmp_path / "post_close_latest.json")
    assert result["daily_review_path"] == str(tmp_path / "daily-reviews" / f"{DAY}.json")
    assert payload["daily_review"]["original_plan_available"] is True
    assert "次日复查" in result["content"]
    next_day = run_pre_open(tmp_path / "post_close_latest.json", notifiers=[])
    assert next_day["next_checks"] == payload["daily_review"]["next_checks"]
    assert adapter.calls == 1


def test_failed_markdown_replacement_keeps_old_report_and_clears_receipt(tmp_path, monkeypatch):
    notifier = FileNotifier(tmp_path, stamp="20260803")
    assert notifier.send("before", "old report")
    path = notifier.last_path
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr("ashare_decision_support.state.store.os.replace", fail)
    assert notifier.send("after", "new report") is False
    assert path.read_bytes() == before
    assert notifier.last_path is None
