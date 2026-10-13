"""轮动记忆读取隔离坏记录，更新保留其他日期与分类。"""

from __future__ import annotations

from datetime import date

import pytest

from ashare_decision_support.jsoncache import write_stamped
from ashare_decision_support.sectors import history
from ashare_decision_support.sectors.base import SectorMetrics, SectorOutlook
from ashare_decision_support.state.store import read_json, write_json


def test_current_industry_rankings_do_not_mix_with_legacy_sw_rankings(tmp_path):
    store = history.SectorRotationStore(tmp_path / "rotation.json")
    store.record(date(2026, 7, 31), "SW1", [_outlook("银行")])
    store.record(date(2026, 8, 3), "SW1", [_outlook("银行")])

    summary = store.remember(date(2026, 8, 3), history.DEFAULT_LEVEL, [_outlook("银行")])

    assert summary.level == "THS_INDUSTRY"
    assert summary.available_days == 1
    assert summary.regime == "样本不足"
    assert summary.persistent == ()
    assert store.summarize(date(2026, 8, 3), "SW1").available_days == 2


def _outlook(name: str) -> SectorOutlook:
    return SectorOutlook(name=name, score=70.0, metrics=SectorMetrics(name, 10, 10))


def _record(day="2026-08-03", level="SW1", leaders=None):
    return {
        "trade_date": day,
        "level": level,
        "leaders": leaders if leaders is not None else [{"name": "电子"}],
    }


@pytest.mark.parametrize("payload", [[], ["bad"], "bad", 42])
def test_wrong_root_shape_can_be_read_and_rebuilt(tmp_path, payload) -> None:
    path = tmp_path / "rotation.json"
    write_json(path, payload)
    store = history.SectorRotationStore(path)

    summary = store.summarize(date(2026, 8, 3), "SW1")
    assert summary.available_days == 0
    assert summary.regime == "样本不足"

    store.record(date(2026, 8, 3), "SW1", [_outlook("电子")])
    assert store.summarize(date(2026, 8, 3), "SW1").available_days == 1


def test_bad_records_do_not_hide_valid_dates_and_levels(tmp_path) -> None:
    path = tmp_path / "rotation.json"
    assert write_stamped(
        path,
        {
            "records": [
                None,
                "bad",
                12,
                [],
                {},
                _record(day="invalid"),
                _record(level=[]),
                _record(),
                _record(level="SW2"),
                _record(day="2026-08-04"),
            ]
        },
        history.HISTORY_SCHEMA,
    )
    store = history.SectorRotationStore(path)

    assert store.summarize(date(2026, 8, 3), "SW1").available_days == 1
    store.record(date(2026, 8, 3), "SW1", [_outlook("银行")])

    payload = read_json(path)
    assert {(row["trade_date"], row["level"]) for row in payload["records"]} == {
        ("2026-08-03", "SW1"),
        ("2026-08-03", "SW2"),
        ("2026-08-04", "SW1"),
    }
    assert store.summarize(date(2026, 8, 4), "SW1").available_days == 2


@pytest.mark.parametrize("leaders", [None, "broken", 42, {"name": "电子"}])
def test_bad_leader_collection_is_missing_coverage(tmp_path, leaders) -> None:
    path = tmp_path / "rotation.json"
    assert write_stamped(
        path, {"records": [{**_record(), "leaders": leaders}]}, history.HISTORY_SCHEMA
    )

    summary = history.SectorRotationStore(path).summarize(date(2026, 8, 3), "SW1")

    assert summary.regime == "样本不足"
    assert summary.available_days == 1
    assert summary.overlap_with_yesterday is None


def test_invalid_and_duplicate_leaders_do_not_distort_overlap(tmp_path) -> None:
    path = tmp_path / "rotation.json"
    leaders = [
        None,
        "bad",
        {},
        {"name": True},
        {"name": "  "},
        {"name": " 电子 "},
        {"name": "电子"},
        {"name": "银行"},
    ]
    assert write_stamped(
        path,
        {
            "records": [
                _record(day="2026-07-31", leaders=leaders),
                _record(leaders=[{"name": "电子"}, {"name": "银行"}]),
            ]
        },
        history.HISTORY_SCHEMA,
    )

    summary = history.SectorRotationStore(path).summarize(date(2026, 8, 3), "SW1", leader_count=2)

    assert summary.overlap_with_yesterday == 1.0
    assert summary.persistent == ("电子", "银行")
    assert summary.new_entries == summary.faded == ()


def test_missing_prior_ranking_is_not_reported_as_accelerating_rotation(tmp_path) -> None:
    path = tmp_path / "rotation.json"
    assert write_stamped(
        path,
        {
            "records": [
                _record(day="2026-07-31", leaders=[]),
                _record(),
            ]
        },
        history.HISTORY_SCHEMA,
    )

    summary = history.SectorRotationStore(path).summarize(date(2026, 8, 3), "SW1")

    assert summary.regime == "样本不足"
    assert summary.overlap_with_yesterday is None
    assert summary.new_entries == ()


def test_date_aliases_count_once_and_updates_replace_that_day(tmp_path) -> None:
    path = tmp_path / "rotation.json"
    assert write_stamped(
        path,
        {
            "records": [
                _record(day="2026-07-31"),
                _record(day="20260731"),
                _record(),
            ]
        },
        history.HISTORY_SCHEMA,
    )
    store = history.SectorRotationStore(path)

    assert store.summarize(date(2026, 8, 3), "SW1").available_days == 2
    store.record(date(2026, 7, 31), "SW1", [_outlook("银行")])

    records = read_json(path)["records"]
    assert [row["trade_date"] for row in records] == ["2026-07-31", "2026-08-03"]
    assert records[0]["leaders"][0]["name"] == "银行"


def test_failed_memory_save_preserves_history_and_releases_lock(tmp_path, monkeypatch) -> None:
    path = tmp_path / "rotation.json"
    store = history.SectorRotationStore(path)
    store.record(date(2026, 7, 31), "SW1", [_outlook("电子")])
    before = path.read_bytes()

    def fail_save(*args):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(history, "write_json", fail_save)
        with pytest.raises(OSError, match="disk full"):
            store.record(date(2026, 8, 3), "SW2", [_outlook("银行")])
    assert path.read_bytes() == before

    store.record(date(2026, 8, 3), "SW2", [_outlook("银行")])
    assert len(read_json(path)["records"]) == 2
