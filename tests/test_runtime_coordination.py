"""验证共享状态下的任务互斥，以及历史合并不会丢掉成功写入。"""

from __future__ import annotations

import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Event

import pytest

from ashare_decision_support.news.theme_history import ThemeOutcomeStore
from ashare_decision_support.sectors.confirmation_ledger import SectorConfirmationLedger
from ashare_decision_support.sectors.outcomes import SectorForecastOutcomeStore
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.runtime import exclusive_run
from ashare_decision_support.state.store import file_lock


def _hold_lock(path, ready, release) -> None:
    with file_lock(Path(path)):
        ready.set()
        if not release.wait(10):
            raise TimeoutError("测试未及时释放文件锁")


@pytest.mark.parametrize("name", ["monitor", "daemon"])
def test_running_job_rejects_another_instance(name) -> None:
    from ashare_decision_support.jobs.monitor import run_monitor
    from ashare_decision_support.schedule.daemon import run_daemon

    ready, release = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(_hold_lock, state_path("runtime", name), ready, release)
        try:
            assert ready.wait(5), "持锁线程未启动"
            with pytest.raises(RuntimeError, match=f"{name} 已有实例"):
                if name == "monitor":
                    run_monitor(object(), once=True, sectors=False, notifiers=[])
                else:
                    run_daemon([], max_ticks=0, notifiers=[])
        finally:
            release.set()
            holder.result(timeout=5)


def test_exclusive_run_detects_an_independent_process() -> None:
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    holder = context.Process(
        target=_hold_lock,
        args=(str(state_path("runtime", "probe")), ready, release),
    )
    holder.start()
    try:
        assert ready.wait(5), "持锁进程未启动"
        with pytest.raises(RuntimeError, match="probe 已有实例"):
            exclusive_run("probe")(lambda: None)()
    finally:
        release.set()
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)
    assert holder.exitcode == 0


def test_exclusive_run_releases_lock_after_success_and_failure() -> None:
    @exclusive_run("probe")
    def run(*, fail=False):
        if fail:
            raise ValueError("原任务异常")
        return "完成"

    with pytest.raises(ValueError, match="原任务异常"):
        run(fail=True)
    assert run() == run() == "完成"


def test_different_state_directories_do_not_share_runtime_lock(tmp_path, monkeypatch) -> None:
    original_path = state_path("runtime", "probe")
    with file_lock(original_path):
        monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "another-state"))
        assert exclusive_run("probe")(lambda: "完成")() == "完成"


def _record_history(arguments) -> None:
    kind, name, offset = arguments
    path = Path(name)
    day = date(2026, 8, 3) + timedelta(days=offset)
    if kind == "theme":
        ThemeOutcomeStore(path).record(day, [], {})
    elif kind == "forecast":
        SectorForecastOutcomeStore(path).record(day, [], {})
    else:
        SectorConfirmationLedger(path).record(
            {
                "target_session": str(day),
                "as_of": f"{day}T10:00:00",
                "results": [{"name": "电子", "status": "已确认"}],
            }
        )


@pytest.mark.parametrize("kind", ["theme", "forecast", "confirmation"])
@pytest.mark.parametrize("processes", [False, True], ids=["threads", "processes"])
def test_concurrent_history_updates_preserve_all_dates(tmp_path, kind, processes) -> None:
    path = tmp_path / f"{kind}.json"
    count = 20
    arguments = [(kind, str(path), offset) for offset in range(count)]
    pool = (
        ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn"))
        if processes
        else ThreadPoolExecutor(max_workers=4)
    )
    with pool:
        list(pool.map(_record_history, arguments, timeout=20))

    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["records"] if kind == "confirmation" else payload["daily"]
    field = "target_session" if kind == "confirmation" else "trade_date"
    expected = {str(date(2026, 8, 3) + timedelta(days=offset)) for offset in range(count)}
    assert len(rows) == count
    assert {row[field] for row in rows} == expected
