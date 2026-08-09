"""State, monitoring, journal, scheduling, and the agent layer.

The tests that matter most here are the ones covering failure: a stale feed, a
symbol that chops across its stop, a data source that throws, a daemon that
restarts mid-day. Those are the paths an automated system actually takes.
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta
from datetime import time as clock_time

import pytest

from ashare_decision_support.advice.engine import StockAdvice
from ashare_decision_support.agents import AgentContext, run_agents
from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.journal import Journal, Recommendation, summarize, verify_pending
from ashare_decision_support.monitor import MonitorSession, TriggerEngine
from ashare_decision_support.monitor.feed import (
    VERDICT_CLOSED,
    VERDICT_DEGRADED,
    VERDICT_FRESH,
    VERDICT_STALE,
    assess_feed,
)
from ashare_decision_support.monitor.loop import INTERVAL_LADDER, run_monitor_loop
from ashare_decision_support.monitor.triggers import LEVEL_ACTION, LEVEL_INFO, LEVEL_WARN
from ashare_decision_support.notify.feishu import MESSAGE_URL, TOKEN_URL, FeishuNotifier, _sign
from ashare_decision_support.schedule import TradingCalendar, build_timetable, run_daemon
from ashare_decision_support.state import (
    AlertState,
    CandidatePool,
    DaemonState,
    WatchEntry,
    Watchlist,
)
from ashare_decision_support.symbols import bare_code, normalize_symbol

SESSION = date(2026, 8, 3)


# ---- fixtures ------------------------------------------------------------


def minute_bars(closes: list[float], day: date = SESSION, start_hour: int = 9, start_min: int = 30):
    origin = datetime(day.year, day.month, day.day, start_hour, start_min)
    return [
        {
            "symbol": "600519.SH",
            "timestamp": origin + timedelta(minutes=index),
            "open": close,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": 1000,
            "amount": close * 1000,
        }
        for index, close in enumerate(closes)
    ]


def daily_bars(count: int = 250, base: float = 90.0, symbol: str = "600519.SH"):
    origin = date(2025, 8, 1)
    return [
        {
            "symbol": symbol,
            "date": origin + timedelta(days=index),
            "open": base + index * 0.04,
            "high": base + index * 0.04 + 1,
            "low": base + index * 0.04 - 1,
            "close": base + index * 0.04,
            "volume": 100000,
            "amount": (base + index * 0.04) * 100000,
        }
        for index in range(count)
    ]


class StubDaily:
    def get_historical_bars(self, symbols, start, end):
        return {symbol: daily_bars(symbol=symbol) for symbol in symbols}

    def get_security_master(self, symbols):
        return {symbol: {"name": "测试股"} for symbol in symbols}


class ScriptedIntraday:
    def __init__(self, script):
        self.script = script
        self.index = 0

    def get_minute_bars(self, symbols, period="1", **kwargs):
        bars = self.script[min(self.index, len(self.script) - 1)]
        self.index += 1
        return (dict.fromkeys(symbols, bars), [])


RISE = [100 + 0.05 * i for i in range(40)]


def run_session(paths, levels=(LEVEL_ACTION, LEVEL_WARN)):
    script = [minute_bars(path) for path in paths]
    session = MonitorSession(
        StubDaily(), ScriptedIntraday(script), SESSION,
        levels=levels, alert_state=AlertState.load(SESSION),
    )
    session.prime(["600519.SH"])
    out = []
    for index in range(len(paths)):
        now = script[index][-1]["timestamp"] + timedelta(minutes=1)
        out.append(session.poll(["600519.SH"], [], now))
    return session, out


# ---- symbols -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("600519", "600519.SH"),
        ("600519.SH", "600519.SH"),
        ("sh600519", "600519.SH"),
        ("SH600519", "600519.SH"),
        ("000001", "000001.SZ"),
        ("300750.sz", "300750.SZ"),
        ("830799", "830799.BJ"),
    ],
)
def test_symbols_normalize_to_one_canonical_form(raw, expected) -> None:
    assert normalize_symbol(raw) == expected
    assert bare_code(raw) == expected.split(".")[0]


def test_unrecognisable_symbol_is_not_guessed_an_exchange() -> None:
    # A wrong suffix fetches a different company's bars, so passing it through
    # unchanged beats inventing one.
    assert normalize_symbol("NOTACODE") == "NOTACODE"


# ---- watchlist and candidate pool ----------------------------------------


def test_watchlist_persists_and_deduplicates_across_symbol_forms() -> None:
    book = Watchlist()
    book.add("600519", name="贵州茅台", note="核心仓")
    _, created = book.add("sh600519")

    assert created is False
    assert Watchlist().symbols() == ["600519.SH"]
    # A later add without a note must not erase the note typed earlier.
    assert Watchlist().get("600519.SH").note == "核心仓"


def test_watchlist_remove_and_clear() -> None:
    book = Watchlist()
    book.add("600519")
    book.add("000001")

    assert book.remove("600519.SH") is True
    assert book.remove("600519.SH") is False
    assert book.clear() == 1
    assert Watchlist().symbols() == []


def test_candidate_pool_expires_when_the_trade_date_rolls_over() -> None:
    CandidatePool(today=SESSION).replace(
        [WatchEntry(symbol="300750.SZ", source="sector-scan", score=78.0)], SESSION
    )

    assert CandidatePool(today=SESSION).symbols() == ["300750.SZ"]
    stale = CandidatePool(today=SESSION + timedelta(days=1))
    assert stale.symbols() == []
    assert stale.stale is True


def test_candidate_pool_preserves_symbol_metadata() -> None:
    CandidatePool(today=SESSION).replace(
        [
            WatchEntry(
                symbol="300750.SZ",
                name="宁德时代",
                sector="电力设备",
                industries=["电力设备", "电池", "锂电池"],
                industry="电力设备",
                industry_leaf="锂电池",
                industry_path="电力设备·电池·锂电池",
                concepts=["固态电池", "储能"],
                source="sector-scan",
                score=78.0,
            )
        ],
        SESSION,
    )

    entry = CandidatePool(today=SESSION).get("300750.SZ")
    assert entry is not None
    assert entry.industry_path == "电力设备·电池·锂电池"
    assert entry.concepts == ["固态电池", "储能"]


def test_alert_state_survives_a_reload() -> None:
    state = AlertState.load(SESSION)
    moment = datetime(2026, 8, 3, 10, 32)
    state.mark_fired("600519.SH", "entry_zone", moment)
    state.set_state("600519.SH", {"vs_vwap": "above"})
    state.pin("600519.SH", stop=99.5, targets=[105.0], at=moment)
    state.save()

    reloaded = AlertState.load(SESSION)
    assert reloaded.last_fired("600519", "entry_zone") == moment
    assert reloaded.fire_count("600519.SH", "entry_zone") == 1
    assert reloaded.state_of("600519") == {"vs_vwap": "above"}
    assert reloaded.pinned_of("600519.SH")["stop"] == 99.5


def test_corrupt_state_file_degrades_instead_of_crashing(tmp_path) -> None:
    path = tmp_path / "watchlist.json"
    path.write_text("{not json", encoding="utf-8")
    # Re-firing every alert of the day is the cost of a parse error taking down
    # the load, so a corrupt file has to read as empty.
    assert Watchlist(path=path).symbols() == []


# ---- freshness gate ------------------------------------------------------


def test_fresh_feed_is_usable() -> None:
    bars = minute_bars(RISE)
    now = bars[-1]["timestamp"] + timedelta(minutes=1)
    health = assess_feed({"a": bars}, 1, now)

    assert health.verdict == VERDICT_FRESH
    assert health.usable is True


def test_lagging_feed_is_degraded_but_still_usable() -> None:
    bars = minute_bars(RISE)
    now = bars[-1]["timestamp"] + timedelta(minutes=9)
    health = assess_feed({"a": bars}, 1, now)

    assert health.verdict == VERDICT_DEGRADED
    assert health.usable is True


def test_previous_session_data_is_never_treated_as_live() -> None:
    bars = minute_bars(RISE, day=SESSION - timedelta(days=1))
    now = datetime(2026, 8, 3, 10, 0)
    health = assess_feed({"a": bars}, 1, now)

    assert health.verdict == VERDICT_STALE
    assert health.usable is False


def test_a_frozen_feed_is_caught_by_non_advancement_not_only_by_lag() -> None:
    bars = minute_bars(RISE)
    now = bars[-1]["timestamp"] + timedelta(minutes=1)
    health = assess_feed({"a": bars}, 1, now, previous_latest=bars[-1]["timestamp"])

    assert health.verdict == VERDICT_STALE
    assert "未推进" in health.reason


def test_outside_session_the_gate_says_closed_rather_than_stale() -> None:
    bars = minute_bars(RISE)
    now = bars[-1]["timestamp"] + timedelta(minutes=1)
    health = assess_feed({"a": bars}, 1, now, in_session=False)

    assert health.verdict == VERDICT_CLOSED
    assert health.usable is False


def test_median_lag_is_used_so_one_prompt_symbol_cannot_vouch_for_the_feed() -> None:
    now = datetime(2026, 8, 3, 11, 0)
    prompt = minute_bars(RISE, start_hour=10, start_min=19)
    stale = [minute_bars(RISE, start_hour=9, start_min=30) for _ in range(3)]
    health = assess_feed(
        {"fresh": prompt, "a": stale[0], "b": stale[1], "c": stale[2]}, 4, now
    )

    assert health.verdict == VERDICT_STALE


def test_a_stale_feed_produces_no_alerts_at_all() -> None:
    script = [minute_bars(RISE, day=SESSION - timedelta(days=1))]
    session = MonitorSession(
        StubDaily(), ScriptedIntraday(script), SESSION, alert_state=AlertState.load(SESSION)
    )
    session.prime(["600519.SH"])
    result = session.poll(["600519.SH"], [], datetime(2026, 8, 3, 10, 0))

    assert result.health.usable is False
    assert result.alerts == []


# ---- triggers ------------------------------------------------------------


def test_first_sighting_establishes_a_baseline_without_alerting() -> None:
    _, results = run_session([RISE])
    assert results[0].alerts == []


def test_losing_vwap_fires_once_on_the_crossing() -> None:
    _, results = run_session([RISE, [*RISE, 101.2, 100.9, 100.6]])
    triggers = [alert.trigger for alert in results[1].alerts]

    assert "vwap_lost" in triggers
    assert "scenario_shift" in triggers


def test_stop_is_pinned_so_a_falling_price_can_actually_break_it() -> None:
    """A stop recomputed from the live session low sits below it by
    construction — price can never break it and the alert never fires."""
    session, results = run_session(
        [
            RISE[:10],                                       # too early to arm
            RISE,                                            # arm here
            [*RISE, 101.2, 100.9, 100.6],                    # lose vwap
            [*RISE, 101.2, 100.9, 100.6, 99.0, 98.0],        # break the pinned stop
            [*RISE, 101.2, 100.9, 100.6, 99.0, 98.0, 97.0],  # deeper, must not re-fire
        ]
    )

    assert session.state.state_of("600519.SH")["armed"] is True
    pinned = session.state.pinned_of("600519.SH")
    assert pinned is not None and pinned["stop"] is not None

    breaks = [a for result in results for a in result.alerts if a.trigger == "stop_break"]
    assert len(breaks) == 1
    assert breaks[0].level == LEVEL_ACTION
    assert results[4].alerts == []


def test_levels_are_not_published_before_the_session_has_a_shape() -> None:
    session, results = run_session([RISE[:10]])
    state = session.state.state_of("600519.SH")

    assert state["armed"] is False
    assert session.state.pinned_of("600519.SH") is None
    assert results[0].alerts == []


def test_info_triggers_are_off_under_the_default_levels() -> None:
    _, results = run_session([RISE, [*RISE, 101.2, 100.6], [*RISE, 101.2, 100.6, 103.0]])
    levels = {alert.level for result in results for alert in result.alerts}

    assert LEVEL_INFO not in levels


def test_info_triggers_appear_when_the_level_is_enabled() -> None:
    _, results = run_session(
        [RISE, [*RISE, 101.2, 100.6], [*RISE, 101.2, 100.6, 103.0]],
        levels=(LEVEL_ACTION, LEVEL_WARN, LEVEL_INFO),
    )
    triggers = {alert.trigger for result in results for alert in result.alerts}

    assert "vwap_regain" in triggers


def test_candidate_pool_names_only_interrupt_at_action_level() -> None:
    engine = TriggerEngine()
    state = AlertState.load(SESSION)
    advice = StockAdvice(symbol="600519.SH", last_price=100.6, entry_low=99.0, entry_high=101.0, stop=95.0)

    from ashare_decision_support.signals.intraday import IntradayAnalyzer

    first = IntradayAnalyzer(today=SESSION).analyze("600519.SH", minute_bars(RISE))
    engine.evaluate(advice, first, state, datetime(2026, 8, 3, 10, 10), watchlist=False)

    second = IntradayAnalyzer(today=SESSION).analyze("600519.SH", minute_bars([*RISE, 100.6]))
    advice.last_price = second.last_price
    alerts = engine.evaluate(advice, second, state, datetime(2026, 8, 3, 10, 30), watchlist=False)

    assert all(alert.level == LEVEL_ACTION for alert in alerts)


# ---- loop ----------------------------------------------------------------


class FlakyIntraday:
    """Fails every symbol for the first `failures` passes, then recovers."""

    def __init__(self, failures: int):
        self.failures = failures
        self.passes = 0

    def get_minute_bars(self, symbols, period="1", **kwargs):
        self.passes += 1
        if self.passes <= self.failures:
            return {}, ["all symbols failed"]
        bars = minute_bars(RISE)
        return dict.fromkeys(symbols, bars), []


class RecordingNotifier:
    from ashare_decision_support.notify.base import NotifierCapabilities

    capabilities = NotifierCapabilities(channel="recording", markdown=True, max_bytes=100_000)

    def __init__(self):
        self.messages = []

    def send(self, title, content):
        self.messages.append((title, content))
        return True


def test_loop_steps_the_interval_down_when_the_feed_keeps_failing() -> None:
    session = MonitorSession(
        StubDaily(), FlakyIntraday(failures=99), SESSION, alert_state=AlertState.load(SESSION)
    )
    session.prime(["600519.SH"])
    stats = run_monitor_loop(
        session=session,
        watch_symbols=lambda: ["600519.SH"],
        candidate_symbols=list,
        notifiers=[],
        now_fn=lambda: datetime(2026, 8, 3, 10, 0),
        sleep=lambda seconds: None,
        max_passes=8,
    )

    assert stats.bad_passes >= 4
    assert stats.interval_seconds > INTERVAL_LADDER[0]
    assert any("轮询间隔" in note for note in stats.notes)


def test_feed_suspension_is_announced_once_not_every_pass() -> None:
    notifier = RecordingNotifier()
    session = MonitorSession(
        StubDaily(), FlakyIntraday(failures=99), SESSION, alert_state=AlertState.load(SESSION)
    )
    session.prime(["600519.SH"])
    run_monitor_loop(
        session=session,
        watch_symbols=lambda: ["600519.SH"],
        candidate_symbols=list,
        notifiers=[notifier],
        now_fn=lambda: datetime(2026, 8, 3, 10, 0),
        sleep=lambda seconds: None,
        max_passes=6,
    )
    suspensions = [msg for msg in notifier.messages if "挂起" in msg[0]]

    assert len(suspensions) == 1


def test_a_pass_that_raises_does_not_kill_the_loop() -> None:
    class Exploding:
        def get_minute_bars(self, *args, **kwargs):
            raise RuntimeError("network gone")

    session = MonitorSession(
        StubDaily(), Exploding(), SESSION, alert_state=AlertState.load(SESSION)
    )
    stats = run_monitor_loop(
        session=session,
        watch_symbols=lambda: ["600519.SH"],
        candidate_symbols=list,
        notifiers=[],
        now_fn=lambda: datetime(2026, 8, 3, 10, 0),
        sleep=lambda seconds: None,
        max_passes=3,
    )

    assert stats.passes == 3
    assert stats.bad_passes == 3


# ---- journal -------------------------------------------------------------


def adjusted_series(multipliers: list[float], base: float = 40.0, start: date = date(2026, 7, 10)):
    """A forward-adjusted series whose absolute level is unrelated to the raw
    price the recommendation was quoted at."""
    return [
        {
            "symbol": "600519.SH",
            "date": start + timedelta(days=index),
            "open": base * mult,
            "high": base * mult * 1.005,
            "low": base * mult * 0.995,
            "close": base * mult,
            "volume": 1,
            "amount": base * mult,
        }
        for index, mult in enumerate(multipliers)
    ]


class StubVerifier:
    def __init__(self, multipliers):
        self.multipliers = multipliers

    def get_historical_bars(self, symbols, start, end):
        return {symbol: adjusted_series(self.multipliers) for symbol in symbols}


def make_recommendation() -> Recommendation:
    advice = StockAdvice(
        symbol="600519.SH", name="测试股", score=76.0, last_price=100.0,
        entry_low=98.0, entry_high=100.0, stop=96.0, targets=[105.0], rrr=1.75,
        trend_state="上升趋势",
    )
    return Recommendation.from_advice(advice, date(2026, 7, 20), source="morning", sector="食品饮料")


def test_recommendation_stores_relative_levels_for_adjustment_safe_verification() -> None:
    record = make_recommendation()

    assert record.entry_low_pct == pytest.approx(-0.02)
    assert record.stop_pct == pytest.approx(-0.04)
    assert record.target_pcts == [pytest.approx(0.05)]


def test_recommendation_without_a_reference_price_is_refused() -> None:
    # No reference price means no relative levels, and a record that cannot be
    # verified only pollutes the statistics.
    assert Recommendation.from_advice(
        StockAdvice(symbol="600519.SH", last_price=None), date(2026, 7, 20), source="morning"
    ) is None


def test_verification_works_across_the_raw_to_adjusted_price_gap() -> None:
    journal = Journal()
    journal.record(make_recommendation())
    path = [1.0] * 10 + [1.0, 1.02, 1.03, 1.06, 0.99, 0.97]

    result = verify_pending(journal, StubVerifier(path), today=date(2026, 8, 1))
    outcome = result["outcomes"][0]

    assert result["resolved"] == 1
    assert outcome["entered"] is True
    assert outcome["first_hit"] == "target"
    assert outcome["stop_hit"] is False
    assert outcome["mfe_pct"] > 0.05


def test_verification_is_idempotent() -> None:
    journal = Journal()
    journal.record(make_recommendation())
    path = [1.0] * 10 + [1.0, 1.02, 1.03, 1.06, 0.99, 0.97]

    verify_pending(journal, StubVerifier(path), today=date(2026, 8, 1))
    again = verify_pending(journal, StubVerifier(path), today=date(2026, 8, 1))

    assert again["resolved"] == 0


def test_a_stop_hit_before_the_target_is_recorded_as_such() -> None:
    journal = Journal()
    journal.record(make_recommendation())
    path = [1.0] * 10 + [1.0, 0.94, 0.95, 1.08, 1.02, 1.01]

    result = verify_pending(journal, StubVerifier(path), today=date(2026, 8, 1))

    assert result["outcomes"][0]["first_hit"] == "stop"


def test_statistics_flag_small_buckets_rather_than_hiding_them() -> None:
    outcomes = [
        {"kind": "outcome", "score": 75, "entered": True, "first_hit": "target",
         "mfe_pct": 0.06, "mae_pct": -0.02, "close_pct": 0.03, "sector": "食品饮料"}
    ]
    stats = summarize(outcomes)

    assert stats["samples"] == 1
    assert stats["model_ready"] is False
    assert stats["model_ready_gap"] == 199
    assert stats["overall"]["trusted"] is False
    # Reported anyway — "too few samples" is part of the finding.
    assert stats["overall"]["median_mfe_pct"] == 0.06


# ---- agents --------------------------------------------------------------


def test_one_failing_agent_does_not_take_down_the_others() -> None:
    class Good:
        name = "good"

        def run(self, ctx):
            return {"value": 1}, ["fine"]

    class Boom:
        name = "boom"

        def run(self, ctx):
            raise ValueError("upstream 500")

    findings = run_agents([Good(), Boom()], AgentContext(trade_date=SESSION))

    assert findings["good"].ok is True
    assert findings["boom"].ok is False
    assert "ValueError" in findings["boom"].error


class FakeLlm:
    configured = True

    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages):
        return self.replies.pop(0)


DEBATE_FACTS = {"symbol": "600519.SH", "last_price": 1337.53, "volume_ratio": 3.2}


def test_debate_roles_pass_when_every_number_traces_to_the_facts() -> None:
    panel = DebatePanel(client=FakeLlm([
        "- 现价 1337.53，量比 3.2 倍",
        "- 量比 3.2 倍也可能是情绪透支",
        "- 分歧在于 1337.53 能否守住",
    ]))
    result = panel.run(DEBATE_FACTS)

    assert [item.role for item in result.arguments] == ["bull", "bear", "judge"]
    assert all(not item.used_fallback for item in result.arguments)


def test_a_role_that_invents_numbers_is_discarded_whole() -> None:
    panel = DebatePanel(client=FakeLlm([
        "- 上涨概率约 68%，目标看 1500.00",
        "- 量比 3.2 倍偏高",
        "- 分歧在量比 3.2",
    ]))
    result = panel.run(DEBATE_FACTS)
    bull = result.arguments[0]

    assert bull.used_fallback is True
    assert bull.markdown == ""
    # The other roles survive — one bad argument does not void the panel.
    assert result.arguments[1].used_fallback is False


def test_debate_is_skipped_without_credentials() -> None:
    class Unconfigured:
        configured = False

    result = DebatePanel(client=Unconfigured()).run(DEBATE_FACTS)

    assert result.arguments == []
    assert any("跳过多空辩论" in note for note in result.notes)


# ---- feishu --------------------------------------------------------------


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_feishu_treats_a_non_zero_code_as_failure_despite_http_200() -> None:
    notifier = FeishuNotifier(
        webhook_url="https://open.feishu.cn/x",
        opener=lambda request, timeout=None: Response(b'{"code":19021,"msg":"sign match fail"}'),
    )

    assert notifier.send("t", "c") is False
    assert "19021" in notifier.last_error


def test_feishu_never_raises_on_a_network_error() -> None:
    def explode(request, timeout=None):
        raise OSError("connection refused")

    assert FeishuNotifier(webhook_url="https://x", opener=explode).send("t", "c") is False


def test_feishu_chunks_by_bytes_not_characters() -> None:
    sent = []

    def capture(request, timeout=None):
        sent.append(json.loads(request.data.decode("utf-8")))
        return Response(b'{"code":0}')

    notifier = FeishuNotifier(webhook_url="https://x", opener=capture)
    long_chinese = "\n".join("中文行内容测试" * 40 for _ in range(60))

    assert notifier.send("盘后简报", long_chinese) is True
    assert len(sent) > 1
    assert all(
        len(card["card"]["elements"][0]["content"].encode("utf-8")) <= notifier.capabilities.max_bytes
        for card in sent
    )


def test_feishu_signs_the_timestamp_not_the_payload() -> None:
    sent = []

    def capture(request, timeout=None):
        sent.append(json.loads(request.data.decode("utf-8")))
        return Response(b'{"code":0}')

    FeishuNotifier(webhook_url="https://x", secret="s3cr3t", opener=capture).send("t", "c")

    assert sent[0]["sign"] == _sign(sent[0]["timestamp"], "s3cr3t")


def test_feishu_reports_missing_configuration_instead_of_failing_silently() -> None:
    notifier = FeishuNotifier(webhook_url=None)

    assert notifier.send("t", "c") is False
    assert "FEISHU_WEBHOOK_URL" in notifier.last_error


def test_feishu_can_send_via_app_credentials() -> None:
    sent = []

    def capture(request, timeout=None):
        sent.append((request.full_url, json.loads(request.data.decode("utf-8")), dict(request.header_items())))
        if request.full_url == TOKEN_URL:
            return Response(b'{"code":0,"tenant_access_token":"tenant-token","expire":7200}')
        return Response(b'{"code":0,"data":{"message_id":"om_x"}}')

    notifier = FeishuNotifier(
        app_id="cli_x",
        app_secret="app-secret",
        receive_id_type="chat_id",
        receive_id="oc_x",
        opener=capture,
    )

    assert notifier.send("盘中告警", "测试正文") is True
    assert sent[0][0] == TOKEN_URL
    assert sent[0][1] == {"app_id": "cli_x", "app_secret": "app-secret"}
    assert sent[1][0] == f"{MESSAGE_URL}?receive_id_type=chat_id"
    assert sent[1][1]["receive_id"] == "oc_x"
    assert sent[1][1]["msg_type"] == "interactive"
    card = json.loads(sent[1][1]["content"])
    assert card["header"]["title"]["content"] == "盘中告警"
    assert sent[1][2]["Authorization"] == "Bearer tenant-token"


def test_feishu_app_mode_reports_incomplete_configuration() -> None:
    notifier = FeishuNotifier(app_id="cli_x", app_secret="app-secret")

    assert notifier.send("t", "c") is False
    assert "FEISHU_RECEIVE_ID" in notifier.last_error


def test_feishu_app_mode_treats_token_errors_as_failure() -> None:
    def capture(request, timeout=None):
        return Response(b'{"code":99991663,"msg":"invalid app_secret"}')

    notifier = FeishuNotifier(
        app_id="cli_x",
        app_secret="bad-secret",
        receive_id_type="chat_id",
        receive_id="oc_x",
        opener=capture,
    )

    assert notifier.send("t", "c") is False
    assert "token 获取失败" in notifier.last_error


# ---- scheduling ----------------------------------------------------------


def timetable(runners):
    return build_timetable(runners)


def test_daemon_runs_each_task_once_and_isolates_failures() -> None:
    calls = []
    tasks = timetable({
        "news": lambda: calls.append("news") or {"ok": True},
        "morning": lambda: calls.append("morning") or {"ok": True},
        "preopen": _raise,
        "postclose": lambda: calls.append("postclose") or {"ok": True},
    })
    report = run_daemon(
        tasks, calendar=_calendar(), day=SESSION,
        now_fn=lambda: datetime(2026, 8, 3, 16, 0), sleep=lambda s: None, max_ticks=30,
    )

    assert report.ran == ["news", "morning", "postclose"]
    assert "preopen" in report.failed
    assert calls == ["news", "morning", "postclose"]


def test_a_failing_task_is_retried_but_not_forever() -> None:
    attempts = []
    tasks = timetable({"morning": lambda: attempts.append(1) or _raise()})
    run_daemon(
        tasks, calendar=_calendar(), day=SESSION,
        now_fn=lambda: datetime(2026, 8, 3, 16, 0), sleep=lambda s: None, max_ticks=30,
    )

    from ashare_decision_support.schedule import MAX_ATTEMPTS

    assert len(attempts) == MAX_ATTEMPTS


def test_a_restarted_daemon_does_not_re_run_completed_tasks() -> None:
    calls = []
    tasks = timetable({"morning": lambda: calls.append("morning") or {"ok": True}})
    def clock():
        return datetime(2026, 8, 3, 16, 0)

    run_daemon(tasks, calendar=_calendar(), day=SESSION, now_fn=clock, sleep=lambda s: None, max_ticks=5)
    run_daemon(tasks, calendar=_calendar(), day=SESSION, now_fn=clock, sleep=lambda s: None, max_ticks=5)

    assert calls == ["morning"]


def test_task_summaries_are_trimmed_to_scalars() -> None:
    tasks = timetable({"morning": lambda: {"ok": True, "content": "x" * 50_000}})
    run_daemon(
        tasks, calendar=_calendar(), day=SESSION,
        now_fn=lambda: datetime(2026, 8, 3, 16, 0), sleep=lambda s: None, max_ticks=5,
    )

    assert DaemonState.load(SESSION).tasks["morning"]["summary"] == {"ok": True}


def test_non_trading_days_produce_nothing() -> None:
    calls = []
    tasks = timetable({"morning": lambda: calls.append("morning")})
    def clock():
        return datetime(2026, 8, 3, 16, 0)

    weekend = run_daemon(tasks, calendar=_calendar(), day=date(2026, 8, 2), now_fn=clock, sleep=lambda s: None, max_ticks=5)
    holiday = run_daemon(tasks, calendar=_calendar(), day=date(2026, 10, 1), now_fn=clock, sleep=lambda s: None, max_ticks=5)

    assert calls == []
    assert "周末" in weekend.notes[0]
    assert "节假日" in holiday.notes[0]


def test_a_make_up_workday_on_a_weekend_still_trades() -> None:
    calendar = _calendar()

    assert calendar.is_trading_day(date(2026, 8, 1)) is True
    assert "调休" in calendar.explain(date(2026, 8, 1))


def test_an_unconfigured_calendar_says_so_rather_than_implying_certainty() -> None:
    calendar = TradingCalendar()

    assert calendar.is_trading_day(SESSION) is True
    assert "未经假期校验" in calendar.explain(SESSION)


def test_timetable_is_ordered_by_time_of_day() -> None:
    tasks = timetable({"postclose": dict, "news": dict, "morning": dict})
    ordered = sorted(tasks, key=lambda task: task.at)

    assert [task.key for task in ordered] == ["news", "morning", "postclose"]
    assert ordered[0].at == clock_time(8, 0)


def _raise():
    raise RuntimeError("data source down")


def _calendar() -> TradingCalendar:
    calendar = TradingCalendar()
    calendar.save(holidays=["2026-10-01"], workdays=["2026-08-01"])
    return calendar


# ---- scoring scale -------------------------------------------------------


def _snapshot(**overrides):
    from ashare_decision_support.models.enums import TrendState
    from ashare_decision_support.models.schemas import DailyTechnicalSnapshot

    defaults = {
        "symbol": "600519.SH", "last_open": 99.0, "last_high": 101.0, "last_low": 98.0,
        "last_close": 100.0, "trend_state": TrendState.UPTREND,
        "bar_count": 250, "volume_ratio_20d": 1.5, "range_position_60d": 0.5,
        "stop_reference": 95.0, "target_reference": 115.0,
        "explanation": "fixture", "source_ts": datetime(2026, 8, 3, 15, 0),
    }
    defaults.update(overrides)
    return DailyTechnicalSnapshot(**defaults)


def _wyckoff(**overrides):
    from ashare_decision_support.models.enums import WyckoffBias
    from ashare_decision_support.models.schemas import WyckoffSignal

    defaults = {
        "symbol": "600519.SH", "bias": WyckoffBias.MARKUP, "score": 100.0,
        "close_position": 0.8, "short_return_pct": 0.05, "medium_return_pct": 0.12,
        "volume_ratio": 1.5, "compression_ratio": 0.4,
        "spring": True, "sign_of_strength": True, "last_point_of_support": True, "upthrust": False,
        "explanation": "fixture", "source_ts": datetime(2026, 8, 3, 15, 0),
    }
    defaults.update(overrides)
    return WyckoffSignal(**defaults)


def _news(**overrides):
    from ashare_decision_support.news.base import SymbolSentiment

    defaults = {
        "symbol": "600519.SH", "polarity": 1.0, "intensity": 1.0, "doc_count": 4,
        "headlines": ("业绩预增",), "bullish_hits": ("业绩预增", "中标"), "bearish_hits": (),
    }
    defaults.update(overrides)
    return SymbolSentiment(**defaults)


def _dip(**overrides):
    from ashare_decision_support.signals.dipbuy import DipFeatures

    defaults = {
        "ret_20d": 0.08, "ret_60d": 0.2, "above_ma20": True, "dist_to_ma20_pct": 0.03,
        "pullback_pct": -0.06, "atr_pct": 0.02, "pullback_atr": 3.0, "pullback_band": "ideal",
        "down_days": 4, "dist_to_support_pct": 0.01,
        "volume_contraction": 0.5, "shrinking": True,
    }
    defaults.update(overrides)
    return DipFeatures(**defaults)


def test_scores_do_not_all_saturate_at_the_top_of_the_scale() -> None:
    """A ranking that ties every decent name at 100.0 cannot rank, and the
    candidate picked inside a sector becomes an artefact of iteration order."""
    from ashare_decision_support.models.enums import WyckoffBias
    from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput

    provider = RuleBasedScoreProvider()
    best = provider.score(ScoreInput(symbol="A", bars=[], snapshot=_snapshot(), wyckoff=_wyckoff()))
    good = provider.score(
        ScoreInput(
            symbol="B", bars=[], snapshot=_snapshot(),
            wyckoff=_wyckoff(bias=WyckoffBias.ACCUMULATION, score=60.0, spring=False),
        )
    )

    assert best.score > good.score
    assert good.score < 100.0
    assert best.score <= 100.0


def test_the_perfect_case_reaches_but_does_not_exceed_the_ceiling() -> None:
    """满分样例现在描述的是**理想低吸形态**，不是放量突破。

    口径转向低吸之后能拿满分的必须同时满足：趋势在（UPTREND）、处在吸筹末端
    （ACCUMULATION 权重高于 MARKUP，主升段是追高位置）、回调到位且缩量。
    放量突破拿不到满分是**设计意图**，不是回归。
    """
    from ashare_decision_support.models.enums import WyckoffBias
    from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput
    from ashare_decision_support.scoring.rule_based import MAX_COMPONENT_TOTAL

    provider = RuleBasedScoreProvider()
    result = provider.score(
        ScoreInput(
            symbol="A", bars=[], snapshot=_snapshot(volume_ratio_20d=0.5),
            wyckoff=_wyckoff(bias=WyckoffBias.ACCUMULATION), news=_news(), dip=_dip(),
        )
    )

    assert result.score == pytest.approx(100.0)
    assert pytest.approx(85.0) == MAX_COMPONENT_TOTAL


def test_missing_news_is_not_read_as_bad_news() -> None:
    """`news=None` means "not collected". Scoring it as a negative would
    penalise every name on a day the news feed is down."""
    from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput

    provider = RuleBasedScoreProvider()
    absent = provider.score(
        ScoreInput(symbol="A", bars=[], snapshot=_snapshot(), wyckoff=_wyckoff())
    )
    neutral = provider.score(
        ScoreInput(
            symbol="A", bars=[], snapshot=_snapshot(), wyckoff=_wyckoff(),
            news=_news(polarity=0.0, intensity=0.0, bullish_hits=(), doc_count=3),
        )
    )
    bullish = provider.score(
        ScoreInput(symbol="A", bars=[], snapshot=_snapshot(), wyckoff=_wyckoff(), news=_news())
    )

    assert absent.score == pytest.approx(neutral.score)
    assert bullish.score > absent.score


def test_bearish_stock_news_lowers_the_score_and_raises_a_warning() -> None:
    from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput

    provider = RuleBasedScoreProvider()
    clean = provider.score(
        ScoreInput(symbol="A", bars=[], snapshot=_snapshot(), wyckoff=_wyckoff())
    )
    flagged = provider.score(
        ScoreInput(
            symbol="A", bars=[], snapshot=_snapshot(), wyckoff=_wyckoff(),
            news=_news(polarity=-1.0, bullish_hits=(), bearish_hits=("减持", "立案调查")),
        )
    )

    assert flagged.score < clean.score
    assert flagged.components["news"] < 0
    assert any("舆情偏空" in item for item in flagged.warnings)


def test_news_cannot_outrank_price_action() -> None:
    """The cap exists so a keyword match never promotes a broken name above a
    healthy one. If it can, the cap is set wrong."""
    from ashare_decision_support.models.enums import TrendState, WyckoffBias
    from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput

    provider = RuleBasedScoreProvider()
    hyped_but_broken = provider.score(
        ScoreInput(
            symbol="BAD", bars=[],
            snapshot=_snapshot(trend_state=TrendState.DOWNTREND, volume_ratio_20d=0.4),
            wyckoff=_wyckoff(bias=WyckoffBias.MARKDOWN, score=-80.0, spring=False,
                             sign_of_strength=False, last_point_of_support=False),
            news=_news(),
        )
    )
    quiet_but_healthy = provider.score(
        ScoreInput(symbol="GOOD", bars=[], snapshot=_snapshot(), wyckoff=_wyckoff())
    )

    assert quiet_but_healthy.score > hyped_but_broken.score


def test_a_broken_name_scores_below_neutral() -> None:
    from ashare_decision_support.models.enums import TrendState, WyckoffBias
    from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput

    result = RuleBasedScoreProvider().score(
        ScoreInput(
            symbol="A", bars=[],
            snapshot=_snapshot(
                trend_state=TrendState.DOWNTREND, volume_ratio_20d=0.4,
                range_position_60d=0.05, stop_reference=99.0, target_reference=101.0,
            ),
            wyckoff=_wyckoff(
                bias=WyckoffBias.MARKDOWN, score=-80.0, spring=False,
                sign_of_strength=False, last_point_of_support=False, upthrust=True,
            ),
        )
    )

    assert result.score < 50.0


# ---- position sizing -----------------------------------------------------


def test_overheat_haircut_is_continuous_not_a_cliff() -> None:
    """A step function here reproduces the exact defect this project rejected
    in the original plan's position curve: readings either side of a threshold
    get sizes far apart, and a market oscillating across it flips the
    recommendation all session."""
    from ashare_decision_support.sectors.predict import overheat_factor

    below = overheat_factor(0.8499, 0.0599)
    above = overheat_factor(0.8501, 0.0601)

    assert abs(below - above) < 0.01


def test_overheat_haircut_needs_both_readings_elevated() -> None:
    from ashare_decision_support.sectors.predict import overheat_factor

    # Broad but flat, or sharp but narrow — neither is a late-stage melt-up.
    assert overheat_factor(0.95, 0.01) == 1.0
    assert overheat_factor(0.40, 0.15) == 1.0
    assert overheat_factor(0.95, 0.15) < 1.0


def test_overheat_haircut_is_bounded() -> None:
    from ashare_decision_support.sectors.predict import OVERHEAT_MAX_HAIRCUT, overheat_factor

    assert overheat_factor(1.0, 1.0) == pytest.approx(1.0 - OVERHEAT_MAX_HAIRCUT)
    assert overheat_factor(None, None) == 1.0


def test_market_outlook_refuses_a_direction_on_thin_coverage() -> None:
    from ashare_decision_support.news import LexiconSentiment
    from ashare_decision_support.sectors.base import SectorMetrics
    from ashare_decision_support.sectors.predict import SectorPredictor

    thin = {
        f"s{i}": SectorMetrics(
            name=f"s{i}", constituents=300, covered=2, breadth=0.9,
            ret_1d=0.02, ret_5d=0.05, above_ma20=0.9, volume_ratio=1.1, strong_count=0,
        )
        for i in range(6)
    }
    outlook = SectorPredictor(LexiconSentiment()).market_outlook([], thin)

    # "Could not measure" must not read the same as "measured, and it is balanced".
    assert outlook.direction == "数据不足"
    assert outlook.trusted is False
    assert outlook.breadth is None


def test_market_outlook_reads_direction_from_measured_breadth() -> None:
    from ashare_decision_support.news import LexiconSentiment
    from ashare_decision_support.sectors.base import SectorMetrics
    from ashare_decision_support.sectors.predict import SectorPredictor

    def metrics(breadth, ret_1d, ret_5d, ma20):
        return {
            f"s{i}": SectorMetrics(
                name=f"s{i}", constituents=120, covered=100, breadth=breadth,
                ret_1d=ret_1d, ret_5d=ret_5d, above_ma20=ma20, volume_ratio=1.1, strong_count=0,
            )
            for i in range(6)
        }

    predictor = SectorPredictor(LexiconSentiment())
    strong = predictor.market_outlook([], metrics(0.72, 0.012, 0.035, 0.68))
    weak = predictor.market_outlook([], metrics(0.28, -0.011, -0.03, 0.31))

    assert strong.direction == "偏多" and strong.trusted is True
    assert weak.direction == "偏空" and weak.trusted is True
    assert strong.score > weak.score


# ---- batch snapshots with depth -----------------------------------------

SINA_LINE = (
    'var hq_str_sh600519="贵州茅台,1330.030,1361.760,1350.600,1355.000,1329.000,'
    '1350.600,1350.670,'
    '84,113322,'
    '84,1350.600,100,1350.550,500,1350.500,100,1350.400,100,1350.350,'
    '3100,1350.670,100,1350.700,1100,1350.880,300,1350.980,600,1351.000,'
    '2026-07-31,15:34:59,00";'
)

# Captured verbatim from qt.gtimg.cn — hand-built fixtures put the 88
# tilde-separated fields out of alignment, which is the exact class of bug
# the parser has to get right.
TENCENT_LINE = (
    'v_sz300750="51~宁德时代~300750~395.30~401.88~395.55~394149~188920~205229~395.30~6~395.29~3~395.28~5~395.26~14~395.25~1~395.60~22~395.66~2~395.74~2~395.80~2~395.81~1~~20260731161442~-6.58~-1.64~400.20~389.00~395.30/394149/15602984033~394149~1560298~0.93~21.52~~400.20~389.00~2.79~16840.10~18289.17~4.82~482.26~321.50~1.01~0~395.86~21.13~25.33~~~1.17~1560298.4033~252.9920~64~ A A~GP-A-CYB~9.70~3.21~1.97~22.41~8.03~468.75~249.81~9.81~4.03~-12.47~4260081019~4626654919~0.00~6.25~4260081019~~~53.99~-0.41~~CNY~0~~396.00~-115~";'
)


class StubHttp:
    def __init__(self, body: str):
        self.body = body
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return Response(self.body.encode("gbk", errors="replace"))


def test_sina_snapshot_carries_a_full_five_level_book() -> None:
    from ashare_decision_support.adapters.snapshot import SnapshotAdapter

    adapter = SnapshotAdapter(opener=StubHttp(SINA_LINE))
    snapshots, notes = adapter.get_snapshots(["600519"])
    snap = snapshots["600519.SH"]

    assert notes == []
    assert snap.name == "贵州茅台"
    assert snap.last_price == 1350.6
    assert len(snap.bids) == 5 and len(snap.asks) == 5
    assert snap.bids[0].price == 1350.6
    assert snap.asks[0].price == 1350.67
    assert snap.as_of == datetime(2026, 7, 31, 15, 34, 59)
    assert snap.change_pct == pytest.approx(1350.6 / 1361.76 - 1)


def test_tencent_snapshot_supplies_real_limit_prices() -> None:
    """The 9.5% approximation cannot distinguish a 10% board from a 20%
    ChiNext/STAR board; the published limit price can."""
    from ashare_decision_support.adapters.snapshot import SnapshotAdapter

    adapter = SnapshotAdapter(prefer="tencent", opener=StubHttp(TENCENT_LINE))
    snapshots, _ = adapter.get_snapshots(["300750.SZ"])
    snap = snapshots["300750.SZ"]

    assert snap.limit_up == 482.26
    assert snap.limit_down == 321.5
    # 482.26 / 401.88 - 1 == +20%, i.e. a ChiNext board, not a 10% one.
    assert snap.limit_up / snap.prev_close - 1 == pytest.approx(0.20, abs=1e-4)
    assert snap.at_limit_up is False


def test_limit_state_is_unknown_rather_than_guessed_without_a_limit_price() -> None:
    from ashare_decision_support.adapters.snapshot import SnapshotAdapter

    adapter = SnapshotAdapter(opener=StubHttp(SINA_LINE))
    snap = adapter.get_snapshots(["600519"])[0]["600519.SH"]

    # Sina does not publish limit prices; falling back to a percentage rule
    # here would silently misclassify every 20% board.
    assert snap.limit_up is None
    assert snap.at_limit_up is None


def test_a_halted_symbol_is_reported_missing_not_priced_at_zero() -> None:
    from ashare_decision_support.adapters.snapshot import SnapshotAdapter

    halted = 'var hq_str_sh600519="ST测试,0.000,0.000,0.000,0.000,0.000,0.000,0.000,0,0,' + \
        ",".join(["0"] * 20) + ',2026-07-31,15:00:00,00";'
    adapter = SnapshotAdapter(opener=StubHttp(halted))
    snapshots, notes = adapter.get_snapshots(["600519"])

    assert snapshots == {}
    assert any("未取到快照" in note for note in notes)


def test_snapshot_request_failure_becomes_a_note_not_an_exception() -> None:
    from ashare_decision_support.adapters.snapshot import SnapshotAdapter

    def explode(request, timeout=None):
        raise OSError("connection reset")

    snapshots, notes = SnapshotAdapter(opener=explode).get_snapshots(["600519"])

    assert snapshots == {}
    assert any("请求失败" in note for note in notes)


def test_snapshot_symbols_are_translated_to_the_vendor_form() -> None:
    from ashare_decision_support.adapters.snapshot import SnapshotAdapter

    http = StubHttp(SINA_LINE)
    SnapshotAdapter(opener=http).get_snapshots(["600519", "000001.SZ"])

    assert "sh600519,sz000001" in http.requests[0].full_url
    assert http.requests[0].headers["Referer"].startswith("https://finance.sina.com.cn")


# ---- 舆情：去重、时效、个股归因 -------------------------------------------


def _doc(title: str, published_at: str = "", summary: str = "", source: str = ""):
    from ashare_decision_support.news.base import NewsDoc

    return NewsDoc(title=title, summary=summary, source=source, published_at=published_at)


NEWS_NOW = datetime(2026, 8, 3, 8, 20)


def test_cross_source_duplicates_are_merged_not_counted_twice() -> None:
    """财联社与东财转发同一条时标题常一字不差。计三次会让板块热度凭空翻三倍。"""
    from ashare_decision_support.news.dedup import weigh_docs

    docs = [
        _doc("【财联社】央行开展5000亿元逆回购", "2026-08-03 07:30:00"),
        _doc("央行开展5000亿元逆回购", "2026-08-03 07:35:00", source="东方财富"),
        _doc("央行开展 5000 亿元逆回购！", "2026-08-03 07:40:00", source="新浪"),
        _doc("某公司业绩预增", "2026-08-03 07:31:00"),
    ]
    kept, notes = weigh_docs(docs, NEWS_NOW)

    assert len(kept) == 2
    assert any("重复舆情合并 2 条" in note for note in notes)
    # 保留最早发布的那条：原发比转发更接近事件时间。
    assert kept[0].published_at == "2026-08-03 07:30:00"


def test_stale_documents_decay_and_ancient_ones_are_dropped() -> None:
    from ashare_decision_support.news.dedup import MAX_AGE_HOURS, weigh_docs

    docs = [
        _doc("刚刚发生的事", "2026-08-03 08:15:00"),
        _doc("昨天此刻的事", "2026-08-02 08:20:00"),
        _doc("上周的事", "2026-07-20 08:20:00"),
    ]
    kept, notes = weigh_docs(docs, NEWS_NOW)

    assert [doc.title for doc in kept] == ["刚刚发生的事", "昨天此刻的事"]
    assert kept[0].weight > 0.9
    # 12 小时半衰期，24 小时前 ≈ 0.25。
    assert kept[1].weight == pytest.approx(0.25, abs=0.02)
    assert any(f"{MAX_AGE_HOURS:.0f} 小时" in note for note in notes)


def test_an_unparseable_timestamp_is_not_treated_as_ancient() -> None:
    """把「不知道多老」当成「很老」会整体抹掉只给日期的源（如新闻联播）。"""
    from ashare_decision_support.news.dedup import weigh_docs

    kept, notes = weigh_docs([_doc("无时间戳的快讯", "")], NEWS_NOW)

    assert len(kept) == 1
    assert kept[0].weight == 1.0
    assert any("未做时效折减" in note for note in notes)


def test_stale_news_moves_the_sector_score_less_than_fresh_news() -> None:
    from ashare_decision_support.news import LexiconSentiment
    from ashare_decision_support.news.dedup import weigh_docs

    analyzer = LexiconSentiment()
    fresh, _ = weigh_docs([_doc("光伏行业业绩预增，订单大幅增长", "2026-08-03 08:00:00")], NEWS_NOW)
    stale, _ = weigh_docs([_doc("光伏行业业绩预增，订单大幅增长", "2026-08-01 08:00:00")], NEWS_NOW)

    fresh_score = analyzer.score_docs(fresh)
    stale_score = analyzer.score_docs(stale)

    assert fresh_score.polarity == pytest.approx(stale_score.polarity)
    # 极性一样，但强度（置信度）被时效压下来了。
    assert fresh_score.intensity > stale_score.intensity


class StubNewsSource:
    """按标的返回预置新闻；`boom` 用来验证单标的失败不影响其余。"""

    def __init__(self, by_symbol, announcements=None, fail=False):
        self.by_symbol = by_symbol
        self.announcements = announcements or []
        self.fail = fail
        self.calls = []

    def fetch_for_symbols(self, symbols, limit=10):
        self.calls.append(list(symbols))
        if self.fail:
            raise RuntimeError("feed down")
        return {s: self.by_symbol.get(s, []) for s in symbols if self.by_symbol.get(s)}, []

    def fetch_announcements(self, trade_date):
        return list(self.announcements), []


def test_symbol_news_agent_merges_announcements_with_headlines() -> None:
    from ashare_decision_support.agents import SymbolNewsAgent

    source = StubNewsSource(
        by_symbol={"600519.SH": [_doc("公司获得大额订单", "2026-08-03 07:00:00")]},
        announcements=[
            {"symbol": "600519", "title": "关于控股股东减持股份的公告",
             "category": "股东减持", "published_at": "2026-08-03"},
            {"symbol": "000001", "title": "别人家的公告", "category": "其他",
             "published_at": "2026-08-03"},
        ],
    )
    agent = SymbolNewsAgent(
        symbols=["600519.SH"], announcements=source.announcements, now=NEWS_NOW
    )
    data, notes = agent.run(
        AgentContext(trade_date=SESSION, news_source=source, symbols=["600519.SH"])
    )
    reading = data["sentiment"]["600519.SH"]

    assert reading.doc_count == 2  # 新闻 + 属于它的那条公告，别人的不算
    assert "大额订单" in reading.bullish_hits
    assert "减持" in reading.bearish_hits
    evidence = data["events"]["600519.SH"]
    assert evidence.event_types == ("订单",)
    assert evidence.risk_hits == ("减持",)
    assert any("个股舆情" in note for note in notes)


def test_symbol_news_failure_degrades_to_no_reading_not_an_exception() -> None:
    from ashare_decision_support.agents import SymbolNewsAgent

    data, notes = SymbolNewsAgent(symbols=["600519.SH"], now=NEWS_NOW).run(
        AgentContext(
            trade_date=SESSION, news_source=StubNewsSource({}, fail=True), symbols=["600519.SH"]
        )
    )

    assert data["sentiment"] == {}
    assert any("个股新闻源不可用" in note for note in notes)


def test_symbol_news_agent_without_a_source_says_so() -> None:
    from ashare_decision_support.agents import SymbolNewsAgent

    data, notes = SymbolNewsAgent(symbols=["600519.SH"]).run(
        AgentContext(trade_date=SESSION, news_source=None, symbols=["600519.SH"])
    )

    assert data["sentiment"] == {}
    assert any("不含个股舆情项" in note for note in notes)


# ---- 突发风险公告告警 -----------------------------------------------------


BLOCKING_NOTICE = {
    "symbol": "600519",
    "title": "关于公司股票被实施退市风险警示的公告",
    "category": "风险警示",
    "published_at": "2026-08-03",
}


def test_refresh_risk_survives_contact_with_a_real_announcement() -> None:
    """这条路径在此之前没有任何调用点，两个缺陷因此一直没被发现：
    没人给它数据，而且它读的是 `AnnouncementRisk` 上不存在的属性。"""
    session = MonitorSession(StubDaily(), ScriptedIntraday([minute_bars(RISE)]), SESSION, alert_state=AlertState.load(SESSION))

    blocked = session.refresh_risk([BLOCKING_NOTICE, {"symbol": "000001", "title": "年度报告", "category": "年报"}])

    assert blocked == 1
    assert session.risk_notes["600519"]  # 非空说明理由真的取出来了


def test_risk_notice_fires_once_the_announcements_are_actually_loaded() -> None:
    engine = TriggerEngine(levels=(LEVEL_ACTION, LEVEL_WARN))
    state = AlertState.load(SESSION)
    advice = StockAdvice(symbol="600519.SH", last_price=100.6, entry_low=99.0, entry_high=101.0, stop=95.0)

    from ashare_decision_support.signals.intraday import IntradayAnalyzer

    snapshot = IntradayAnalyzer(today=SESSION).analyze("600519.SH", minute_bars(RISE))
    advice.last_price = snapshot.last_price
    # 第一轮建基线，但风险公告本身就是一次跃迁，不必等第二轮。
    alerts = engine.evaluate(
        advice=advice, intraday=snapshot, state=state,
        now=datetime(2026, 8, 3, 10, 0), risk_note="风险警示：关于公司股票被实施退市风险警示的公告",
    )

    assert [alert.trigger for alert in alerts] == ["risk_notice"]
    assert alerts[0].level == LEVEL_WARN

    # 一天一次：同一条公告不会每轮重推。
    again = engine.evaluate(
        advice=advice, intraday=snapshot, state=state,
        now=datetime(2026, 8, 3, 11, 0), risk_note="风险警示：关于公司股票被实施退市风险警示的公告",
    )
    assert again == []


def test_monitor_loop_refreshes_announcements_on_a_slow_cycle() -> None:
    """公告是整日全量拉取，每轮都拉是浪费；但只在开盘读一次会漏掉盘中临时公告。"""
    calls: list[int] = []
    session = MonitorSession(StubDaily(), ScriptedIntraday([minute_bars(RISE)]), SESSION, alert_state=AlertState.load(SESSION))
    session.prime(["600519.SH"])

    stats = run_monitor_loop(
        session=session,
        watch_symbols=lambda: ["600519.SH"],
        candidate_symbols=list,
        notifiers=[],
        now_fn=lambda: datetime(2026, 8, 3, 10, 0),
        sleep=lambda seconds: None,
        max_passes=7,
        refresh_risk=lambda: (calls.append(1), [])[1],
        refresh_risk_every=3,
    )

    assert stats.passes == 7
    assert len(calls) == 2  # 第 3 轮与第 6 轮


def test_a_failing_announcement_refresh_does_not_stop_monitoring() -> None:
    def boom():
        raise RuntimeError("notice feed down")

    session = MonitorSession(StubDaily(), ScriptedIntraday([minute_bars(RISE)]), SESSION, alert_state=AlertState.load(SESSION))
    session.prime(["600519.SH"])

    stats = run_monitor_loop(
        session=session,
        watch_symbols=lambda: ["600519.SH"],
        candidate_symbols=list,
        notifiers=[],
        now_fn=lambda: datetime(2026, 8, 3, 10, 0),
        sleep=lambda seconds: None,
        max_passes=3,
        refresh_risk=boom,
        refresh_risk_every=1,
    )

    assert stats.passes == 3
    assert any("公告刷新失败" in note for note in stats.notes)


# ---- 任务层接线：并行采集 · 舆情入分 · 辩论 -------------------------------


def test_advise_runs_its_independent_fetches_concurrently() -> None:
    """日线、宽基、分时、个股舆情互相独立且全是网络等待。"""
    import threading
    import time

    from ashare_decision_support.jobs import run_advise

    entered = threading.Barrier(4, timeout=5)

    class SlowDaily(StubDaily):
        def get_historical_bars(self, symbols, start, end):
            entered.wait()
            time.sleep(0.05)
            return super().get_historical_bars(symbols, start, end)

    class SlowIntraday:
        def get_minute_bars(self, symbols, period="5", **kwargs):
            entered.wait()
            time.sleep(0.05)
            return {symbol: minute_bars(RISE) for symbol in symbols}, []

    class SlowNews:
        def fetch_for_symbols(self, symbols, limit=10):
            entered.wait()
            time.sleep(0.05)
            return {symbol: [] for symbol in symbols}, []

    started = time.monotonic()
    summary = run_advise(
        adapter=SlowDaily(),
        symbols=["600519.SH"],
        intraday_adapter=SlowIntraday(),
        news_source=SlowNews(),
        notifiers=[],
        trade_date=SESSION,
    )
    elapsed = time.monotonic() - started

    # Barrier(4) 只有四路同时在跑才会放行；超时会抛 BrokenBarrierError。
    assert summary["requested"] == 1
    assert elapsed < 0.4


def test_advise_includes_relative_strength_in_facts_and_brief() -> None:
    from ashare_decision_support.jobs import run_advise

    class Adapter(StubDaily):
        def get_historical_bars(self, symbols, start, end):
            return {
                symbol: daily_bars(base=100.0 if symbol == "sh000001" else 90.0, symbol=symbol)
                for symbol in symbols
            }

    class Intraday:
        def get_minute_bars(self, symbols, period="5", **kwargs):
            return {symbol: minute_bars(RISE) for symbol in symbols}, []

    summary = run_advise(
        adapter=Adapter(),
        symbols=["600519.SH"],
        intraday_adapter=Intraday(),
        use_news=False,
        use_order_book=False,
        notifiers=[],
        trade_date=SESSION,
    )

    relative = summary["facts"]["symbols"][0]["relative_strength"]
    assert relative["benchmark_name"] == "上证指数"
    assert "1d" in relative["excess"]
    assert "相对宽基" in summary["content"]


def test_advise_presents_company_event_and_risk_evidence() -> None:
    from ashare_decision_support.jobs import run_advise

    class Intraday:
        def get_minute_bars(self, symbols, period="5", **kwargs):
            return {symbol: minute_bars(RISE) for symbol in symbols}, []

    fresh = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    source = StubNewsSource(
        {"600519.SH": [_doc("测试股中标大额订单", fresh)]},
        announcements=[
            {"symbol": "600519", "title": "关于控股股东减持股份的公告", "published_at": fresh}
        ],
    )
    summary = run_advise(
        adapter=StubDaily(),
        symbols=["600519.SH"],
        intraday_adapter=Intraday(),
        news_source=source,
        use_order_book=False,
        notifiers=[],
        trade_date=SESSION,
    )

    evidence = summary["facts"]["symbols"][0]["event_evidence"]
    assert evidence["event_types"] == ["订单"]
    assert evidence["risk_hits"] == ["减持"]
    assert "事件催化证据：订单" in summary["content"]
    assert "风险证据：减持" in summary["content"]


def test_advise_feeds_stock_news_into_the_score() -> None:
    from ashare_decision_support.jobs import run_advise

    class Intraday:
        def get_minute_bars(self, symbols, period="5", **kwargs):
            return {symbol: minute_bars(RISE) for symbol in symbols}, []

    class News:
        def __init__(self, docs):
            self.docs = docs

        def fetch_for_symbols(self, symbols, limit=10):
            return {symbol: list(self.docs) for symbol in symbols}, []

    def score_of(docs):
        summary = run_advise(
            adapter=StubDaily(), symbols=["600519.SH"], intraday_adapter=Intraday(),
            news_source=News(docs), notifiers=[], trade_date=SESSION,
        )
        return summary["facts"]["symbols"][0]

    quiet = score_of([])
    # 发布时刻必须相对**真实 now** 取，不能写死。个股舆情按 12 小时半衰期折权，
    # 而折权用的是墙上时钟，不是 `trade_date`——写死成 2026-08-03 07:00 的话，
    # 这条断言只在真实时间落在那之后 24 小时内才成立：实测 24.2 小时时 intensity
    # 衰减到 0.2449，而告警门槛要求 ≥0.25，差 2%。**测试会在某天早上自己变红，
    # 而那天没有任何人改过代码。**
    fresh = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    bearish = score_of([_doc("控股股东拟大幅减持，公司收到问询函", fresh)])

    assert bearish["score"] < quiet["score"]
    assert bearish["news"]["doc_count"] == 1
    assert any("舆情偏空" in item for item in bearish["warnings"])


def test_advise_exposes_rule_score_attribution_in_facts_and_brief() -> None:
    """总分可追溯到技术基准与确定性上下文，不用模型编造权重。"""
    from ashare_decision_support.jobs import run_advise

    class Intraday:
        def get_minute_bars(self, symbols, period="5", **kwargs):
            return {symbol: minute_bars(RISE) for symbol in symbols}, []

    summary = run_advise(
        adapter=StubDaily(),
        symbols=["600519.SH"],
        intraday_adapter=Intraday(),
        use_news=False,
        use_order_book=False,
        notifiers=[],
        trade_date=SESSION,
    )

    item = summary["facts"]["symbols"][0]
    assert "technical" in item["score_components"]
    assert "context_delta" in item["score_components"]
    assert "评分归因：价格/结构基准" in summary["content"]


def test_advise_appends_the_debate_without_letting_it_touch_the_numbers() -> None:
    from ashare_decision_support.jobs import run_advise

    class Intraday:
        def get_minute_bars(self, symbols, period="5", **kwargs):
            return {symbol: minute_bars(RISE) for symbol in symbols}, []

    panel = DebatePanel(client=FakeLlm([
        "- 结构完整", "- 但已接近压力位", "- 分歧在于能否守住",
    ]))
    summary = run_advise(
        adapter=StubDaily(), symbols=["600519.SH"], intraday_adapter=Intraday(),
        use_news=False, debate=panel, notifiers=[], trade_date=SESSION,
    )

    assert "多空视角" in summary["content"]
    assert "结构完整" in summary["content"]
    # facts 是辩论的输入，不能被辩论回写。
    assert "多空视角" not in json.dumps(summary["facts"], ensure_ascii=False)


def test_monitor_loads_announcements_so_the_risk_trigger_has_input() -> None:
    from ashare_decision_support.jobs import run_monitor
    from ashare_decision_support.state import Watchlist

    book = Watchlist()
    book.add("600519.SH")
    book.save()

    class Intraday:
        def get_minute_bars(self, symbols, period="1", **kwargs):
            return {symbol: minute_bars(RISE) for symbol in symbols}, []

    summary = run_monitor(
        adapter=StubDaily(),
        once=True,
        intraday_adapter=Intraday(),
        news_source=StubNewsSource({}, announcements=[BLOCKING_NOTICE]),
        notifiers=[],
        trade_date=SESSION,
        now=datetime(2026, 8, 3, 10, 0),
    )

    assert summary["risk_blocked"] == 1
    assert any("公告命中否决项" in note for note in summary["notes"])


def test_monitor_says_so_when_the_announcement_source_is_missing() -> None:
    from ashare_decision_support.jobs import run_monitor
    from ashare_decision_support.state import Watchlist

    book = Watchlist()
    book.add("600519.SH")
    book.save()

    class Intraday:
        def get_minute_bars(self, symbols, period="1", **kwargs):
            return {symbol: minute_bars(RISE) for symbol in symbols}, []

    summary = run_monitor(
        adapter=StubDaily(), once=True, intraday_adapter=Intraday(),
        news_source=object(), notifiers=[], trade_date=SESSION,
        now=datetime(2026, 8, 3, 10, 0),
    )

    assert summary["risk_blocked"] == 0
    assert any("不含突发风险公告告警" in note for note in summary["notes"])


def test_post_close_accepts_the_trade_date_the_daemon_passes_it() -> None:
    """`daily_cycle` 一直在传 `trade_date=`，而函数没有这个参数——守护进程里的
    15:10 盘后任务因此每次都以 TypeError 失败。"""
    import inspect

    from ashare_decision_support.jobs import run_post_close

    assert "trade_date" in inspect.signature(run_post_close).parameters


def test_post_close_runs_the_debate_on_its_own_facts(tmp_path) -> None:
    from ashare_decision_support.jobs import run_post_close

    panel = DebatePanel(client=FakeLlm(["- 结构完整", "- 位置偏高", "- 分歧在成交量"]))
    summary = run_post_close(
        adapter=StubDaily(), symbols=["600519.SH"], output_dir=str(tmp_path),
        notifiers=[], debate=panel, trade_date=SESSION,
    )

    assert "多空视角" in summary["content"]
    assert "结构完整" in summary["content"]


def test_two_headlines_cannot_max_out_the_sector_sentiment_cap() -> None:
    """实测缺陷：基础化工靠 2 条快讯拿满 +12，计算机 22 条只拿 +2.0——证据越多
    得分越低。`intensity` 是命中密度（按文档数归一），不是证据量。"""
    from ashare_decision_support.news import LexiconSentiment
    from ashare_decision_support.sectors.base import SectorDefinition, SectorMetrics
    from ashare_decision_support.sectors.predict import SENTIMENT_CAP, SectorPredictor

    definition = SectorDefinition(name="基础化工", level="SW1", symbols=["600230.SH", "002440.SZ"])
    metrics = {
        "基础化工": SectorMetrics(
            name="基础化工", constituents=2, covered=2, ret_1d=0.01, ret_5d=0.02,
            breadth=0.6, above_ma20=0.6, strong_count=0, volume_ratio=1.1,
        )
    }
    strong = [_doc("化工行业利好，产品大幅涨价", "2026-08-03 08:00:00")] * 2

    outlook = SectorPredictor(LexiconSentiment()).predict([definition], metrics, strong)[0]
    points = [item for item in outlook.reasons if "舆情" in item]

    # 2 条 → 置信度 2/8 = 0.25，拿不满上限。
    assert not points or f"+{SENTIMENT_CAP:.1f}" not in points[0]
    assert all(abs(float(item.split("（")[1].split("，")[0])) < SENTIMENT_CAP for item in points)


def test_more_evidence_is_worth_more_not_less() -> None:
    from ashare_decision_support.sectors.predict import sentiment_confidence

    assert sentiment_confidence(0) == 0.0
    assert sentiment_confidence(2) < sentiment_confidence(8)
    assert sentiment_confidence(8) == 1.0
    assert sentiment_confidence(50) == 1.0


# ---- 涨停池情绪 ----------------------------------------------------------


def _pool_payload(pool: str, qdate: int = 20260731):
    rows = {
        "ZT": [
            {"c": "000593", "m": 0, "n": "德龙汇能", "p": 16920, "zdp": 10.01,
             "amount": 1.1e8, "hs": 1.8, "lbc": 1, "fbt": 92500, "fund": 8.2e7,
             "zbc": 0, "hybk": "燃气Ⅱ", "zttj": {"days": 1, "ct": 1}},
            # 真实形态：`zttj` 是「20 天 10 板」的统计窗口，`lbc` 才是连板高度。
            # 两者拉开差距，这条断言才验得出读的是哪个字段。
            {"c": "603221", "m": 1, "n": "爱丽家居", "p": 10000, "zdp": 10.0,
             "amount": 9.2e7, "hs": 5.0, "lbc": 6, "fbt": 92500, "fund": 9.2e7,
             "zbc": 0, "hybk": "家居用品", "zttj": {"days": 20, "ct": 10}},
        ],
        "ZB": [
            {"c": "000636", "m": 0, "n": "风华高科", "p": 49350, "ztp": 55550,
             "zdp": -2.28, "amount": 1.3e10, "hs": 22.1, "lbc": 0, "fbt": 92500,
             "zbc": 3, "hybk": "元件", "zttj": {"days": 0, "ct": 0}},
        ],
        "DT": [],
    }[pool]
    return json.dumps({"rc": 0, "data": {"tc": len(rows), "qdate": qdate, "pool": rows}})


class StubPoolHttp:
    def __init__(self, qdate: int = 20260731, fail: set | None = None):
        self.qdate = qdate
        self.fail = fail or set()
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request.full_url)
        pool = next(p for p in ("ZT", "ZB", "DT") if f"getTopic{p}Pool" in request.full_url)
        if pool in self.fail:
            raise OSError("boom")
        return _Resp(_pool_payload(pool, self.qdate).encode("utf-8"))


class _Resp:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_limit_pool_reads_seal_quality_not_just_counts() -> None:
    from ashare_decision_support.adapters.limit_pool import LimitPoolAdapter

    snap = LimitPoolAdapter(opener=StubPoolHttp()).fetch(date(2026, 7, 31))

    assert snap.limit_up_count == 2 and snap.blowup_count == 1
    # 炸板率 = 冲过板的票里没守住的比例，不是"炸板数/涨停数"。
    assert snap.blowup_rate == pytest.approx(1 / 3, abs=1e-4)
    assert snap.max_streak == 6
    assert snap.limit_up[1].window_days == 20      # 「20 天 10 板」是另一件事
    assert snap.limit_up[1].window_limit_ups == 10
    assert snap.limit_up[1].symbol == "603221.SH"   # m=1 -> 沪
    assert snap.limit_up[0].symbol == "000593.SZ"   # m=0 -> 深
    assert snap.limit_up[0].price == pytest.approx(16.92)   # 分 -> 元
    assert snap.limit_up[0].first_seal == "09:25"
    assert snap.blowups[0].limit_price == pytest.approx(55.55)


def test_limit_pool_does_not_mistake_the_stat_window_for_a_streak() -> None:
    """东财 `zttj` 是「N 天 M 板」，不是连板高度。

    2026-07-31 实测：全市场最高连板 9 板（爱丽家居 lbc=9），但它的
    `zttj.days` 是 20，读错字段会把「最高连板」报成 20 板。这个数直接进
    `sectors/predict.py` 的情绪打分（≥5 加分、≤2 减分），一只「10 天 5 板」
    的**首板**股（美利云 days=10 / lbc=1）会把减分翻成加分。
    """
    from ashare_decision_support.adapters.limit_pool import LimitPoolAdapter

    class StatWindowHttp(StubPoolHttp):
        def __call__(self, request, timeout=None):
            if "getTopicZTPool" not in request.full_url:
                return _Resp(json.dumps({"rc": 0, "data": {"tc": 0, "pool": []}}).encode())
            rows = [
                {"c": "000815", "m": 0, "n": "美利云", "p": 10000, "zdp": 10.0,
                 "lbc": 1, "zbc": 0, "zttj": {"days": 10, "ct": 5}},
            ]
            return _Resp(
                json.dumps({"rc": 0, "data": {"tc": 1, "qdate": 20260731, "pool": rows}}).encode()
            )

    snap = LimitPoolAdapter(opener=StatWindowHttp()).fetch(date(2026, 7, 31))

    assert snap.max_streak == 1          # 首板，不是 10 板
    assert snap.limit_up[0].window_days == 10
    assert snap.limit_up[0].window_limit_ups == 5


def test_limit_pool_labels_data_with_the_date_the_server_actually_returned() -> None:
    """周末或盘前问"今天"，服务端返回的是最近交易日。按请求日期标注，就是把上一个
    交易日的情绪读数挂在今天名下。"""
    from ashare_decision_support.adapters.limit_pool import LimitPoolAdapter

    snap = LimitPoolAdapter(opener=StubPoolHttp(qdate=20260731)).fetch(date(2026, 8, 1))

    assert snap.trade_date == date(2026, 7, 31)
    assert snap.requested_date == date(2026, 8, 1)
    assert any("实际为 2026-07-31" in note for note in snap.notes)


def test_no_limit_up_at_all_is_not_reported_as_zero_blowup_rate() -> None:
    """分母为 0 时给 0.0 会读成"封板全部守住"，而实际是"没有票冲板"。"""
    from ashare_decision_support.adapters.limit_pool import LimitPoolSnapshot

    empty = LimitPoolSnapshot(trade_date=date(2026, 8, 1))

    assert empty.blowup_rate is None


def test_a_dead_pool_degrades_instead_of_raising() -> None:
    from ashare_decision_support.adapters.limit_pool import LimitPoolAdapter

    snap = LimitPoolAdapter(opener=StubPoolHttp(fail={"ZB"})).fetch(date(2026, 7, 31))

    assert snap.ok is True
    assert snap.limit_up_count == 2
    assert any("炸板池" in note for note in snap.notes)


def test_blowup_rate_moves_the_market_score_against_a_rising_index() -> None:
    """指数全线上涨而封板意愿崩塌，是同一天会发生的两件事。别的读数都看不到后者。"""
    from ashare_decision_support.sectors.predict import limit_pool_points

    healthy, healthy_reasons = limit_pool_points(0.15, 7)
    stressed, stressed_reasons = limit_pool_points(0.60, 2)

    assert healthy > 0 > stressed
    assert any("封板结实" in item for item in healthy_reasons)
    assert any("封板意愿弱" in item for item in stressed_reasons)
    # 没有读数就不该有分值，而不是给 0 分默认"正常"。
    assert limit_pool_points(None, None) == (0.0, [])


# ---- 大模型舆情归因 ------------------------------------------------------


class StubLlm:
    configured = True

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def chat(self, messages):
        self.prompts.append(messages[-1]["content"])
        return self.reply if isinstance(self.reply, str) else self.reply(messages)


NEWS_SECTORS = ["电子", "计算机", "传媒", "交通运输"]


def test_labels_align_by_echoed_title_not_by_model_reported_index() -> None:
    """实测缺陷：模型自报的 i 整体偏一位，「法国森林火灾」拿到了「动画电影票房」的
    归因，整批错行——而且在模型被要求说明传导机制之前完全看不出来。"""
    from ashare_decision_support.news.llm_classifier import LlmNewsClassifier

    docs = [
        _doc("法国森林火灾过火面积接近12万公顷", "2026-08-01 08:00:00"),
        _doc("中微公司5年内产能将超700亿元", "2026-08-01 08:01:00"),
    ]
    # 序号全部错开一位，但 t 抄对了。
    reply = json.dumps([
        {"i": 1, "t": "法国森林火灾", "sectors": [], "direction": "neutral",
         "event": "其他", "why": ""},
        {"i": 2, "t": "中微公司5年", "sectors": ["电子"], "direction": "bullish",
         "event": "产能", "why": "设备产能扩张"},
    ], ensure_ascii=False)

    result = LlmNewsClassifier(StubLlm(reply)).classify(docs, NEWS_SECTORS)
    from ashare_decision_support.news.dedup import dedup_key

    assert result.labels[dedup_key(docs[0])].sectors == ()
    assert result.labels[dedup_key(docs[1])].sectors == ("电子",)
    assert result.labels[dedup_key(docs[1])].why == "设备产能扩张"


def test_a_batch_that_cannot_be_aligned_is_discarded_whole() -> None:
    from ashare_decision_support.news.llm_classifier import LlmNewsClassifier

    docs = [_doc("央行开展5000亿元逆回购", "2026-08-01 08:00:00")]
    reply = json.dumps([
        {"i": 0, "t": "完全不相干的标题", "sectors": ["电子"], "direction": "bullish",
         "event": "政策", "why": "硬凑"},
    ], ensure_ascii=False)

    result = LlmNewsClassifier(StubLlm(reply)).classify(docs, NEWS_SECTORS)

    assert result.labels == {}
    assert result.ok is False


def test_an_invented_sector_name_is_dropped_not_fuzzy_matched() -> None:
    from ashare_decision_support.news.llm_classifier import LlmNewsClassifier

    docs = [_doc("某公司发布新款芯片", "2026-08-01 08:00:00")]
    reply = json.dumps([
        {"i": 0, "t": "某公司发布新", "sectors": ["半导体设备", "电子"],
         "direction": "bullish", "event": "技术突破", "why": "新品发布"},
    ], ensure_ascii=False)

    result = LlmNewsClassifier(StubLlm(reply)).classify(docs, NEWS_SECTORS)
    from ashare_decision_support.news.dedup import dedup_key

    # 「半导体设备」不在清单里，直接丢；映射到"最像的"那个等于放幻觉进来。
    assert result.labels[dedup_key(docs[0])].sectors == ("电子",)


def test_an_attribution_without_a_stated_mechanism_is_not_trusted() -> None:
    from ashare_decision_support.news.dedup import dedup_key
    from ashare_decision_support.news.llm_classifier import LlmNewsClassifier

    docs = [_doc("西班牙发生山火", "2026-08-01 08:00:00")]
    reply = json.dumps([
        {"i": 0, "t": "西班牙发生山", "sectors": ["传媒"], "direction": "bullish",
         "event": "其他", "why": ""},
    ], ensure_ascii=False)

    result = LlmNewsClassifier(StubLlm(reply)).classify(docs, NEWS_SECTORS)

    assert result.labels[dedup_key(docs[0])].sectors == ()


def test_the_model_saying_no_sector_is_a_verdict_not_a_gap() -> None:
    """空归属曾被 attribution() 过滤掉，于是下游退回关键词重新归因一遍——
    模型说「不属于任何板块」变成了没说。"""
    from ashare_decision_support.news import LexiconSentiment
    from ashare_decision_support.news.dedup import dedup_key
    from ashare_decision_support.sectors.base import SectorDefinition
    from ashare_decision_support.sectors.predict import SectorPredictor

    doc = _doc("光伏行业迎来政策利好，产品涨价", "2026-08-01 08:00:00")
    definition = SectorDefinition(name="电力设备", level="SW1", symbols=["600000.SH"])
    predictor = SectorPredictor(LexiconSentiment())

    # 没有归因 -> 关键词把"光伏"挂到电力设备
    by_keyword = predictor._attribute_docs([definition], [doc], None)
    assert by_keyword["电力设备"] == [doc]

    # 模型明确判定无归属 -> 就是无归属，不再回退关键词
    by_model = predictor._attribute_docs([definition], [doc], {dedup_key(doc): ()})
    assert by_model["电力设备"] == []


def test_classification_falls_back_to_the_lexicon_without_a_key() -> None:
    from ashare_decision_support.news.llm_classifier import LlmNewsClassifier

    class Unconfigured:
        configured = False

    result = LlmNewsClassifier(Unconfigured()).classify([_doc("x")], NEWS_SECTORS)

    assert result.ok is False
    assert any("关键词词典" in note for note in result.notes)


def test_label_backed_sentiment_never_takes_a_magnitude_from_the_model() -> None:
    """模型只出方向标签，强度固定。让它输出"有多强"就是让它输出数字。"""
    from ashare_decision_support.news import LexiconSentiment
    from ashare_decision_support.news.dedup import dedup_key, weigh_docs
    from ashare_decision_support.news.llm_classifier import DocLabel, LabelBackedSentiment

    docs = [
        _doc("公司获得订单", "2026-08-01 08:00:00"),
        _doc("公司股东减持", "2026-08-01 08:00:00"),
    ]
    docs, _ = weigh_docs(docs, NEWS_NOW.replace(day=1))
    labels = {
        dedup_key(docs[0]): DocLabel(sectors=("电子",), direction="bullish", why="订单"),
        dedup_key(docs[1]): DocLabel(sectors=("电子",), direction="bearish", why="减持"),
    }
    score = LabelBackedSentiment(labels, LexiconSentiment()).score_docs(docs)

    # 一多一空、权重相同 -> 极性抵消。极性只能是 +1/-1/0 的加权平均。
    assert score.polarity == pytest.approx(0.0, abs=1e-6)
    assert 0.0 < score.intensity <= 1.0


def test_an_invented_event_type_is_dropped_like_an_invented_sector() -> None:
    """事件类型也必须原样命中清单。

    2026-08-02 实测 314 条归因里 17 条（5.4%）带着清单外的自创类型——销量 10、
    事件 3、高管变动/组织调整/产品创新/品牌建设各 1——而它们会原样印进简报的
    「关键词：技术突破、品牌建设、需求变化」。板块名和方向早就在校验，事件类型
    漏了，于是它成了幻觉唯一还走得通的那条路。

    置空而不是丢整条：归属与方向仍然可用，只有这个标签不可信。
    """
    from ashare_decision_support.news.dedup import dedup_key
    from ashare_decision_support.news.llm_classifier import LlmNewsClassifier

    docs = [_doc("比亚迪7月新能源汽车销量419211辆", "2026-08-01 08:00:00")]
    reply = json.dumps([
        {"i": 0, "t": "比亚迪7月新能", "sectors": ["电子"], "direction": "bullish",
         "event": "销量", "why": "新能源车销量大增"},
    ], ensure_ascii=False)

    label = LlmNewsClassifier(StubLlm(reply)).classify(docs, NEWS_SECTORS).labels[
        dedup_key(docs[0])
    ]

    assert label.event is None, "「销量」不在清单里"
    assert label.sectors == ("电子",)
    assert label.direction == "bullish"


def test_a_listed_event_type_survives() -> None:
    from ashare_decision_support.news.dedup import dedup_key
    from ashare_decision_support.news.llm_classifier import LlmNewsClassifier

    docs = [_doc("某公司中标10亿元订单", "2026-08-01 08:00:00")]
    reply = json.dumps([
        {"i": 0, "t": "某公司中标10", "sectors": ["计算机"], "direction": "bullish",
         "event": "订单", "why": "大额订单落地"},
    ], ensure_ascii=False)

    label = LlmNewsClassifier(StubLlm(reply)).classify(docs, NEWS_SECTORS).labels[
        dedup_key(docs[0])
    ]

    assert label.event == "订单"
