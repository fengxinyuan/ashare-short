import json
from datetime import date, datetime
from types import SimpleNamespace

from ashare_decision_support.ask import assemble
from ashare_decision_support.ask.context import _render_compact_block, _trim_morning
from ashare_decision_support.jobs.morning import _attach_live_quotes
from ashare_decision_support.jobs.morning_facts import metric_bar_freshness
from ashare_decision_support.jobs.morning_publish import _execution_banner
from ashare_decision_support.monitor.feed import VERDICT_CLOSED, assess_feed
from ashare_decision_support.monitor.triggers import (
    _breakout_confirmed,
    _entry_plan_detail,
    _entry_quality_blocked,
)
from ashare_decision_support.notify.render import render_feed_suspension


def test_lunch_without_minute_data_is_closed_not_a_feed_outage() -> None:
    health = assess_feed({}, 1, datetime(2026, 9, 14, 12, 1), in_session=False)

    assert health.verdict == VERDICT_CLOSED
    message = render_feed_suspension(health)
    assert "非连续交易时段" in message
    assert "数据源停更" not in message


def test_intraday_report_flags_prior_day_quotes_as_not_executable() -> None:
    message = _execution_banner(
        {
            "execution_ready": False,
            "execution_blocks": ["盘中日线截至 2026-09-11，未更新到今日", "全市场扫描缺失"],
            "data_date": "2026-09-11",
        }
    )

    assert "不可用于买入" in message
    assert "全市场扫描缺失" in message
    assert "2026-09-11" in message
    assert "盘中快照单独列示" in message
    assert _execution_banner({"execution_ready": True}) == ""


def test_ask_context_keeps_and_shows_execution_block() -> None:
    compact, _ = _trim_morning(
        {
            "trade_date": "2026-09-14",
            "data_date": "2026-09-11",
            "execution_ready": False,
            "execution_blocks": ["全市场扫描缺失"],
            "candidates": [{"symbol": "600519.SH"}],
        }
    )

    assert compact["execution_ready"] is False
    assert compact["data_date"] == "2026-09-11"
    assert "不可用于当前买入" in "\n".join(_render_compact_block("盘前主链路", compact))


def test_ask_compaction_keeps_late_missing_liquidity_fields() -> None:
    compact, _ = _trim_morning(
        {
            "data_notes": [f"普通来源说明 {index}" for index in range(30)]
            + ["3 只行情缺少必要的流通市值或成交额，本次只按已知字段过滤"],
        }
    )

    assert "缺少必要" in compact["data_notes"][0]


def test_ask_health_uses_morning_market_date_not_report_date(tmp_path) -> None:
    (tmp_path / "morning_latest.json").write_text(
        json.dumps(
            {
                "trade_date": "2026-09-14",
                "data_date": "2026-09-11",
                "execution_ready": False,
                "sectors": [],
            }
        ),
        encoding="utf-8",
    )

    ctx = assemble(datetime(2026, 9, 14, 12, 50), output_dir=tmp_path)
    health = next(item for item in ctx.health.blocks if item.name == "盘前主链路")

    assert health.trade_date == "2026-09-11"
    assert health.status == "上一交易日"


def test_live_entry_alert_uses_current_price_for_stop_distance_and_rrr() -> None:
    advice = SimpleNamespace(stop=20.51, entry_low=21.5, rrr=11.32, targets=[32.75], warnings=[])

    lines = _entry_plan_detail(advice, None, [32.75], current_price=22.63)

    assert any("按现价 22.63：-9.4%" in line for line in lines)
    assert "按现价盈亏比 4.77" in lines

    advice.targets = [24.0]
    assert _entry_quality_blocked(advice, 22.63)


def test_breakout_alert_does_not_chase_far_above_entry_band() -> None:
    advice = SimpleNamespace(
        entry_low=21.5,
        entry_high=21.5,
        stop=20.51,
        targets=[32.75],
        rrr=11.32,
        warnings=[],
        shortline=SimpleNamespace(action="breakout_follow"),
    )
    intraday = SimpleNamespace(
        last_price=22.63, session_high=22.63, session_low=21.0, volume_ratio=1.1
    )

    assert not _breakout_confirmed(advice, intraday)
    intraday.last_price = 21.8
    intraday.session_high = 21.8
    assert _breakout_confirmed(advice, intraday)


def test_metric_freshness_uses_constituent_dates_instead_of_sector_index() -> None:
    bars = {
        "A": [{"timestamp": "2026-09-11", "close": 10}],
        "B": [{"timestamp": "2026-09-11", "close": 11}],
        "C": [{"timestamp": "2026-09-14", "close": 12}],
        "D": [],
    }
    assert metric_bar_freshness(bars, date(2026, 9, 14)) == ("2026-09-11", 1 / 3)
    assert metric_bar_freshness({}, date(2026, 9, 14)) == (None, 0.0)


def test_live_scan_quote_is_attached_without_replacing_historical_close() -> None:
    candidates = [{"symbol": "600519.SH", "last_close": 1400.0}]
    scan = SimpleNamespace(
        ok=True,
        session_in_progress=True,
        as_of=datetime(2026, 9, 14, 13, 50),
        get=lambda symbol: SimpleNamespace(price=1412.5),
    )

    assert (
        _attach_live_quotes(candidates, scan, date(2026, 9, 14), now=datetime(2026, 9, 14, 13, 54))
        == 1
    )
    assert candidates[0]["last_close"] == 1400.0
    assert candidates[0]["live_price"] == 1412.5
    assert candidates[0]["live_quote_as_of"] == "2026-09-14T13:50:00"

    candidates = [{"symbol": "600519.SH", "last_close": 1400.0}]
    assert _attach_live_quotes(candidates, scan, date(2026, 9, 15)) == 0
    assert "live_price" not in candidates[0]
    assert (
        _attach_live_quotes(candidates, scan, date(2026, 9, 14), now=datetime(2026, 9, 14, 13, 56))
        == 0
    )
