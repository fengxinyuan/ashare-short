"""历史刷新必须保留累积数据，并区分取数成功、保存失败和旧缓存可用。"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from ashare_decision_support import cli
from ashare_decision_support.adapters import overseas_history as overseas
from ashare_decision_support.adapters import ths_sector_index as sector
from ashare_decision_support.jsoncache import read_stamped, write_stamped
from ashare_decision_support.state import store


@pytest.fixture(params=["sector", "overseas"])
def history(request):
    if request.param == "sector":
        return (
            sector,
            "881101",
            lambda root, bars: sector._write_series(root, "881101", "半导体", bars),
        )
    return (
        overseas,
        "sox",
        lambda root, bars: overseas._write_series(root, overseas.INSTRUMENTS["sox"], bars),
    )


def _bars(count=1, start=date(2026, 8, 3)):
    return [
        {
            "date": (start + timedelta(days=offset)).strftime("%Y%m%d"),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1000.0,
            "amount": 100000.0,
        }
        for offset in range(count)
    ]


def test_history_merge_keeps_older_years_and_replaces_same_day(tmp_path, history) -> None:
    module, key, write = history
    assert write(tmp_path, _bars(start=date(2025, 12, 31)))
    assert write(tmp_path, _bars())
    correction = {**_bars()[0], "close": 105.0}

    assert write(tmp_path, [correction])

    assert module.load_series(key, tmp_path) == [*_bars(start=date(2025, 12, 31)), correction]


def test_failed_history_replace_preserves_old_file_and_allows_retry(
    tmp_path, history, monkeypatch
) -> None:
    module, key, write = history
    assert write(tmp_path, _bars())
    path = tmp_path / f"{key}.json"
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("disk full")

    fresh = _bars(start=date(2026, 8, 4))
    with monkeypatch.context() as patch:
        patch.setattr(store.os, "replace", fail_replace)
        assert write(tmp_path, fresh) is False
    assert path.read_bytes() == before
    assert not [
        entry
        for entry in tmp_path.iterdir()
        if entry.is_file() and entry.suffix not in (".json", ".lock")
    ]

    assert write(tmp_path, fresh)
    assert module.load_series(key, tmp_path) == [*_bars(), *fresh]


def test_unavailable_history_directory_is_a_reported_write_failure(tmp_path, history) -> None:
    _module, _key, write = history
    blocked = tmp_path / "file_instead_of_directory"
    blocked.write_text("keep", encoding="utf-8")

    assert write(blocked, _bars()) is False
    assert blocked.read_text(encoding="utf-8") == "keep"


def test_history_lock_failure_preserves_old_file(tmp_path, history, monkeypatch) -> None:
    _module, key, write = history
    assert write(tmp_path, _bars())
    path = tmp_path / f"{key}.json"
    before = path.read_bytes()
    original_open = open

    def deny_lock(file, *args, **kwargs):
        if str(file).endswith(".lock"):
            raise PermissionError("lock unavailable")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", deny_lock)
    assert write(tmp_path, _bars(start=date(2026, 8, 4))) is False
    assert path.read_bytes() == before


def test_nameless_backfill_preserves_latest_sector_name(tmp_path) -> None:
    assert sector._write_series(tmp_path, "881101", "最新行业名", _bars())

    assert sector._write_series(tmp_path, "881101", None, _bars(start=date(2025, 12, 31)))

    payload = read_stamped(tmp_path / "881101.json", sector.HISTORY_SCHEMA)
    assert payload["name"] == "最新行业名"
    assert len(payload["bars"]) == 2


def test_backfill_coverage_only_includes_requested_sectors(tmp_path, monkeypatch) -> None:
    assert sector._write_series(tmp_path, "881102", "其他行业", _bars(5))
    adapter = sector.ThsSectorIndexAdapter(history_dir=tmp_path)
    monkeypatch.setattr(adapter, "_fetch_year", lambda job: [])

    result = adapter.backfill(["881101"], since_year=2026, until_year=2026)

    assert result.ok is False
    assert result.codes == result.bars_after == 0
    assert result.earliest is result.latest is None
    assert len(sector.load_series("881102", tmp_path)) == 5


def test_backfill_exposes_failed_saves_and_keeps_other_sectors(tmp_path, monkeypatch) -> None:
    adapter = sector.ThsSectorIndexAdapter(history_dir=tmp_path)
    monkeypatch.setattr(adapter, "_fetch_year", lambda job: _bars())
    write = sector._write_series
    monkeypatch.setattr(
        sector,
        "_write_series",
        lambda root, code, name, bars: False if code == "881101" else write(root, code, name, bars),
    )

    result = adapter.backfill(["881101", "881102"], since_year=2026, until_year=2026)

    assert result.codes == 1
    assert result.bars_after == 1
    assert result.failures == 0
    assert result.to_dict()["write_failures"] == 1
    assert any("保存失败" in note and "881101" in note for note in result.notes)


def test_live_sector_readings_survive_failed_history_saves(tmp_path, monkeypatch) -> None:
    adapter = sector.ThsSectorIndexAdapter(history_dir=tmp_path)
    monkeypatch.setattr(adapter, "_fetch_one", lambda code: ("半导体", _bars(26)))
    monkeypatch.setattr(sector, "_write_series", lambda *args: False)

    result = adapter.fetch(["881101"])

    assert result.ok
    assert result.readings[0].close == 100.0
    assert adapter.history_written == 0
    assert any("保存失败" in note for note in result.notes)


def test_sector_history_count_describes_the_current_fetch(tmp_path, monkeypatch) -> None:
    adapter = sector.ThsSectorIndexAdapter(history_dir=tmp_path)
    monkeypatch.setattr(adapter, "_fetch_one", lambda code: ("半导体", _bars(26)))
    assert adapter.fetch(["881101"]).ok
    assert adapter.history_written == 1

    assert adapter.fetch([]).ok is False
    assert adapter.history_written == 0


@pytest.mark.parametrize("operation", ["fetch", "backfill"])
def test_repeated_sector_codes_do_not_duplicate_requests_or_readings(
    tmp_path, monkeypatch, operation
) -> None:
    adapter = sector.ThsSectorIndexAdapter(history_dir=tmp_path)
    fetched = []

    def fetch(code):
        fetched.append(code)
        return "半导体", _bars(26)

    monkeypatch.setattr(adapter, "_fetch_one", fetch)
    monkeypatch.setattr(adapter, "_fetch_year", lambda job: fetch(job[0])[1])
    if operation == "fetch":
        result = adapter.fetch(iter(["881101", "881101"]))
        assert len(result.readings) == 1
    else:
        result = adapter.backfill(iter(["881101", "881101"]), since_year=2026, until_year=2026)
        assert result.bars_after == 26
        assert result.codes == 1
    assert fetched == ["881101"]


def test_overseas_failed_saves_are_separate_from_cached_coverage(tmp_path, monkeypatch) -> None:
    assert overseas._write_series(tmp_path, overseas.INSTRUMENTS["sox"], _bars())
    adapter = overseas.OverseasHistoryAdapter(history_dir=tmp_path)
    monkeypatch.setattr(adapter, "_fetch_one", lambda instrument: _bars(start=date(2026, 8, 4)))
    write = overseas._write_series
    monkeypatch.setattr(
        overseas,
        "_write_series",
        lambda root, instrument, bars: (
            False if instrument.key == "sox" else write(root, instrument, bars)
        ),
    )

    result = adapter.refresh(["sox", "sp500"])

    assert result.ok
    assert result.failures == 0
    assert result.to_dict()["write_failures"] == 1
    assert result.to_dict()["updated_keys"] == ["sp500"]
    assert result.latest == {"sox": "20260803", "sp500": "20260804"}
    assert any("保存失败" in note and "sox" in note for note in result.notes)


@pytest.mark.parametrize("as_iterator", [False, True])
def test_overseas_selection_fetches_each_key_once_and_reports_unknowns(
    tmp_path, monkeypatch, as_iterator
) -> None:
    adapter = overseas.OverseasHistoryAdapter(history_dir=tmp_path)
    fetched = []

    def fetch(instrument):
        fetched.append(instrument.key)
        return _bars()

    monkeypatch.setattr(adapter, "_fetch_one", fetch)
    keys = ["sox", "unknown", "sox"]

    result = adapter.refresh(iter(keys) if as_iterator else keys)

    assert fetched == ["sox"]
    assert result.keys == ("sox",)
    assert any("unknown" in note for note in result.notes)


def test_explicit_empty_overseas_selection_does_not_fetch_every_instrument(
    tmp_path, monkeypatch
) -> None:
    adapter = overseas.OverseasHistoryAdapter(history_dir=tmp_path)
    fetched = []
    monkeypatch.setattr(
        adapter, "_fetch_one", lambda instrument: fetched.append(instrument.key) or []
    )

    result = adapter.refresh([])

    assert not fetched
    assert result.keys == ()
    assert result.ok is False


def test_default_overseas_selection_still_refreshes_every_instrument(tmp_path, monkeypatch) -> None:
    adapter = overseas.OverseasHistoryAdapter(history_dir=tmp_path)
    monkeypatch.setattr(adapter, "_fetch_one", lambda instrument: _bars())

    result = adapter.refresh()

    assert result.keys == tuple(overseas.INSTRUMENTS)
    assert set(result.updated_keys) == set(overseas.INSTRUMENTS)
    assert result.bars_after == dict.fromkeys(overseas.INSTRUMENTS, 1)


def test_empty_overseas_response_is_visible_with_cached_history(tmp_path, monkeypatch) -> None:
    assert overseas._write_series(tmp_path, overseas.INSTRUMENTS["sox"], _bars())
    adapter = overseas.OverseasHistoryAdapter(history_dir=tmp_path)
    monkeypatch.setattr(adapter, "_fetch_one", lambda instrument: [])

    result = adapter.refresh(["sox"])

    assert result.ok
    assert result.to_dict()["updated_keys"] == []
    assert any("未返回可用日线" in note and "sox" in note for note in result.notes)


def test_wrong_history_schema_is_replaced_without_merging(tmp_path, history) -> None:
    module, key, write = history
    assert write_stamped(tmp_path / f"{key}.json", {"bars": _bars(start=date(2020, 1, 1))}, "old")

    assert write(tmp_path, _bars())

    assert module.load_series(key, tmp_path) == _bars()


@pytest.mark.parametrize("failed_target", ["sector", "overseas"])
def test_history_refresh_cli_reports_partial_save_failure_and_finishes_other_source(
    tmp_path,
    monkeypatch,
    capsys,
    failed_target,
) -> None:
    roots = {name: tmp_path / name for name in ("sector", "overseas")}
    roots[failed_target].write_text("keep", encoding="utf-8")
    sector_adapter = sector.ThsSectorIndexAdapter(history_dir=roots["sector"])
    overseas_adapter = overseas.OverseasHistoryAdapter(history_dir=roots["overseas"])
    monkeypatch.setattr(sector_adapter, "_codes", lambda notes: ["881101"])
    monkeypatch.setattr(sector_adapter, "_fetch_year", lambda job: _bars())
    monkeypatch.setattr(overseas_adapter, "_fetch_one", lambda instrument: _bars())
    monkeypatch.setattr(cli, "ThsSectorIndexAdapter", lambda: sector_adapter)
    monkeypatch.setattr(cli, "OverseasHistoryAdapter", lambda: overseas_adapter)
    monkeypatch.setattr(cli, "load_env_files", lambda: None)
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "ashare-ds",
            "history-refresh",
            "--since-year",
            "2026",
            "--until-year",
            "2026",
            "--keys",
            "sox",
        ],
    )

    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"] == ["sector-index", "overseas"]
    for name, output_key in (("sector", "sector_index"), ("overseas", "overseas")):
        result = payload[output_key]
        assert result["ok"] is (name != failed_target)
        assert result["failures"] == 0
        assert result["write_failures"] == int(name == failed_target)
    assert any("保存失败" in note for note in payload["notes"])
    assert roots[failed_target].read_text(encoding="utf-8") == "keep"
