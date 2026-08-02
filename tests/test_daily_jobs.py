from __future__ import annotations

from datetime import date, datetime, timedelta
import json

from ashare_decision_support.jobs import run_post_close, run_pre_open
from ashare_decision_support.notify import FileNotifier, split_utf8_text
from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput
from ashare_decision_support.signals import DailyTechnicalAnalyzer, WyckoffAnalyzer


def make_bars(symbol: str, count: int = 140, start_price: float = 20.0, drift: float = 0.15) -> list[dict]:
    """Synthetic daily bars with a steady drift, enough to fill the MA60 window."""
    bars: list[dict] = []
    price = start_price
    day = date(2026, 1, 5)
    for index in range(count):
        price = max(price + drift, 1.0)
        bars.append(
            {
                "symbol": symbol,
                "timestamp": datetime.combine(day + timedelta(days=index), datetime.min.time()),
                "open": round(price - 0.1, 2),
                "high": round(price + 0.3, 2),
                "low": round(price - 0.4, 2),
                "close": round(price, 2),
                "volume": 100000 + index * 100,
                "amount": (100000 + index * 100) * price * 100,
            }
        )
    return bars


class FakeAdapter:
    """Adapter stub that serves canned bars without touching the network."""

    def __init__(self, bars_by_symbol: dict[str, list[dict]], mode: str = "free") -> None:
        self.bars_by_symbol = bars_by_symbol
        self.mode = mode
        self.calls = 0

    def get_historical_bars(self, symbols, start, end):
        self.calls += 1
        return {symbol: self.bars_by_symbol.get(symbol, []) for symbol in symbols}

    def health_check(self):
        return {
            "mode": self.mode,
            "capabilities": {"supports_intraday": self.mode == "full"},
        }


def score_for(symbol: str, bars: list[dict]):
    snapshot = DailyTechnicalAnalyzer().analyze(symbol, bars)
    wyckoff = WyckoffAnalyzer().analyze(symbol, bars)
    return RuleBasedScoreProvider().score(ScoreInput(symbol=symbol, bars=bars, snapshot=snapshot, wyckoff=wyckoff))


def test_rule_based_scoring_prefers_uptrend_over_downtrend() -> None:
    up = score_for("UP.SH", make_bars("UP.SH", drift=0.2))
    down = score_for("DOWN.SH", make_bars("DOWN.SH", start_price=60.0, drift=-0.2))

    assert up.score > down.score
    assert 0.0 <= down.score <= 100.0 and 0.0 <= up.score <= 100.0
    assert up.reasons, "an actionable score must explain itself"


def test_rule_based_scoring_flags_thin_history() -> None:
    result = score_for("SHORT.SH", make_bars("SHORT.SH", count=30))
    assert any("样本" in warning for warning in result.warnings)


def test_split_utf8_text_respects_byte_limit_for_chinese() -> None:
    chunks = split_utf8_text("中文测试内容\n" * 20, 60)
    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-8")) <= 60 for chunk in chunks)


def test_file_notifier_reports_failure_instead_of_raising(tmp_path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    notifier = FileNotifier(blocker / "sub", slug="x")

    assert notifier.send("t", "c") is False


def test_post_close_then_pre_open_round_trip(tmp_path) -> None:
    bars = {"AAA.SH": make_bars("AAA.SH"), "BBB.SZ": make_bars("BBB.SZ", start_price=30.0, drift=0.05)}
    adapter = FakeAdapter(bars)

    summary = run_post_close(
        adapter=adapter,
        symbols=["AAA.SH", "BBB.SZ"],
        output_dir=tmp_path,
        notifiers=[],
    )

    assert summary["scored"] == 2
    matrix_path = tmp_path / "decision_matrix_latest.json"
    assert matrix_path.exists()

    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    assert [c["symbol"] for c in matrix["candidates"]]
    assert all(c["research_score"] is not None for c in matrix["candidates"])
    assert matrix["position_band_factor"] == 0.5

    calls_after_post_close = adapter.calls
    pre_open = run_pre_open(matrix_path=matrix_path, notifiers=[])

    assert pre_open["candidates"] == len(matrix["candidates"])
    assert "09:24:58" in pre_open["content"], "the auction cutoff rule must be on the pre-open card"
    assert adapter.calls == calls_after_post_close, "pre-open must reuse the snapshot, not refetch"


def test_post_close_notes_missing_symbols_and_free_tier_limits(tmp_path) -> None:
    adapter = FakeAdapter({"AAA.SH": make_bars("AAA.SH")})

    summary = run_post_close(
        adapter=adapter,
        symbols=["AAA.SH", "GONE.SZ"],
        output_dir=tmp_path,
        notifiers=[],
    )

    notes = " ".join(summary["data_notes"])
    assert "GONE.SZ" in notes
    assert "免费档" in notes
    assert "前复权" in notes
    assert summary["scored"] == 1 and summary["requested"] == 2


def test_pre_open_survives_a_corrupt_snapshot_file(tmp_path) -> None:
    adapter = FakeAdapter({"AAA.SH": make_bars("AAA.SH")})
    run_post_close(adapter=adapter, symbols=["AAA.SH"], output_dir=tmp_path, notifiers=[])
    (tmp_path / "snapshots_latest.json").write_text("{not json", encoding="utf-8")

    summary = run_pre_open(matrix_path=tmp_path / "decision_matrix_latest.json", notifiers=[])

    assert summary["candidates"] == 1
    assert any("快照读取失败" in note for note in summary["data_notes"])
