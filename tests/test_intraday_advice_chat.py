from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ashare_decision_support.adapters.freshness import check_intraday_freshness, in_session
from ashare_decision_support.adapters.sina_intraday import SinaIntradayAdapter, sina_symbol
from ashare_decision_support.advice import AdviceEngine
from ashare_decision_support.chat.session import (
    ChatResponder,
    ChatSession,
    handle_command,
    render_facts_summary,
)
from ashare_decision_support.models.enums import TrendState
from ashare_decision_support.models.schemas import DailyTechnicalSnapshot, PriceLevel
from ashare_decision_support.signals.intraday import IntradayAnalyzer, _minutes_since_open


# ---- symbol normalization ------------------------------------------------


def test_sina_symbol_covers_every_input_shape() -> None:
    assert sina_symbol("600519.SH") == "sh600519"
    assert sina_symbol("000001.SZ") == "sz000001"
    assert sina_symbol("300750") == "sz300750"
    assert sina_symbol("688981") == "sh688981"
    assert sina_symbol("sh600519") == "sh600519"
    assert sina_symbol("830799.BJ") == "bj830799"


# ---- intraday fetch ------------------------------------------------------


def minute_rows(day: str, count: int, start_price: float = 10.0, step: float = 0.01) -> list[dict]:
    rows = []
    moment = datetime.strptime(f"{day} 09:30:00", "%Y-%m-%d %H:%M:%S")
    price = start_price
    for index in range(count):
        price += step
        rows.append(
            {
                "day": (moment + timedelta(minutes=5 * index)).strftime("%Y-%m-%d %H:%M:%S"),
                "open": round(price - step, 3),
                "high": round(price + 0.02, 3),
                "low": round(price - 0.02, 3),
                "close": round(price, 3),
                "volume": 1000.0,
                "amount": 1000.0 * price,
            }
        )
    return rows


class FakeSinaAk:
    def __init__(self, broken: set[str] | None = None, rows: list[dict] | None = None) -> None:
        self.broken = broken or set()
        self.rows = rows if rows is not None else minute_rows("2026-07-31", 48)
        self.calls: list[tuple[str, str]] = []

    def stock_zh_a_minute(self, symbol, period, adjust):
        self.calls.append((symbol, period))
        if symbol in self.broken:
            raise ConnectionError("feed down")
        return list(self.rows)


def test_intraday_adapter_requests_raw_prices() -> None:
    """Levels are order prices; an adjusted series would make them wrong."""
    ak = FakeSinaAk()
    captured = {}
    original = ak.stock_zh_a_minute

    def spy(symbol, period, adjust):
        captured["adjust"] = adjust
        return original(symbol, period, adjust)

    ak.stock_zh_a_minute = spy
    SinaIntradayAdapter(ak_module=ak).get_minute_bars(["600519.SH"], period="5")

    assert captured["adjust"] == ""


def test_intraday_adapter_degrades_one_symbol_at_a_time() -> None:
    adapter = SinaIntradayAdapter(ak_module=FakeSinaAk(broken={"sz000002"}))
    bars, notes = adapter.get_minute_bars(["600519.SH", "000002.SZ"], period="5")

    assert list(bars) == ["600519.SH"]
    assert any("000002.SZ" in note for note in notes)


def test_intraday_adapter_rejects_an_unsupported_period() -> None:
    adapter = SinaIntradayAdapter(ak_module=FakeSinaAk())
    try:
        adapter.get_minute_bars(["600519.SH"], period="7")
    except ValueError as exc:
        assert "7" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


# ---- intraday structure --------------------------------------------------


def bars_from(rows: list[dict], symbol: str = "600519.SH") -> list[dict]:
    return SinaIntradayAdapter(ak_module=FakeSinaAk(rows=rows)).get_minute_bars([symbol], "5")[0][symbol]


def test_minutes_since_open_skips_the_lunch_break() -> None:
    assert _minutes_since_open(datetime(2026, 7, 31, 9, 30)) == 0
    assert _minutes_since_open(datetime(2026, 7, 31, 10, 30)) == 60
    assert _minutes_since_open(datetime(2026, 7, 31, 11, 30)) == 120
    # 13:00 is the afternoon open, so no minutes accrue over the break.
    assert _minutes_since_open(datetime(2026, 7, 31, 13, 0)) == 120
    assert _minutes_since_open(datetime(2026, 7, 31, 14, 0)) == 180
    assert _minutes_since_open(datetime(2026, 7, 31, 15, 0)) == 240


def test_snapshot_flags_a_session_that_is_not_today() -> None:
    """Treating the previous close as live is the dangerous failure here."""
    snapshot = IntradayAnalyzer(today=date(2026, 8, 1)).analyze(
        "600519.SH", bars_from(minute_rows("2026-07-31", 48))
    )

    assert snapshot is not None
    assert snapshot.session_date == date(2026, 7, 31)
    assert not snapshot.is_current_session
    assert any("非当日" in note for note in snapshot.notes)


def test_snapshot_marks_the_current_session_clean() -> None:
    snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze(
        "600519.SH", bars_from(minute_rows("2026-07-31", 48))
    )

    assert snapshot is not None
    assert snapshot.is_current_session
    assert snapshot.notes == []


def test_snapshot_computes_vwap_and_position() -> None:
    snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze(
        "600519.SH", bars_from(minute_rows("2026-07-31", 48))
    )

    assert snapshot is not None
    assert snapshot.vwap is not None
    assert snapshot.session_low < snapshot.vwap < snapshot.session_high
    # Steady uptrend closes at the high.
    assert snapshot.range_position is not None and snapshot.range_position > 0.9
    assert snapshot.price_vs_open_pct is not None and snapshot.price_vs_open_pct > 0


def test_snapshot_uses_only_the_latest_session() -> None:
    rows = minute_rows("2026-07-30", 48, start_price=5.0) + minute_rows("2026-07-31", 48, start_price=10.0)
    snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", bars_from(rows))

    assert snapshot is not None
    assert snapshot.session_date == date(2026, 7, 31)
    assert snapshot.session_low > 9.0


def test_volume_ratio_scales_the_baseline_for_a_partial_session() -> None:
    """A half day against full days would read ~0.5 and look like drying up."""
    rows = []
    for day in ("2026-07-28", "2026-07-29", "2026-07-30"):
        rows.extend(minute_rows(day, 48))
    rows.extend(minute_rows("2026-07-31", 24))  # half session
    snapshot = IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", bars_from(rows))

    assert snapshot is not None
    assert snapshot.volume_ratio is not None
    assert 0.9 <= snapshot.volume_ratio <= 1.1


def test_snapshot_returns_none_without_usable_bars() -> None:
    assert IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", []) is None


# ---- advice --------------------------------------------------------------


def price_level(side: str, price: float) -> PriceLevel:
    return PriceLevel(
        side=side, price=price, touches=2, strength=0.6, distance_pct=0.05, label=f"{side}-{price}"
    )


def daily_snapshot(last_close: float, supports: list[float], resistances: list[float]) -> DailyTechnicalSnapshot:
    return DailyTechnicalSnapshot(
        symbol="600519.SH",
        bar_count=120,
        last_close=last_close,
        last_open=last_close * 0.99,
        last_high=last_close * 1.01,
        last_low=last_close * 0.98,
        trend_state=TrendState.UPTREND,
        supports=[price_level("support", level) for level in supports],
        resistances=[price_level("resistance", level) for level in resistances],
        explanation="测试用快照",
        source_ts=datetime(2026, 7, 31, 15, 0),
    )


def advice_for(daily=None, rows=None, today=date(2026, 7, 31)):
    intraday = IntradayAnalyzer(today=today).analyze(
        "600519.SH", bars_from(rows if rows is not None else minute_rows("2026-07-31", 48))
    )
    return AdviceEngine().build("600519.SH", daily, intraday)


def test_entry_band_has_width() -> None:
    """A zero-width band is unfillable; the earlier version collapsed to one price."""
    advice = advice_for()

    assert advice.entry_low is not None and advice.entry_high is not None
    assert advice.entry_high > advice.entry_low


def test_stop_sits_below_the_session_low() -> None:
    advice = advice_for()
    assert advice.stop is not None and advice.entry_low is not None
    assert advice.stop < advice.entry_low


def test_daily_levels_are_converted_into_raw_price_space() -> None:
    """Adjusted daily levels quoted as order prices are simply wrong."""
    rows = minute_rows("2026-07-31", 48, start_price=100.0, step=0.1)
    intraday = IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", bars_from(rows))
    assert intraday is not None

    # Daily series is adjusted to half the real price; resistance 60 -> 120.
    daily = daily_snapshot(intraday.last_price / 2, supports=[45.0], resistances=[60.0])
    advice = AdviceEngine().build("600519.SH", daily, intraday)

    assert advice.targets
    assert any(abs(target - 120.0) < 1.0 for target in advice.targets)


def test_absurd_price_scale_disables_daily_levels_instead_of_lying() -> None:
    rows = minute_rows("2026-07-31", 48, start_price=100.0, step=0.1)
    intraday = IntradayAnalyzer(today=date(2026, 7, 31)).analyze("600519.SH", bars_from(rows))
    assert intraday is not None

    daily = daily_snapshot(0.5, supports=[0.4], resistances=[0.6])  # factor ~200x
    advice = AdviceEngine().build("600519.SH", daily, intraday)

    assert any("无法对齐" in warning for warning in advice.warnings)


def test_advice_explains_why_there_is_no_target() -> None:
    """At the high with no overhead resistance, say so rather than print a dash."""
    rows = minute_rows("2026-07-31", 48)
    # Close the session on a new high, so nothing intraday is left to aim at.
    rows[-1]["close"] = 10.6
    rows[-1]["high"] = 10.6
    advice = advice_for(daily=daily_snapshot(10.5, supports=[9.0], resistances=[]), rows=rows)

    assert advice.targets == []
    assert any("无法给出目标价" in warning for warning in advice.warnings)


def test_advice_without_intraday_refuses_to_quote_order_prices() -> None:
    advice = AdviceEngine().build(
        "600519.SH", daily_snapshot(10.0, supports=[9.0], resistances=[11.0]), intraday=None
    )

    assert advice.entry_low is None
    assert any("不可直接用于挂单" in warning for warning in advice.warnings)


def test_advice_always_publishes_three_scenarios() -> None:
    advice = advice_for()
    names = [scenario.name for scenario in advice.scenarios]

    assert names == ["强势延续", "均价震荡", "转弱回落"]
    assert all(scenario.level is not None for scenario in advice.scenarios)


def test_advice_with_no_data_at_all_says_so() -> None:
    advice = AdviceEngine().build("600519.SH", daily=None, intraday=None)
    assert any("无法给出任何判断" in warning for warning in advice.warnings)


# ---- chat ----------------------------------------------------------------


ADVICE_FACTS = {
    "trade_date": "2026-08-01",
    "symbols": [
        {"symbol": "600519.SH", "name": "贵州茅台", "last_price": 1350.6,
         "entry_low": 1333.93, "entry_high": 1337.53, "stop": 1285.46,
         "targets": [1355.72], "rrr": 4.08},
    ],
}


class FakeClient:
    def __init__(self, reply: str = "结构完好。", configured: bool = True, boom: bool = False) -> None:
        self.reply = reply
        self._configured = configured
        self.boom = boom
        self.provider = type("P", (), {"api_key_env": "DASHSCOPE_API_KEY"})()
        self.seen: list[list[dict]] = []

    @property
    def configured(self) -> bool:
        return self._configured

    def chat(self, messages):
        self.seen.append(messages)
        if self.boom:
            raise TimeoutError("upstream")
        return self.reply


def session_with_facts() -> ChatSession:
    session = ChatSession()
    session.add_symbols(["600519.SH"])
    session.set_facts("advice", ADVICE_FACTS)
    return session


def test_chat_answers_from_the_fact_bundle() -> None:
    client = FakeClient("现价 1350.6，止损 1285.46。")
    answer, notes = ChatResponder(client).answer(session_with_facts(), "现在什么情况")

    assert "1350.6" in answer
    assert notes == []
    # The facts are re-sent every turn rather than relied on from history.
    assert "1350.6" in client.seen[0][-1]["content"]


def test_chat_discards_an_answer_that_invents_a_number() -> None:
    client = FakeClient("目标价 1800.0，上涨概率 72%。")
    answer, notes = ChatResponder(client).answer(session_with_facts(), "能到多少")

    assert "已丢弃" in answer
    assert notes and "数字校验拦截" in notes[0]


def test_chat_records_history_only_for_accepted_answers() -> None:
    session = session_with_facts()
    responder = ChatResponder(FakeClient("目标价 1800.0。"))
    responder.answer(session, "能到多少")

    assert session.history == []


def test_chat_reports_a_failed_call_without_raising() -> None:
    answer, notes = ChatResponder(FakeClient(boom=True)).answer(session_with_facts(), "怎么样")
    assert "调用失败" in answer and notes


def test_chat_without_a_key_points_at_the_offline_path() -> None:
    answer, _ = ChatResponder(FakeClient(configured=False)).answer(session_with_facts(), "怎么样")
    assert "/show" in answer


def test_chat_with_empty_context_asks_for_symbols() -> None:
    answer, _ = ChatResponder(FakeClient()).answer(ChatSession(), "怎么样")
    assert "/add" in answer


def test_add_command_computes_and_reports() -> None:
    session = ChatSession()
    calls: list[list[str]] = []

    def compute(symbols):
        calls.append(list(symbols))
        return ADVICE_FACTS

    message = handle_command(session, "/add 600519.SH", compute, dict)

    assert calls == [["600519.SH"]]
    assert "600519.SH" in message
    assert session.facts["advice"] == ADVICE_FACTS


def test_drop_command_removes_the_symbol_from_facts_too() -> None:
    session = session_with_facts()
    handle_command(session, "/drop 600519.SH", lambda s: ADVICE_FACTS, dict)

    assert session.symbols == []
    assert session.facts["advice"]["symbols"] == []


def test_refresh_command_needs_a_non_empty_context() -> None:
    message = handle_command(ChatSession(), "/refresh", lambda s: ADVICE_FACTS, dict)
    assert "上下文为空" in message


def test_clear_keeps_facts_and_drops_history() -> None:
    session = session_with_facts()
    session.record("user", "hi")
    handle_command(session, "/clear", lambda s: ADVICE_FACTS, dict)

    assert session.history == []
    assert session.has_facts


def test_show_renders_without_touching_the_llm() -> None:
    summary = render_facts_summary(session_with_facts())
    assert "600519.SH" in summary and "1285.46" in summary


def test_unknown_command_is_reported_not_executed() -> None:
    assert "未知命令" in handle_command(ChatSession(), "/nope", lambda s: {}, dict)


# ---- freshness probe -----------------------------------------------------


def test_in_session_matches_the_a_share_clock() -> None:
    assert in_session(datetime(2026, 7, 31, 10, 0))       # Friday morning
    assert in_session(datetime(2026, 7, 31, 14, 0))       # Friday afternoon
    assert not in_session(datetime(2026, 7, 31, 12, 0))   # lunch
    assert not in_session(datetime(2026, 8, 1, 10, 0))    # Saturday


class StubIntraday:
    def __init__(self, first_last: datetime, second_last: datetime | None = None) -> None:
        self.first_last = first_last
        self.second_last = second_last or first_last
        self.calls = 0

    def get_minute_bars(self, symbols, period="1", max_bars=None):
        self.calls += 1
        stamp = self.first_last if self.calls == 1 else self.second_last
        return ({symbol: [{"timestamp": stamp, "close": 10.0}] for symbol in symbols}, [])


def test_freshness_refuses_to_judge_outside_a_session() -> None:
    stub = StubIntraday(datetime(2026, 7, 31, 15, 0))
    report = check_intraday_freshness(
        stub, ["600519.SH"], now=datetime(2026, 8, 1, 10, 0), sleep=lambda _: None
    )

    assert not report["in_session"]
    assert "非交易时段" in report["verdict"]
    assert stub.calls == 1  # no second sample outside a session


def test_freshness_passes_a_live_feed() -> None:
    now = datetime(2026, 7, 31, 10, 30)
    stub = StubIntraday(now - timedelta(minutes=1), now)
    report = check_intraday_freshness(stub, ["600519.SH"], now=now, sleep=lambda _: None)

    assert report["in_session"]
    assert "暂不需要付费" in report["verdict"]


def test_freshness_flags_a_feed_that_does_not_advance() -> None:
    now = datetime(2026, 7, 31, 10, 30)
    frozen = now - timedelta(minutes=1)
    report = check_intraday_freshness(
        StubIntraday(frozen, frozen), ["600519.SH"], now=now, sleep=lambda _: None
    )

    assert "没有前进" in report["verdict"]
    assert "付费" in report["verdict"]


def test_freshness_flags_an_unusable_lag() -> None:
    now = datetime(2026, 7, 31, 14, 0)
    stub = StubIntraday(now - timedelta(minutes=20), now - timedelta(minutes=18))
    report = check_intraday_freshness(stub, ["600519.SH"], now=now, sleep=lambda _: None)

    assert "建议开通付费档" in report["verdict"]
