"""多数据源自动降级链单元测试。"""

from __future__ import annotations

import pytest

from ashare_decision_support.adapters.fallback_chain import (
    AllSourcesFailedError,
    FallbackChain,
    ResilientMarketAdapter,
)


class MockSource:
    def __init__(self, name: str, fail_times: int = 0, return_val: str = "ok") -> None:
        self.name = name
        self.fail_times = fail_times
        self.calls = 0
        self.return_val = return_val

    def query(self, symbol: str) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError(f"{self.name} network down")
        return f"{self.name}:{self.return_val}:{symbol}"


def test_primary_source_success() -> None:
    src1 = MockSource("primary", fail_times=0)
    src2 = MockSource("secondary", fail_times=0)
    chain = FallbackChain([("primary", src1), ("secondary", src2)])

    res, used, notes = chain.execute("query", lambda s: s.query("600519"))
    assert res == "primary:ok:600519"
    assert used == "primary"
    assert src1.calls == 1
    assert src2.calls == 0
    assert len(notes) == 0


def test_fallback_to_secondary_on_primary_failure() -> None:
    src1 = MockSource("primary", fail_times=5)
    src2 = MockSource("secondary", fail_times=0, return_val="fallback_data")
    chain = FallbackChain([("primary", src1), ("secondary", src2)], max_failures=2)

    res, used, notes = chain.execute("query", lambda s: s.query("600519"))
    assert res == "secondary:fallback_data:600519"
    assert used == "secondary"
    assert src1.calls == 1
    assert src2.calls == 1
    assert any("primary" in n and "失败" in n for n in notes)


def test_circuit_breaker_trips_and_cools_down() -> None:
    simulated_time = 1000.0
    src1 = MockSource("primary", fail_times=10)
    src2 = MockSource("secondary", fail_times=0)
    chain = FallbackChain(
        [("primary", src1), ("secondary", src2)],
        max_failures=2,
        cooldown_seconds=30.0,
        now_fn=lambda: simulated_time,
    )

    # 第一次失败
    chain.execute("query", lambda s: s.query("A"))
    assert chain.nodes[0].state == "CLOSED"
    assert chain.nodes[0].failures == 1

    # 第二次失败 -> 触发熔断 OPEN
    chain.execute("query", lambda s: s.query("B"))
    assert chain.nodes[0].state == "OPEN"
    assert chain.nodes[0].failures == 2

    # 第三次调用处于冷却期 -> 直接跳过 primary
    simulated_time += 10.0
    _res, used, notes = chain.execute("query", lambda s: s.query("C"))
    assert used == "secondary"
    assert src1.calls == 2  # 没有增加调用
    assert any("熔断冷却中" in n for n in notes)

    # 经过 35 秒后冷却结束 -> HALF_OPEN 允许试探
    simulated_time += 25.0
    chain.execute("query", lambda s: s.query("D"))
    assert src1.calls == 3


def test_all_sources_fail_raises_exception() -> None:
    src1 = MockSource("s1", fail_times=10)
    src2 = MockSource("s2", fail_times=10)
    chain = FallbackChain([("s1", src1), ("s2", src2)])

    with pytest.raises(AllSourcesFailedError) as exc_info:
        chain.execute("query", lambda s: s.query("X"))
    assert len(exc_info.value.notes) == 2


def test_resilient_market_adapter_facade() -> None:
    src1 = MockSource("p", fail_times=1)
    src2 = MockSource("s", fail_times=0, return_val="resilient")
    adapter = ResilientMarketAdapter.create(
        primary_adapter=("primary", src1),
        fallback_adapters=[("backup", src2)],
    )

    result = adapter.call("query", "000001")
    assert result == "s:resilient:000001"
    assert len(adapter.notes) >= 1
