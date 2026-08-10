"""`daemon` 的任务接线：每个任务真的能调起它那条主链路吗。

**为什么单独测这个。** `jobs/daily_cycle.py` 是**唯一**用这套关键字去调
`run_morning` / `run_advise` / `run_monitor` / `run_post_close` / `run_review`
的地方——`cli.py` 有它自己的一组调用点。于是这两边的签名一旦漂移（有人给
`run_advise` 改了个参数名、或者把某个参数变成必填），CLI 那条路照常绿，而
daemon 这条路要到**某个交易日 09:15 无人值守跑到那一步**才炸。

这个文件此前一行测试都没有（覆盖率 33%，未覆盖的正好是全部任务闭包）。下面的
用例不测业务结果，只测**接线**：闭包能不能调起来、传过去的参数在真实签名下是否
合法、以及那条「构造延迟到执行时刻」的承诺还成不成立。
"""

from __future__ import annotations

import inspect
from datetime import date
from typing import Any

import pytest

from ashare_decision_support.jobs import daily_cycle
from ashare_decision_support.jobs.daily_cycle import run_daily_cycle

TRADE_DATE = date(2026, 8, 7)

# 任务 key → 它应该调起的那个函数名（在 `daily_cycle` 命名空间里）
TASK_TARGET = {
    "morning": "run_morning",
    "preopen": "run_advise",
    "monitor": "run_monitor",
    "postclose": "run_post_close",
    "review": "run_review",
}


class _Calendar:
    def is_trading_day(self, _day: date) -> bool:
        return True

    def explain(self, _day: date) -> str:
        return "测试日历"


def _tasks(monkeypatch, tmp_path, **overrides) -> dict[str, Any]:
    """构造 daemon 的任务表，但不真的跑 daemon。"""
    captured: dict[str, Any] = {}

    def fake_run_daemon(tasks, **kwargs):
        captured["tasks"] = {task.key: task for task in tasks}
        captured["daemon_kwargs"] = kwargs
        return type(
            "R", (), {"ran": [], "failed": [], "skipped": [], "notes": []}
        )()

    monkeypatch.setattr(daily_cycle, "run_daemon", fake_run_daemon)
    run_daily_cycle(
        adapter_factory=overrides.pop("adapter_factory", lambda: object()),
        output_dir=tmp_path,
        trade_date=TRADE_DATE,
        calendar=_Calendar(),
        **overrides,
    )
    return captured


def _bind_recorder(real: Any, sink: dict, key: str):
    """替身：按**真实签名**绑定实参，不合法就抛 TypeError。

    这正是这组用例要抓的东西——签名漂移。只记参数不校验的话，daemon 传一个已经
    被改名的关键字，替身照收不误，测试全绿而真实链路会炸。
    """
    signature = inspect.signature(real)

    def recorder(*args, **kwargs):
        signature.bind(*args, **kwargs)  # 不合法立即 TypeError
        sink[key] = kwargs
        return {"ok": True}

    return recorder


@pytest.mark.parametrize("task_key", sorted(TASK_TARGET))
def test_every_daemon_task_calls_its_chain_with_a_valid_signature(
    task_key, monkeypatch, tmp_path
) -> None:
    """闭包调起来，且实参在真实签名下合法。

    `preopen` 与 `postclose` 依赖自选股/候选池，空的时候它们**故意**直接返回
    `{"skipped": ...}`，那也是合法结局——但不能是抛异常。
    """
    sink: dict[str, Any] = {}
    for key, target in TASK_TARGET.items():
        real = getattr(daily_cycle, target)
        monkeypatch.setattr(daily_cycle, target, _bind_recorder(real, sink, key))
    # 状态目录的隔离由 conftest 的 autouse fixture 统一做（`ASHARE_DS_HOME`）。
    # 这里原来另设了一个 `ASHARE_DS_STATE_DIR`——那个变量不存在，读它的代码一行
    # 也没有，于是这三行看着像在做隔离，实际什么都没做；真正兜住的一直是 conftest。

    captured = _tasks(monkeypatch, tmp_path)
    result = captured["tasks"][task_key].run()

    assert isinstance(result, dict)
    if "skipped" not in result:
        assert sink[task_key], f"{task_key} 没有调起 {TASK_TARGET[task_key]}"


def test_every_task_passes_the_same_trade_date_it_was_built_for(monkeypatch, tmp_path) -> None:
    """全天所有任务必须锚在**同一个** `trade_date`。

    不一致的后果不是报错：盘前用 8 号选出的候选池，盘后按 7 号去核对结果，台账
    会记下一批对不上的验证，而两边各自看起来都正常。
    """
    sink: dict[str, Any] = {}
    for key, target in TASK_TARGET.items():
        monkeypatch.setattr(
            daily_cycle, target, _bind_recorder(getattr(daily_cycle, target), sink, key)
        )

    captured = _tasks(monkeypatch, tmp_path)
    for key in TASK_TARGET:
        captured["tasks"][key].run()

    for key, kwargs in sink.items():
        assert kwargs.get("trade_date") == TRADE_DATE, f"{key} 用了别的交易日"


def test_adapters_are_built_at_run_time_not_at_schedule_time(monkeypatch, tmp_path) -> None:
    """构造延迟到执行时刻——模块 docstring 承诺的那条。

    08:00 的网络故障不该让 08:20 的任务在**构造阶段**就崩掉。所以排期时一次
    `adapter_factory` 都不能调。
    """
    built = []
    sink: dict[str, Any] = {}
    for key, target in TASK_TARGET.items():
        monkeypatch.setattr(
            daily_cycle, target, _bind_recorder(getattr(daily_cycle, target), sink, key)
        )

    def factory():
        built.append(1)
        return object()

    captured = _tasks(monkeypatch, tmp_path, adapter_factory=factory)
    assert built == [], "排期阶段不该构造行情适配器"

    captured["tasks"]["morning"].run()
    assert built == [1]


def test_dry_run_reports_the_timetable_without_touching_the_network(tmp_path) -> None:
    report = run_daily_cycle(
        adapter_factory=lambda: pytest.fail("dry-run 不该构造适配器"),
        output_dir=tmp_path,
        trade_date=TRADE_DATE,
        dry_run=True,
        calendar=_Calendar(),
    )

    assert report["dry_run"] is True
    assert report["trade_date"] == str(TRADE_DATE)
    assert set(report["tasks"]) == {"news", *TASK_TARGET}


def test_only_narrows_the_timetable(tmp_path) -> None:
    """`--only` 选出来的任务之外，其余一个都不该排进去。"""
    report = run_daily_cycle(
        adapter_factory=lambda: object(),
        output_dir=tmp_path,
        trade_date=TRADE_DATE,
        dry_run=True,
        only=["morning", "review"],
        calendar=_Calendar(),
    )

    assert set(report["tasks"]) == {"morning", "review"}


def test_monitor_is_the_only_blocking_task(monkeypatch, tmp_path) -> None:
    """盘中监控要一直跑到收盘，其余任务跑完就走。

    标错的后果是整张时间表卡在某个本该几分钟跑完的任务上——后面的全部不执行，
    而 daemon 看起来「还在运行」。
    """
    sink: dict[str, Any] = {}
    for key, target in TASK_TARGET.items():
        monkeypatch.setattr(
            daily_cycle, target, _bind_recorder(getattr(daily_cycle, target), sink, key)
        )
    captured = _tasks(monkeypatch, tmp_path)

    blocking = {key for key, task in captured["tasks"].items() if task.blocking}
    assert blocking == {"monitor"}
