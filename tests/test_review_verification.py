"""复盘必须等待真实、完整的兑现窗口，坏记录不能影响其他标的。"""

from __future__ import annotations

from datetime import date

import pytest

from ashare_decision_support.cli import build_parser
from ashare_decision_support.jobs.review import run_review
from ashare_decision_support.journal import Journal, Recommendation, verify_pending
from ashare_decision_support.state.store import append_jsonl

SIGNAL_DAY = date(2026, 8, 17)
REVIEW_DAY = date(2026, 8, 24)
SYMBOL = "600519.SH"


def _bars() -> list[dict]:
    return [
        {
            "date": f"2026-08-{day}",
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 100.0 if day == 17 else 102.0,
        }
        for day in (17, 18, 19, 20, 21, 24)
    ]


class Adapter:
    def __init__(self, by_symbol: dict):
        self.by_symbol = by_symbol
        self.calls = 0

    def get_historical_bars(self, symbols, start, end):
        self.calls += 1
        return {symbol: self.by_symbol.get(symbol, []) for symbol in symbols}


def _book(tmp_path, symbols=(SYMBOL,)) -> Journal:
    book = Journal(root=tmp_path / "journal")
    for symbol in symbols:
        book.record(
            Recommendation(
                symbol=symbol,
                trade_date=str(SIGNAL_DAY),
                source="morning",
                reference_price=100,
                entry_high_pct=0,
                stop_pct=-0.04,
                target_pcts=[0.06],
            )
        )
    return book


def test_verification_does_not_use_bars_after_the_review_date(tmp_path) -> None:
    book = _book(tmp_path)
    adapter = Adapter({SYMBOL: _bars()})

    result = verify_pending(book, adapter, today=date(2026, 8, 19))

    assert result["resolved"] == 0
    assert not book.outcomes()
    assert "窗口未满" in " ".join(result["notes"])
    assert result["deferred"][0]["symbol"] == SYMBOL


def test_elapsed_calendar_days_do_not_complete_a_short_window(tmp_path) -> None:
    book = _book(tmp_path)
    adapter = Adapter({SYMBOL: _bars()[:2]})

    result = verify_pending(book, adapter, today=date(2026, 9, 10))

    assert result["resolved"] == 0
    assert len(book.pending(date(2026, 9, 10))) == 1
    assert "窗口未满" in " ".join(result["notes"])


def test_duplicate_dates_cannot_fill_the_holding_window(tmp_path) -> None:
    book = _book(tmp_path)
    rows = _bars()[:2]
    rows.extend([{"timestamp": "2026-08-18T15:00:00", **_bars()[1]}] * 4)

    result = verify_pending(book, Adapter({SYMBOL: rows}), today=REVIEW_DAY)

    assert result["resolved"] == 0


def test_corrected_bar_overrides_same_day_and_only_counts_once(tmp_path) -> None:
    book = _book(tmp_path)
    rows = _bars()
    rows.append({**rows[1], "timestamp": "2026-08-18T15:00:00", "close": 104.0})

    result = verify_pending(book, Adapter({SYMBOL: rows}), today=REVIEW_DAY)

    assert result["resolved"] == 1
    assert result["outcomes"][0]["sessions"] == 5
    assert result["outcomes"][0]["next_close_pct"] == pytest.approx(0.04)


def test_missing_signal_close_is_not_replaced_by_an_older_day(tmp_path) -> None:
    book = _book(tmp_path)
    rows = _bars()
    rows[0] = {**rows[0], "date": "2026-08-14", "close": 50.0}

    result = verify_pending(book, Adapter({SYMBOL: rows}), today=REVIEW_DAY)

    assert result["resolved"] == 0
    assert "建议日收盘" in " ".join(result["notes"])


@pytest.mark.parametrize("bad_price", [None, "bad", "nan", "inf", -1, 0, True, 10**400])
def test_invalid_anchor_is_deferred_without_blocking_healthy_symbols(tmp_path, bad_price) -> None:
    book = _book(tmp_path, (SYMBOL, "000001.SZ"))
    rows = _bars()
    rows[0]["close"] = bad_price
    adapter = Adapter({SYMBOL: rows, "000001.SZ": _bars()})

    result = verify_pending(book, adapter, today=REVIEW_DAY)

    assert result["resolved"] == 1
    assert result["outcomes"][0]["symbol"] == "000001.SZ"
    assert result["deferred"][0]["symbol"] == SYMBOL


@pytest.mark.parametrize(
    "broken",
    [
        {"high": "bad"},
        {"low": -1},
        {"close": "nan"},
        {"open": True},
        {"high": 98.0},
        {"close": 106.0},
    ],
)
def test_invalid_window_bar_waits_for_repair_instead_of_skipping_a_session(
    tmp_path, broken
) -> None:
    book = _book(tmp_path)
    rows = _bars()
    rows[1].update(broken)
    rows.append({**rows[-1], "date": "2026-08-25"})
    adapter = Adapter({SYMBOL: rows})

    result = verify_pending(book, adapter, today=date(2026, 8, 25))

    assert result["resolved"] == 0
    assert "价格无效" in " ".join(result["notes"])

    adapter.by_symbol[SYMBOL] = _bars()
    repaired = verify_pending(book, adapter, today=date(2026, 8, 25))
    assert repaired["resolved"] == 1
    assert repaired["outcomes"][0]["sessions"] == 5


def test_missing_symbol_in_a_hand_edited_journal_does_not_crash_review(tmp_path) -> None:
    book = _book(tmp_path)
    append_jsonl(
        book.root / "2026-08.jsonl",
        {
            "kind": "recommendation",
            "trade_date": str(SIGNAL_DAY),
            "source": "morning",
        },
    )

    result = run_review(
        Adapter({SYMBOL: _bars()}),
        journal=book,
        notifiers=[],
        trade_date=REVIEW_DAY,
    )

    assert result["checked"] == 2
    assert result["resolved"] == 1
    assert len(result["deferred"]) == 1
    assert "代码" in result["content"]


@pytest.mark.parametrize("horizon", [0, -1, True, 1.5])
def test_invalid_horizon_is_rejected_before_fetching_or_writing(tmp_path, horizon) -> None:
    book = _book(tmp_path)
    adapter = Adapter({SYMBOL: _bars()})
    before = (book.root / "2026-08.jsonl").read_bytes()

    with pytest.raises(ValueError, match="正整数"):
        verify_pending(book, adapter, today=REVIEW_DAY, horizon=horizon)

    assert adapter.calls == 0
    assert (book.root / "2026-08.jsonl").read_bytes() == before


@pytest.mark.parametrize("horizon", ["0", "-1"])
def test_cli_rejects_invalid_horizon_during_argument_parsing(horizon, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["review", "--horizon", horizon])

    assert exc.value.code == 2
    assert "正整数" in capsys.readouterr().err


def test_empty_review_returns_no_deferred_rows(tmp_path) -> None:
    book = Journal(root=tmp_path / "journal")
    adapter = Adapter({})

    result = run_review(adapter, journal=book, notifiers=[], trade_date=REVIEW_DAY)

    assert result["deferred"] == []
    assert "没有待核对" in result["content"]
    assert adapter.calls == 0


def test_review_statistics_do_not_include_outcomes_from_a_later_date(tmp_path) -> None:
    book = _book(tmp_path)
    adapter = Adapter({SYMBOL: _bars()})
    resolved = verify_pending(book, adapter, today=REVIEW_DAY)["outcomes"][0]
    path = book.root / "2026-08.jsonl"
    append_jsonl(path, {**resolved, "symbol": "000001.SZ", "resolved_on": "2026-08-31"})
    append_jsonl(path, {**resolved, "trade_date": "2026-08-25", "resolved_on": "2026-08-31"})

    result = run_review(adapter, journal=book, notifiers=[], trade_date=REVIEW_DAY)

    assert result["total_outcomes"] == 1


@pytest.mark.parametrize(
    "field", ["dip", "market_state", "trade_flow", "adjudication", "macro_context"]
)
def test_malformed_recommendation_context_is_reported_before_statistics(tmp_path, field) -> None:
    book = _book(tmp_path)
    row = book.recommendations()[0]
    append_jsonl(book.root / "2026-08.jsonl", {**row, "symbol": "000001.SZ", field: "bad"})

    result = run_review(
        Adapter({SYMBOL: _bars(), "000001.SZ": _bars()}),
        journal=book,
        notifiers=[],
        trade_date=REVIEW_DAY,
    )

    assert result["resolved"] == 1
    assert result["deferred"][0]["symbol"] == "000001.SZ"
    assert field in result["deferred"][0]["reason"]
