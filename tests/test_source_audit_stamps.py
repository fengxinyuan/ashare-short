"""离线验证数据源审计的时间口径、取数对象、参数和报告输出。"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import signal
import sys
import time
from datetime import UTC, date, datetime
from datetime import time as dtime
from types import SimpleNamespace

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "source_audit.py"


def _module():
    """按路径加载独立脚本；先注册模块，供 dataclass 解析注解。"""
    spec = importlib.util.spec_from_file_location("source_audit", _PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = _module()


@pytest.mark.parametrize(
    ("label", "clock", "now", "expected"),
    [
        # 周一盘前：15:13 今天还没到，它属于上一个交易日（上周五）。
        ("周一盘前", dtime(15, 13, 19), datetime(2026, 8, 10, 8, 22), "2026-08-07"),
        # 盘中：10:23 已经过了，就是今天的。
        ("周一盘中", dtime(10, 23, 0), datetime(2026, 8, 10, 10, 25), "2026-08-10"),
        # 盘后：15:13 已经过了，仍是今天的。
        ("周一盘后", dtime(15, 13, 19), datetime(2026, 8, 10, 16, 0), "2026-08-10"),
        # 周末不是交易日，无论几点都算上周五。
        ("周六晚间", dtime(15, 13, 19), datetime(2026, 8, 8, 20, 0), "2026-08-07"),
        ("周日午间", dtime(15, 13, 19), datetime(2026, 8, 9, 12, 0), "2026-08-07"),
    ],
)
def test_bare_times_land_on_the_session_they_belong_to(label, clock, now, expected) -> None:
    assert audit.anchor_bare_time(clock, now).date().isoformat() == expected, label


def test_a_bare_time_is_never_stamped_in_the_future() -> None:
    """这是上面五条的共同判据，也是原来那个 bug 的判据。

    未来的时间戳不只是「不准」——lag 会变成负数，于是一个三天没更新的源在
    「时效」这一列上排第一。
    """
    now = datetime(2026, 8, 10, 8, 22)
    for hour in range(24):
        stamped = audit.anchor_bare_time(dtime(hour, 30), now)
        assert stamped <= now, f"{hour}:30 被标到了未来：{stamped}"


def test_tickflow_quotes_probe_uses_keyword_only_symbols() -> None:
    class Quotes:
        def get(self, *, symbols):
            assert symbols == "600519.SH"
            return [{"timestamp": "2026-08-26 15:00:00"}]

    class Client:
        quotes = Quotes()

    probes = audit.build_probes(None, Client(), ["600519.SH"])
    probe = next(item for item in probes if item.key == "tf_quotes")

    result = audit.run_once(probe)

    assert result.ok is True
    assert result.rows == 1


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="requires signal.SIGALRM")
@pytest.mark.parametrize("timeout", [False, True])
def test_probe_restores_the_previous_alarm_handler_after_success_or_timeout(timeout) -> None:
    def previous_handler(signum, frame):
        pytest.fail("probe used the caller's alarm handler")

    def fetch():
        if timeout:
            signal.raise_signal(signal.SIGALRM)
        return audit.Result(ok=True, seconds=0)

    original = signal.signal(signal.SIGALRM, previous_handler)
    try:
        probe = audit.Probe("sample", "fixture", "sample", fetch, "test")
        result = audit.run_once(probe)
        assert result.ok is not timeout
        if timeout:
            assert "ProbeTimeout" in result.error
        assert signal.getsignal(signal.SIGALRM) is previous_handler
        assert signal.alarm(0) == 0
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, original)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires host timezone switching")
def test_tickflow_batch_uses_unique_symbols_and_beijing_day_bounds_on_a_utc_host(
    monkeypatch,
) -> None:
    calls = []

    def fetch(symbols, **kwargs):
        calls.append((symbols, kwargs))
        return {symbol: {"timestamp": ["2026-09-11"]} for symbol in symbols}

    monkeypatch.setattr(audit, "beijing_today", lambda: date(2026, 9, 11))
    client = SimpleNamespace(klines=SimpleNamespace(batch=fetch))
    probes = audit.build_probes(None, client, ["sz000001", "000001.SZ", "600519"])
    probe = next(item for item in probes if item.key == "tf_klines_batch")
    try:
        with monkeypatch.context() as host:
            host.setenv("TZ", "UTC")
            time.tzset()
            result = audit.run_once(probe)
    finally:
        time.tzset()

    assert result.ok
    assert result.rows == 2
    symbols, kwargs = calls[0]
    assert symbols == ["000001.SZ", "600519.SH"]
    assert datetime.fromtimestamp(kwargs["start_time"] / 1000, UTC) == datetime(
        2026, 8, 1, 16, tzinfo=UTC
    )
    assert datetime.fromtimestamp(kwargs["end_time"] / 1000, UTC) == datetime(
        2026, 9, 11, 15, 59, 59, 999000, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "value",
    [
        date(2026, 9, 11),
        20260911,
        "2026/09/11",
        "2026-09-10T16:00:00Z",
        datetime(2026, 9, 10, 16, tzinfo=UTC),
        datetime(2026, 9, 10, 16, tzinfo=UTC).timestamp() * 1000,
    ],
)
def test_complete_timestamps_share_the_market_data_timezone(value) -> None:
    assert audit.parse_ts(value) == datetime(2026, 9, 11)


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), 10**400, "bad"])
def test_invalid_timestamps_are_missing_instead_of_breaking_the_report(value) -> None:
    assert audit.parse_ts(value) is None


def test_yearless_date_uses_the_previous_year_at_new_year(monkeypatch) -> None:
    monkeypatch.setattr(audit, "beijing_naive_now", lambda: datetime(2026, 1, 2, 8))
    assert audit.parse_ts("12-31 15:00") == datetime(2025, 12, 31, 15)


def test_mixed_timezones_produce_a_usable_lag_report(monkeypatch) -> None:
    monkeypatch.setattr(audit, "beijing_naive_now", lambda: datetime(2026, 9, 11, 15, 2))
    latest = audit.newest(
        [
            {"time": "2026-09-11 15:00:00"},
            {"time": "2026-09-11T07:01:00+00:00"},
            {"time": float("inf"), "date": "2026-09-10"},
        ],
        "time",
        "date",
    )
    probe = audit.Probe(
        "sample", "fixture", "sample", lambda: audit.Result(True, 0, latest=latest), "test"
    )

    result = audit.audit([probe], 1)["sources"][0]

    assert result["latest_data"] == "2026-09-11T15:01:00"
    assert result["lag_minutes_vs_now"] == 1.0


@pytest.mark.parametrize(
    "key, endpoint, expected",
    [
        ("sina_min_1", "stock_zh_a_minute", {"symbol": "sz000001", "period": "1", "adjust": ""}),
        ("sina_daily", "stock_zh_a_daily", {"symbol": "sz000001", "adjust": ""}),
        ("tx_tick", "stock_zh_a_tick_tx_js", {"symbol": "sz000001"}),
        ("em_flow_indiv", "stock_individual_fund_flow", {"stock": "000001", "market": "sz"}),
    ],
)
def test_single_stock_probes_follow_the_requested_symbol(key, endpoint, expected) -> None:
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return [{"date": "2026-09-11"}]

    module = SimpleNamespace(**{endpoint: fetch})
    probe = next(item for item in audit.build_probes(module, None, ["000001"]) if item.key == key)
    assert audit.run_once(probe).ok
    assert calls == [expected]


def test_failed_table_conversion_is_visible_and_other_probes_continue() -> None:
    class BrokenFrame:
        def to_dict(self, **kwargs):
            raise TypeError("schema changed")

    module = SimpleNamespace(
        stock_zh_a_spot=lambda: BrokenFrame(),
        stock_zh_a_daily=lambda **kwargs: [{"date": "2026-09-11"}],
    )
    probes = [
        probe
        for probe in audit.build_probes(module, None, ["600519.SH"])
        if probe.key in {"sina_spot_all", "sina_daily"}
    ]
    report = audit.audit(probes, 1)

    failed, healthy = report["sources"]
    assert failed["successes"] == 0
    assert failed["errors"] == ["TypeError: schema changed"]
    assert healthy["successes"] == 1


def test_selecting_akshare_does_not_initialize_tickflow_and_writes_nested_report(
    tmp_path, monkeypatch
) -> None:
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return [{"date": "2026-09-11"}]

    def unexpected_client():
        pytest.fail("unselected TickFlow client was initialized")

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_zh_a_daily=fetch))
    monkeypatch.setitem(
        sys.modules, "tickflow", SimpleNamespace(TickFlow=SimpleNamespace(free=unexpected_client))
    )
    target = tmp_path / "nested" / "audit.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source_audit.py",
            "--only",
            "sina_daily",
            "--symbols",
            "sz000001",
            "--repeats",
            "2",
            "--json",
            str(target),
        ],
    )

    audit.main()

    report = json.loads(target.read_text(encoding="utf-8"))
    assert [entry["key"] for entry in report["sources"]] == ["sina_daily"]
    assert report["sources"][0]["attempts"] == 2
    assert calls == [{"symbol": "sz000001", "adjust": ""}] * 2


@pytest.mark.parametrize(
    "args", [["--symbols"], ["--only"], ["--repeats", "0"], ["--only", "unknown"]]
)
def test_invalid_arguments_do_not_replace_an_existing_report(tmp_path, monkeypatch, args) -> None:
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace())
    target = tmp_path / "audit.json"
    target.write_text("previous report", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["source_audit.py", "--json", str(target), *args])

    with pytest.raises(SystemExit) as error:
        audit.main()

    assert error.value.code == 2
    assert target.read_text(encoding="utf-8") == "previous report"


@pytest.mark.parametrize("selection", [["--only", "tf_quotes"], ["--only", "sina_daily"]])
def test_missing_dependencies_or_failed_initialization_cannot_produce_an_empty_success(
    tmp_path, monkeypatch, selection
) -> None:
    def connect():
        raise ConnectionError("fixture: connection unavailable")

    monkeypatch.setitem(sys.modules, "akshare", None)
    monkeypatch.setitem(
        sys.modules,
        "tickflow",
        SimpleNamespace(
            TickFlow=SimpleNamespace(free=lambda: SimpleNamespace(connect=connect)),
        ),
    )
    target = tmp_path / "audit.json"
    target.write_text("previous report", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["source_audit.py", "--json", str(target), *selection])

    with pytest.raises(SystemExit) as error:
        audit.main()

    assert error.value.code == 2
    assert target.read_text(encoding="utf-8") == "previous report"


def test_classification_probe_remains_available_without_market_sdks(tmp_path, monkeypatch):
    from ashare_decision_support.adapters.ths_taxonomy import (
        SymbolProfile,
        ThsTaxonomySnapshot,
        ThsTaxonomySource,
    )

    monkeypatch.setitem(sys.modules, "akshare", None)
    monkeypatch.setitem(sys.modules, "tickflow", None)
    snapshot = ThsTaxonomySnapshot(
        profiles={
            f"6000{i:02d}.SH": SymbolProfile(
                f"6000{i:02d}.SH", f"测试{i}", industry="半导体", concepts=("先进封装",)
            )
            for i in range(20)
        },
        ok=True,
    )
    monkeypatch.setattr(ThsTaxonomySource, "load", lambda self, **kwargs: snapshot)
    target = tmp_path / "audit.json"
    monkeypatch.setattr(sys, "argv", ["source_audit.py", "--repeats", "1", "--json", str(target)])

    audit.main()

    report = json.loads(target.read_text(encoding="utf-8"))
    assert [entry["key"] for entry in report["sources"]] == ["ths_taxonomy"]
    assert report["sources"][0]["successes"] == 1
