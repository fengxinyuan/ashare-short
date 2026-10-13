"""多数据源自动降级与熔断机制（Fallback & Circuit Breaker）。

当主数据源（如付费/自建 TickFlow 接口）发生限频、网络波动或鉴权失效时，
自动在毫秒级无缝降级到备用源（如新浪/腾讯/东财/AkShare），并记录完整降级原因与冷却审计。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


class AllSourcesFailedError(RuntimeError):
    """降级链中所有候选源均调用失败。"""

    def __init__(self, notes: list[str]) -> None:
        super().__init__("；".join(notes))
        self.notes = notes


@dataclass
class SourceNode:
    """单个数据源的运行健康度与熔断状态。"""

    name: str
    instance: Any
    failures: int = 0
    last_failure: float = 0.0
    state: str = "CLOSED"  # "CLOSED" (正常), "OPEN" (熔断), "HALF_OPEN" (试探)

    def is_available(self, now: float, cooldown: float) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if now - self.last_failure >= cooldown:
                self.state = "HALF_OPEN"
                return True
            return False
        return True  # HALF_OPEN 允许单次试探

    def record_success(self) -> None:
        self.failures = 0
        self.state = "CLOSED"

    def record_failure(self, now: float, max_failures: int) -> None:
        self.failures += 1
        self.last_failure = now
        if self.failures >= max_failures:
            self.state = "OPEN"


class FallbackChain:
    """按优先级调度的数据源降级执行链。"""

    def __init__(
        self,
        sources: list[tuple[str, Any]],
        max_failures: int = 3,
        cooldown_seconds: float = 60.0,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.nodes = [SourceNode(name=name, instance=inst) for name, inst in sources]
        self.max_failures = max_failures
        self.cooldown_seconds = cooldown_seconds
        self.now_fn = now_fn or time.monotonic

    def execute(
        self,
        operation_name: str,
        caller: Callable[[Any], T],
    ) -> tuple[T, str, list[str]]:
        """按优先级依次尝试各源；主源失败自动降级至备源，并返回审计日志。"""
        now = self.now_fn()
        notes: list[str] = []

        for node in self.nodes:
            if not node.is_available(now, self.cooldown_seconds):
                remaining = int(self.cooldown_seconds - (now - node.last_failure))
                notes.append(f"数据源 [{node.name}] 处于熔断冷却中（剩余 {remaining}s），跳过")
                continue

            try:
                result = caller(node.instance)
                node.record_success()
                return result, node.name, notes
            except Exception as exc:
                node.record_failure(now, self.max_failures)
                state_str = (
                    f"已进入熔断冷却({self.cooldown_seconds}s)"
                    if node.state == "OPEN"
                    else f"连续失败 {node.failures} 次"
                )
                notes.append(
                    f"数据源 [{node.name}] 执行 {operation_name} 失败（{type(exc).__name__}: {exc}），{state_str}"
                )

        raise AllSourcesFailedError(notes)


@dataclass
class ResilientMarketAdapter:
    """具备自动容灾和降级能力的高可用行情适配器门面。"""

    chain: FallbackChain
    notes: list[str] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        primary_adapter: tuple[str, Any],
        fallback_adapters: list[tuple[str, Any]],
        max_failures: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> ResilientMarketAdapter:
        sources = [primary_adapter, *fallback_adapters]
        chain = FallbackChain(
            sources=sources,
            max_failures=max_failures,
            cooldown_seconds=cooldown_seconds,
        )
        return cls(chain=chain)

    def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """透明调用底层适配器的方法，主源异常时自动故障转移。"""

        def _invoke(instance: Any) -> Any:
            method = getattr(instance, method_name, None)
            if not callable(method):
                raise AttributeError(f"{type(instance).__name__} 不支持方法 {method_name}")
            return method(*args, **kwargs)

        result, _used_source, audit_notes = self.chain.execute(method_name, _invoke)
        self.notes.extend(audit_notes)
        return result
