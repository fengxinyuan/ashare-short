"""在真实线程和独立进程间验证持仓与历史合并不会丢更新。"""

from __future__ import annotations

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from ashare_decision_support.adapters.daily_cache import DailyBarsCache
from ashare_decision_support.adapters.overseas_history import INSTRUMENTS
from ashare_decision_support.adapters.overseas_history import _write_series as write_overseas
from ashare_decision_support.adapters.overseas_history import load_series as load_overseas
from ashare_decision_support.adapters.ths_sector_index import _write_series as write_sector
from ashare_decision_support.adapters.ths_sector_index import load_series as load_sector
from ashare_decision_support.sectors.base import SectorMetrics, SectorOutlook
from ashare_decision_support.sectors.history import SectorRotationStore
from ashare_decision_support.state.positions import Positions
from ashare_decision_support.state.store import read_json


def _add_position(path: Path, index: int, ready) -> None:
    book = Positions(path)
    ready.wait(timeout=10)
    book.open(f"60000{index}", 100 + index, shares=100)


def _merge_bars(root: Path, index: int, ready) -> None:
    cache = DailyBarsCache(root=root)
    ready.wait(timeout=10)
    for offset in range(5):
        day = date(2026, 8, 1) + timedelta(days=5 * index + offset)
        cache.write("600519.SH", [{"date": str(day), "close": 100 + index}])


def _merge_sector_history(root: Path, index: int, ready) -> None:
    ready.wait(timeout=10)
    for offset in range(5):
        day = date(2026, 8, 1) + timedelta(days=5 * index + offset)
        assert write_sector(
            root, "881101", "半导体", [{"date": day.strftime("%Y%m%d"), "close": 100 + index}]
        )


def _merge_overseas_history(root: Path, index: int, ready) -> None:
    ready.wait(timeout=10)
    for offset in range(5):
        day = date(2026, 8, 1) + timedelta(days=5 * index + offset)
        assert write_overseas(
            root, INSTRUMENTS["sox"], [{"date": day.strftime("%Y%m%d"), "close": 100 + index}]
        )


def _record_sector_memory(path: Path, index: int, ready) -> None:
    book = SectorRotationStore(path)
    outlook = SectorOutlook("电子", 70.0, SectorMetrics("电子", 10, 10))
    ready.wait(timeout=10)
    for day in range(1, 6):
        book.record(date(2026, 8, day), f"SW{index + 1}", [outlook])


def _run_concurrently(mode: str, worker, path: Path) -> None:
    if mode == "threads":
        ready = Barrier(4)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(worker, path, index, ready) for index in range(4)]
            for future in futures:
                future.result(timeout=15)
        return

    # spawn 同时覆盖 Windows 使用的启动方式，避免继承父进程的锁。
    context = multiprocessing.get_context("spawn")
    ready = context.Barrier(4)
    processes = [context.Process(target=worker, args=(path, index, ready)) for index in range(4)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            process.close()


@pytest.mark.parametrize("mode", ["threads", "processes"])
def test_concurrent_positions_preserve_every_successful_update(tmp_path, mode) -> None:
    path = tmp_path / "positions.json"

    _run_concurrently(mode, _add_position, path)

    book = Positions(path)
    assert set(book.symbols()) == {f"60000{index}.SH" for index in range(4)}
    assert all(book.get(f"60000{index}").cost_price == 100 + index for index in range(4))


@pytest.mark.parametrize("mode", ["threads", "processes"])
def test_concurrent_cache_merges_preserve_every_session(tmp_path, mode) -> None:
    _run_concurrently(mode, _merge_bars, tmp_path)

    rows = DailyBarsCache(root=tmp_path).read("600519.SH", date.min, date.max)
    assert [row["date"] for row in rows] == [f"2026-08-{day:02d}" for day in range(1, 21)]
    assert [row["close"] for row in rows] == [100 + index for index in range(4) for _ in range(5)]


@pytest.mark.parametrize("mode", ["threads", "processes"])
@pytest.mark.parametrize(
    ("worker", "loader", "key"),
    [
        (_merge_sector_history, load_sector, "881101"),
        (_merge_overseas_history, load_overseas, "sox"),
    ],
    ids=["sector", "overseas"],
)
def test_concurrent_index_history_preserves_every_session(
    tmp_path, mode, worker, loader, key
) -> None:
    _run_concurrently(mode, worker, tmp_path)

    rows = loader(key, tmp_path)
    assert [row["date"] for row in rows] == [f"202608{day:02d}" for day in range(1, 21)]
    assert [row["close"] for row in rows] == [100 + index for index in range(4) for _ in range(5)]


@pytest.mark.parametrize("mode", ["threads", "processes"])
def test_concurrent_sector_memory_keeps_all_dates_and_levels(tmp_path, mode) -> None:
    path = tmp_path / "rotation.json"
    _run_concurrently(mode, _record_sector_memory, path)

    records = read_json(path)["records"]
    assert {(row["trade_date"], row["level"]) for row in records} == {
        (f"2026-08-{day:02d}", f"SW{level}") for day in range(1, 6) for level in range(1, 5)
    }
