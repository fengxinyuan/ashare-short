"""TickFlow 速率闸门与同花顺静态分类缓存。"""

from __future__ import annotations

import pytest

from ashare_decision_support.adapters.ratelimit import (
    DEFAULT_FREE_TIER_LIMIT,
    RateLimitedClient,
    RateLimiter,
    is_rate_limit_error,
    resolve_limit,
    retry_after_seconds,
    wrap_client,
)
from ashare_decision_support.adapters.ths_taxonomy import (
    SymbolProfile,
    ThsTaxonomySnapshot,
    ThsTaxonomySource,
)
from tests.fakes import json_response

# clock double


class FakeClock:
    """单调钟 + sleep，sleep 直接推进时间，测试里不真的等。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_limiter(limit: int = 3, window: float = 60.0) -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock()
    return (
        RateLimiter(limit, window=window, monotonic=clock.monotonic, sleep=clock.sleep),
        clock,
    )


# limiter


def test_limiter_lets_the_first_window_through_without_waiting() -> None:
    limiter, clock = make_limiter(limit=3)
    for _ in range(3):
        limiter.acquire()
    assert clock.slept == []


def test_limiter_waits_exactly_until_the_oldest_call_leaves_the_window() -> None:
    limiter, clock = make_limiter(limit=3, window=60.0)
    for _ in range(3):
        limiter.acquire()
    clock.now = 20.0  # 三次调用都在 t=0，窗口还剩 40 秒

    limiter.acquire()

    assert clock.slept == [40.0]
    assert clock.now == 60.0


def test_limiter_does_not_wait_once_calls_have_aged_out() -> None:
    limiter, clock = make_limiter(limit=2, window=60.0)
    limiter.acquire()
    limiter.acquire()
    clock.now = 61.0

    limiter.acquire()

    assert clock.slept == []


def test_limiter_reports_what_it_cost() -> None:
    limiter, _ = make_limiter(limit=1, window=10.0)
    limiter.acquire()
    limiter.acquire()

    stats = limiter.stats()
    assert stats["limit"] == 1
    assert stats["waits"] == 1
    assert stats["waited_seconds"] == 10.0


# error recognition


class FakeRateLimitError(Exception):
    pass


FakeRateLimitError.__name__ = "RateLimitError"

LIVE_MESSAGE = (
    "请求频率超限 (60/min)，请 38266ms 后重试。"
    "免费服务有速率限制，如需更高频率请访问 https://tickflow.org 注册。"
)


def test_rate_limit_recognised_by_class_name() -> None:
    assert is_rate_limit_error(FakeRateLimitError("boom"))


def test_rate_limit_recognised_by_status_code() -> None:
    exc = RuntimeError("something")
    exc.status_code = 429
    assert is_rate_limit_error(exc)


def test_rate_limit_recognised_through_the_adapter_wrapper() -> None:
    """适配层把原异常包成 TickFlowAdapterError 后只剩文本，仍须认得出来。"""
    assert is_rate_limit_error(RuntimeError(f"RateLimitError: {LIVE_MESSAGE}"))


def test_plain_errors_are_not_treated_as_rate_limits() -> None:
    assert not is_rate_limit_error(RuntimeError("PermissionError: 免费档不提供该端点"))


def test_retry_delay_comes_from_the_server_not_a_local_guess() -> None:
    assert retry_after_seconds(RuntimeError(LIVE_MESSAGE)) == pytest.approx(38.766, abs=1e-3)


def test_retry_delay_falls_back_when_the_message_says_nothing() -> None:
    assert retry_after_seconds(RuntimeError("429"), default=7.0) == 7.0


# limit resolution


def test_paid_tier_is_not_throttled() -> None:
    assert resolve_limit("sk-live-key") is None


def test_free_tier_defaults_to_the_measured_ceiling(monkeypatch) -> None:
    monkeypatch.delenv("TICKFLOW_RATE_LIMIT", raising=False)
    assert resolve_limit(None) == DEFAULT_FREE_TIER_LIMIT


def test_env_can_override_and_disable(monkeypatch) -> None:
    monkeypatch.setenv("TICKFLOW_RATE_LIMIT", "20")
    assert resolve_limit(None) == 20
    monkeypatch.setenv("TICKFLOW_RATE_LIMIT", "0")
    assert resolve_limit(None) is None


def test_garbage_env_falls_back_instead_of_crashing(monkeypatch) -> None:
    monkeypatch.setenv("TICKFLOW_RATE_LIMIT", "fast")
    assert resolve_limit(None) == DEFAULT_FREE_TIER_LIMIT


# client proxy


class FakeUniverses:
    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    def get(self, universe_id):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise FakeRateLimitError(LIVE_MESSAGE)
        return {"id": universe_id, "symbols": ["600000.SH"]}

    def batch(self, ids):
        return {uid: self.get(uid) for uid in ids}

    def boom(self):
        raise ValueError("not a rate limit")


class FakeSdkClient:
    def __init__(self, fail_times: int = 0) -> None:
        self.universes = FakeUniverses(fail_times)
        self.mode = "free"

    def connect(self):
        return True


def test_proxy_throttles_calls_reached_through_a_namespace() -> None:
    limiter, clock = make_limiter(limit=2, window=60.0)
    client = RateLimitedClient(FakeSdkClient(), limiter)

    for _ in range(3):
        client.universes.get("CN_Equity_SW1_1")

    assert clock.slept == [60.0]


def test_proxy_retries_a_rate_limit_using_the_server_delay() -> None:
    limiter, clock = make_limiter(limit=10, window=60.0)
    sdk = FakeSdkClient(fail_times=1)
    client = RateLimitedClient(sdk, limiter)

    payload = client.universes.get("CN_Equity_SW1_1")

    assert payload["symbols"] == ["600000.SH"]
    assert sdk.universes.calls == 2
    assert clock.slept == [pytest.approx(38.766, abs=1e-3)]


def test_proxy_reraises_everything_that_is_not_a_rate_limit() -> None:
    limiter, _ = make_limiter(limit=10)
    client = RateLimitedClient(FakeSdkClient(), limiter)

    with pytest.raises(ValueError):
        client.universes.boom()


def test_proxy_passes_through_plain_attributes() -> None:
    limiter, _ = make_limiter(limit=10)
    client = RateLimitedClient(FakeSdkClient(), limiter)
    assert client.mode == "free"


def test_wrap_client_is_a_no_op_for_the_paid_tier() -> None:
    sdk = FakeSdkClient()
    assert wrap_client(sdk, api_key="sk-live") is sdk


def test_wrap_client_installs_the_gate_for_the_free_tier(monkeypatch) -> None:
    monkeypatch.delenv("TICKFLOW_RATE_LIMIT", raising=False)
    sdk = FakeSdkClient()
    wrapped = wrap_client(sdk, api_key=None)
    assert isinstance(wrapped, RateLimitedClient)


def test_batch_calls_are_still_throttled_on_the_free_tier() -> None:
    limiter, clock = make_limiter(limit=1, window=60.0)
    client = RateLimitedClient(FakeSdkClient(), limiter)

    client.universes.batch(["CN_Equity_SW1_1"])
    client.universes.batch(["CN_Equity_SW1_2"])

    assert clock.slept == [60.0]


def test_static_taxonomy_fixture_fetches_and_merges_concepts(tmp_path) -> None:
    files = {
        "industries.json": [
            {
                "symbol": "600000",
                "name": "测试银行",
                "industries": ["银行", "银行Ⅱ"],
            }
        ],
        "concepts.json": [{"symbol": "600000.SH", "concepts": ["金融科技", "融资融券"]}],
    }

    def opener(request, timeout):
        return json_response(files[request.full_url.rsplit("/", 1)[-1]])

    snapshot = ThsTaxonomySource(opener=opener, cache_dir=tmp_path).load(force_refresh=True)
    assert snapshot.ok is True
    assert snapshot.profiles["600000.SH"].concepts == ("金融科技", "融资融券")


def test_static_taxonomy_uses_stale_cache_when_refresh_fails(tmp_path, monkeypatch) -> None:
    source = ThsTaxonomySource(cache_dir=tmp_path, max_age_days=0)
    source._write_cache(
        ThsTaxonomySnapshot(
            profiles={"600000.SH": SymbolProfile("600000.SH", "测试银行")},
            fetched_on="2000-01-01",
        )
    )
    monkeypatch.setattr(source, "_fetch", lambda: (None, ["fixture failure"]))
    snapshot = source.load(force_refresh=True)
    assert snapshot.ok is True
    assert snapshot.from_cache is True
    assert any("继续使用" in note for note in snapshot.notes)
