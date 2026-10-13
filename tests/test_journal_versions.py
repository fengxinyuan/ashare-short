"""同日判断只追加，首次样本稳定；新旧台账都能完成独立核对。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime

import pytest

from ashare_decision_support.journal import Journal, Recommendation, summarize, verify_pending
from ashare_decision_support.journal.records import record_key
from ashare_decision_support.journal.stats import rolling_validation
from ashare_decision_support.state.store import append_jsonl

DAY = date(2026, 8, 3)
END = date(2026, 8, 4)


def recommendation(**kwargs):
    fields = {
        "symbol": "600519",
        "trade_date": str(DAY),
        "source": "morning",
        "reference_price": 100,
        "stop_pct": -0.04,
        "target_pcts": [0.06],
        "recorded_at": datetime(2026, 8, 3, 9).isoformat(),
    }
    return Recommendation(**(fields | kwargs))


class DailyBars:
    def get_historical_bars(self, symbols, start, end):
        self.requested = symbols
        return {
            symbol: [
                {
                    "date": str(DAY),
                    "open": 99,
                    "close": 100,
                    "high": 101,
                    "low": 99,
                    "volume": 1000,
                },
                {
                    "date": str(END),
                    "open": 101,
                    "close": 102,
                    "high": 103,
                    "low": 100,
                    "volume": 1000,
                },
            ]
            for symbol in symbols
        }


def test_changed_and_reverted_decisions_form_an_append_only_chain(tmp_path):
    book = Journal(tmp_path)
    first = recommendation()
    assert book.record_many([first]) == 1
    assert book.record_many([replace(first, symbol="sh600519", facts_hash="new-note-only")]) == 0
    assert book.record_many([replace(first, stop_pct=-0.02), first]) == 2
    rows = book.recommendations(DAY, DAY)
    assert len({row["decision_id"] for row in rows}) == 3
    assert [row["revision"] for row in rows] == [1, 2, 3]
    assert rows[1]["supersedes"] == record_key(rows[0])
    assert rows[2]["supersedes"] == record_key(rows[1])
    assert [row["stop_pct"] for row in rows] == [-0.04, -0.02, -0.04]


def test_all_versions_verify_but_repeat_runs_never_inflate_samples(tmp_path):
    book = Journal(tmp_path)
    first = recommendation(plan_id="plan-a", strategy_version="alpha")
    book.record_many(
        [first, replace(first, stop_pct=-0.03), replace(first, strategy_version="beta")]
    )
    result = verify_pending(book, DailyBars(), today=END, horizon=1)
    assert result["resolved"] == 3
    outcomes = book.outcomes(DAY, END)
    assert {row["decision_id"] for row in outcomes} == {
        row["decision_id"] for row in book.recommendations(DAY, END)
    }
    assert {row["plan_id"] for row in outcomes} == {"plan-a"}
    stats = summarize(outcomes)
    assert stats["samples"] == 1
    assert stats["decisions_recorded"] == 3
    assert {row["bucket"]: row["samples"] for row in stats["by_strategy_version"]} == {
        "alpha": 1,
        "beta": 1,
    }
    assert verify_pending(book, DailyBars(), today=END, horizon=1)["resolved"] == 0
    assert rolling_validation(outcomes)["windows"]["last_20_sessions"]["samples"] == 1


def test_a_later_resolved_revision_does_not_replace_an_unresolved_first(tmp_path):
    book = Journal(tmp_path)
    book.record_many([recommendation(adjudication=[]), recommendation(stop_pct=-0.02)])
    result = verify_pending(book, DailyBars(), today=END, horizon=1)
    assert result["resolved"] == 1
    assert result["deferred"][0]["revision"] == 1
    assert summarize(book.outcomes(DAY, END))["samples"] == 0
    assert summarize(book.outcomes(DAY, END))["by_strategy_version"] == []


def test_legacy_rows_keep_their_keys_and_new_revisions_do_not_replace_them(tmp_path):
    book = Journal(tmp_path)
    legacy = recommendation().to_dict()
    for key in (
        "decision_id",
        "recorded_at",
        "revision",
        "supersedes",
        "strategy_revision",
        "strategy_version",
        "plan_id",
    ):
        legacy.pop(key)
    legacy["symbol"] = "600519.SH"
    path = tmp_path / "2026-08.jsonl"
    append_jsonl(path, legacy)
    prefix = path.read_bytes()
    book.record(recommendation(stop_pct=-0.02))
    assert path.read_bytes().startswith(prefix)
    assert verify_pending(book, DailyBars(), today=END, horizon=1)["resolved"] == 2
    assert book.pending(END) == []
    assert summarize(book.outcomes(DAY, END))["samples"] == 1
    assert record_key(legacy) == "600519.SH|2026-08-03|morning"


def test_concurrent_identical_runs_write_one_decision(tmp_path):
    def record(_):
        return Journal(tmp_path).record_many([recommendation()])

    with ThreadPoolExecutor(max_workers=6) as pool:
        counts = list(pool.map(record, range(18)))
    assert sum(counts) == 1
    assert len(Journal(tmp_path).recommendations(DAY, DAY)) == 1


def test_an_explicit_decision_id_cannot_be_rewritten(tmp_path):
    book = Journal(tmp_path)
    row = recommendation(decision_id="fixed")
    book.record(row)
    assert book.record_many([row]) == 0
    with pytest.raises(ValueError, match="不同内容"):
        book.record(replace(row, stop_pct=-0.10))
    assert len(book.recommendations(DAY, DAY)) == 1


def test_repeated_ids_for_different_symbols_do_not_swallow_outcomes(tmp_path):
    book = Journal(tmp_path)
    book.record_many(
        [
            recommendation(decision_id="external"),
            recommendation(symbol="000001", decision_id="external"),
        ]
    )
    assert verify_pending(book, DailyBars(), today=END, horizon=1)["resolved"] == 2
    assert {row["symbol"] for row in book.outcomes(DAY, END)} == {"600519.SH", "000001.SZ"}


def test_legacy_compact_dates_and_symbol_aliases_join_the_resolved_outcome(tmp_path):
    book = Journal(tmp_path)
    row = recommendation().to_dict()
    row.update(symbol="sh600519", trade_date="20260803", decision_id=None)
    append_jsonl(tmp_path / "2026-08.jsonl", row)
    adapter = DailyBars()
    assert verify_pending(book, adapter, today=END, horizon=1)["resolved"] == 1
    assert adapter.requested == ["600519.SH"]
    assert book.pending(END) == []
