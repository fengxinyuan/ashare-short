from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from ashare_decision_support.jsoncache import (
    merge_series,
    read_series,
    read_stamped,
    series_bars,
    write_stamped,
)
from ashare_decision_support.state import store


def _bar(stamp: str, close: float = 100.0) -> dict:
    return {"date": stamp, "close": close}


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_invalid_numeric_cache_is_not_reused(tmp_path, literal) -> None:
    path = tmp_path / "cache.json"
    path.write_text('{"schema": 1, "price": ' + literal + "}", encoding="utf-8")

    assert read_stamped(path, 1) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_failed_serialization_preserves_last_good_cache(tmp_path, value) -> None:
    path = tmp_path / "cache.json"
    assert write_stamped(path, {"price": 12.5}, 1)
    before = path.read_bytes()

    assert write_stamped(path, {"price": value}, 1) is False
    assert path.read_bytes() == before
    assert [item for item in tmp_path.iterdir() if item.is_file()] == [path]


def test_schema_argument_cannot_be_overridden_by_payload(tmp_path) -> None:
    path = tmp_path / "cache.json"

    assert write_stamped(path, {"schema": "old", "price": 12.5}, "current")
    assert read_stamped(path, "current")["price"] == 12.5


def test_same_process_writers_have_independent_temporary_files(tmp_path, monkeypatch) -> None:
    path = tmp_path / "cache.json"
    barrier = Barrier(2, timeout=5)
    replace = store.os.replace
    temporary_paths = []

    def concurrent_replace(source, destination):
        import sys
        import time

        temporary_paths.append(source)
        barrier.wait()
        for attempt in range(10):
            try:
                replace(source, destination)
                break
            except PermissionError:
                if sys.platform == "win32" and attempt < 9:
                    time.sleep(0.01)
                    continue
                raise

    monkeypatch.setattr(store.os, "replace", concurrent_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: write_stamped(path, {"price": value}, 1), [10, 20]))

    assert results == [True, True]
    assert len(set(temporary_paths)) == 2
    assert read_stamped(path, 1)["price"] in (10, 20)
    assert [item for item in tmp_path.iterdir() if item.is_file()] == [path]


def test_replace_failure_preserves_old_file_and_removes_temporary(tmp_path, monkeypatch) -> None:
    path = tmp_path / "cache.json"
    assert write_stamped(path, {"price": 10}, 1)

    def fail_replace(*args):
        raise OSError("disk error")

    monkeypatch.setattr(store.os, "replace", fail_replace)

    assert write_stamped(path, {"price": 20}, 1) is False
    assert read_stamped(path, 1)["price"] == 10
    assert [item for item in tmp_path.iterdir() if item.is_file()] == [path]


def test_bad_utf8_and_overflow_jsonl_rows_do_not_hide_good_records(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_bytes(b'{"value": 1}\n{"text": "\xff"}\n{"value": 1e999}\n{"value": 2}\n')

    assert store.read_jsonl(path) == [{"value": 1}, {"value": 2}]


def test_series_bars_drops_rows_that_cannot_be_ordered() -> None:
    payload = {"bars": [_bar("20260803"), {"close": 1.0}, "junk", {"date": ""}, _bar("20260804")]}

    assert series_bars(payload) == [_bar("20260803"), _bar("20260804")]
    assert series_bars({"bars": "not-a-list"}) == []
    assert series_bars(None) == []


def test_merge_series_orders_by_date_and_lets_later_rows_win(tmp_path) -> None:
    path = tmp_path / "series.json"

    def header(stored):
        return {"code": "881101"}

    assert merge_series(path, "v1", [_bar("20260804"), _bar("20260803")], header)
    assert merge_series(path, "v1", [_bar("20260804", close=105.0), _bar("20250101")], header)

    assert read_series(path, "v1") == [
        _bar("20250101"),
        _bar("20260803"),
        _bar("20260804", close=105.0),
    ]
    assert read_stamped(path, "v1")["code"] == "881101"


def test_merge_series_header_sees_the_stored_payload_inside_the_lock(tmp_path) -> None:
    path = tmp_path / "series.json"
    seen = []

    def header(stored):
        seen.append(stored)
        return {"name": (stored or {}).get("name") or "新名字"}

    assert merge_series(path, "v1", [_bar("20260803")], header)
    assert merge_series(path, "v1", [_bar("20260804")], header)

    assert seen[0] is None
    assert seen[1]["name"] == "新名字"
    assert read_stamped(path, "v1")["name"] == "新名字"


def test_merge_series_replaces_a_stale_schema_instead_of_merging(tmp_path) -> None:
    path = tmp_path / "series.json"
    assert write_stamped(path, {"bars": [_bar("20200101")]}, "v0")

    assert merge_series(path, "v1", [_bar("20260803")], lambda stored: {})

    assert read_series(path, "v1") == [_bar("20260803")]
    assert read_series(path, "v0") == []


def test_merge_series_refuses_empty_input_and_missing_path(tmp_path) -> None:
    path = tmp_path / "series.json"

    assert merge_series(path, "v1", [], lambda stored: {}) is False
    assert merge_series(None, "v1", [_bar("20260803")], lambda stored: {}) is False
    assert not path.exists()
    assert read_series(None, "v1") == []


def test_merge_series_reports_lock_and_save_failures_without_touching_the_file(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "series.json"
    assert merge_series(path, "v1", [_bar("20260803")], lambda stored: {})
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(store.os, "replace", fail_replace)
        assert merge_series(path, "v1", [_bar("20260804")], lambda stored: {}) is False
    assert path.read_bytes() == before

    blocked = tmp_path / "file_not_dir"
    blocked.write_text("keep", encoding="utf-8")
    assert (
        merge_series(blocked / "series.json", "v1", [_bar("20260804")], lambda stored: {}) is False
    )
    assert blocked.read_text(encoding="utf-8") == "keep"
