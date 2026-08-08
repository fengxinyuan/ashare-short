"""免费档速率闸门与分类完整性。

这两件事是同一个故障的两面：TickFlow 免费档 60 次/分钟撞墙后，
`_universe_get` 会静默吞掉异常返回空成分股，于是残缺的分类被写进缓存，
之后每一次盘前扫描都在用它。2026-08-01 实测到的现场是 3961/4430 只——
少 469 只，而链路一声不响地跑完了。
"""

from __future__ import annotations

import json

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
from ashare_decision_support.sectors import SectorTaxonomyError, TickFlowSwTaxonomy
from ashare_decision_support.sectors import taxonomy as taxonomy_module

# ---- clock double --------------------------------------------------------


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


# ---- limiter -------------------------------------------------------------


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


# ---- error recognition ---------------------------------------------------


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


# ---- limit resolution ----------------------------------------------------


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


# ---- client proxy --------------------------------------------------------


class FakeUniverses:
    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    def get(self, universe_id):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise FakeRateLimitError(LIVE_MESSAGE)
        return {"id": universe_id, "symbols": ["600000.SH"]}

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
    assert wrapped.unwrapped is sdk


# ---- taxonomy integrity --------------------------------------------------


class ShardyUniverses:
    """第 2 个分片取数失败，模拟限流被吞掉的现场。"""

    def __init__(self, failing_id: str | None = None) -> None:
        self.failing_id = failing_id
        self._detail = {
            "CN_Equity_SW1_1": ["600000.SH", "600001.SH"],
            "CN_Equity_SW1_2": ["600002.SH", "600003.SH"],
        }

    def list(self):
        return [
            {"id": "CN_Equity_SW1_1", "name": "SW1电子"},
            {"id": "CN_Equity_SW1_2", "name": "SW1电子"},
        ]

    def get(self, universe_id):
        if universe_id == self.failing_id:
            raise FakeRateLimitError(LIVE_MESSAGE)
        return {"id": universe_id, "symbols": self._detail[universe_id]}


class ShardyClient:
    def __init__(self, failing_id: str | None = None) -> None:
        self.universes = ShardyUniverses(failing_id)


def test_complete_taxonomy_still_builds_and_caches(tmp_path) -> None:
    taxonomy = TickFlowSwTaxonomy(ShardyClient(), cache_dir=tmp_path, min_constituents=1)
    sectors = taxonomy.list_sectors()

    assert len(sectors[0].symbols) == 4
    assert taxonomy.last_shard_failures == 0
    assert (tmp_path / "sector_taxonomy_sw1.json").exists()


def test_a_failed_shard_raises_instead_of_returning_a_short_sector(tmp_path) -> None:
    taxonomy = TickFlowSwTaxonomy(
        ShardyClient("CN_Equity_SW1_2"), cache_dir=tmp_path, min_constituents=1
    )

    with pytest.raises(SectorTaxonomyError, match="1/2"):
        taxonomy.list_sectors()


def test_a_partial_taxonomy_is_never_written_to_disk(tmp_path) -> None:
    """缓存是永久的：一次限流不能污染之后每一次盘前扫描。"""
    taxonomy = TickFlowSwTaxonomy(
        ShardyClient("CN_Equity_SW1_2"), cache_dir=tmp_path, min_constituents=1
    )

    with pytest.raises(SectorTaxonomyError):
        taxonomy.list_sectors()

    assert not (tmp_path / "sector_taxonomy_sw1.json").exists()


def test_empty_shard_is_not_mistaken_for_a_failed_one(tmp_path) -> None:
    """成分股为空是合法读数，不该触发完整性告警。"""

    class EmptyUniverses(ShardyUniverses):
        def get(self, universe_id):
            return {"id": universe_id, "symbols": []}

    client = ShardyClient()
    client.universes = EmptyUniverses()
    taxonomy = TickFlowSwTaxonomy(client, cache_dir=tmp_path, min_constituents=0)
    sectors = taxonomy.list_sectors()

    assert [item.symbols for item in sectors] == [()]
    assert taxonomy.last_shard_failures == 0


def test_a_cache_without_the_completeness_stamp_is_rebuilt(tmp_path) -> None:
    """修复前写下的缓存可能缺片，且外观上与好文件毫无区别。

    2026-08-01 现场那份 SW1 缓存少了 469/4430 只成分股，链路照跑不误。
    没有戳就必须重建——继续用一份不知道完不完整的分类，比多花一次重建的时间贵。
    """
    stale = tmp_path / "sector_taxonomy_sw1.json"
    stale.write_text(
        json.dumps({"level": "SW1", "sectors": [{"name": "电子", "symbols": ["600000.SH"]}]}),
        encoding="utf-8",
    )

    taxonomy = TickFlowSwTaxonomy(ShardyClient(), cache_dir=tmp_path, min_constituents=1)
    sectors = taxonomy.list_sectors()

    assert len(sectors[0].symbols) == 4          # 重建过，不是缓存里那 1 只
    assert json.loads(stale.read_text())["schema"] == "sw-complete-v1"


def test_a_stamped_cache_is_reused_without_refetching(tmp_path) -> None:
    client = ShardyClient()
    TickFlowSwTaxonomy(client, cache_dir=tmp_path, min_constituents=1).list_sectors()

    class ExplodingUniverses(ShardyUniverses):
        def get(self, universe_id):
            raise AssertionError("带戳的缓存不该再打网络")

    second_client = ShardyClient()
    second_client.universes = ExplodingUniverses()
    sectors = TickFlowSwTaxonomy(
        second_client, cache_dir=tmp_path, min_constituents=1
    ).list_sectors()

    assert len(sectors[0].symbols) == 4


# ---- 批量分片取数 --------------------------------------------------------
#
# 逐个 `universes.get()` 要打 335 次，免费档 60 次/分钟下被节流成 6 分钟冷启动。
# 付费档的 `universes.batch(ids)` 实测 200 个 / 0.27 秒，335 片折成 2 次调用。
# 语义必须与逐个取完全一致——包括「取数失败」和「成分股为空」的区分。


class BatchUniverses(ShardyUniverses):
    """带批量接口的 client。`missing` 里的 id 在批量结果中缺席（= 取数失败）。"""

    def __init__(self, missing: tuple[str, ...] = (), chunk_boom: bool = False) -> None:
        super().__init__()
        self.missing = set(missing)
        self.chunk_boom = chunk_boom
        self.batch_calls: list[list[str]] = []
        self.get_calls: list[str] = []

    def batch(self, ids):
        self.batch_calls.append(list(ids))
        if self.chunk_boom:
            raise FakeRateLimitError(LIVE_MESSAGE)
        return {
            uid: {"id": uid, "symbols": self._detail[uid]}
            for uid in ids
            if uid not in self.missing
        }

    def get(self, universe_id):
        self.get_calls.append(universe_id)
        return super().get(universe_id)


def _client_with(universes) -> ShardyClient:
    client = ShardyClient()
    client.universes = universes
    return client


def test_batch_is_preferred_over_fetching_shards_one_by_one(tmp_path) -> None:
    universes = BatchUniverses()
    taxonomy = TickFlowSwTaxonomy(_client_with(universes), cache_dir=tmp_path, min_constituents=1)

    sectors = taxonomy.list_sectors()

    assert len(sectors[0].symbols) == 4
    assert universes.batch_calls == [["CN_Equity_SW1_1", "CN_Equity_SW1_2"]]
    assert universes.get_calls == []          # 一次逐个取都没发生


def test_batch_and_one_by_one_produce_the_same_taxonomy(tmp_path) -> None:
    """加速不许改变结果——这是这次改动唯一的验收条件。"""
    batched = TickFlowSwTaxonomy(
        _client_with(BatchUniverses()), cache_dir=tmp_path / "a", min_constituents=1
    ).list_sectors()
    one_by_one = TickFlowSwTaxonomy(
        ShardyClient(), cache_dir=tmp_path / "b", min_constituents=1
    ).list_sectors()

    assert [(s.name, s.symbols) for s in batched] == [(s.name, s.symbols) for s in one_by_one]


def test_a_shard_missing_from_the_batch_response_counts_as_a_failure(tmp_path) -> None:
    """批量接口不会为缺失的 id 报错，它只是不返回——静默少一片正是要防的那件事。"""
    taxonomy = TickFlowSwTaxonomy(
        _client_with(BatchUniverses(missing=("CN_Equity_SW1_2",))),
        cache_dir=tmp_path,
        min_constituents=1,
    )

    with pytest.raises(SectorTaxonomyError, match="1/2"):
        taxonomy.list_sectors()

    assert not (tmp_path / "sector_taxonomy_sw1.json").exists()


def test_a_client_without_batch_still_works(tmp_path) -> None:
    """免费档或老 SDK 没有批量接口，退回逐个取，行为不变。"""
    taxonomy = TickFlowSwTaxonomy(ShardyClient(), cache_dir=tmp_path, min_constituents=1)

    assert len(taxonomy.list_sectors()[0].symbols) == 4


def test_a_broken_batch_endpoint_falls_back_to_one_by_one(tmp_path) -> None:
    """批量接口存在但调不通时，不能把整份分类判死——逐个取仍然能出结果。"""
    universes = BatchUniverses(chunk_boom=True)
    taxonomy = TickFlowSwTaxonomy(_client_with(universes), cache_dir=tmp_path, min_constituents=1)

    sectors = taxonomy.list_sectors()

    assert len(sectors[0].symbols) == 4
    assert universes.get_calls == ["CN_Equity_SW1_1", "CN_Equity_SW1_2"]


def test_batch_is_chunked_so_one_request_cannot_grow_without_bound(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(taxonomy_module, "BATCH_SIZE", 1)
    universes = BatchUniverses()
    TickFlowSwTaxonomy(_client_with(universes), cache_dir=tmp_path, min_constituents=1).list_sectors()

    assert universes.batch_calls == [["CN_Equity_SW1_1"], ["CN_Equity_SW1_2"]]


def test_batch_shards_are_still_throttled_on_the_free_tier() -> None:
    """闸门包在 client 上，批量接口是后加的——必须自动被覆盖，不能漏网。"""
    limiter, clock = make_limiter(limit=1, window=60.0)
    client = RateLimitedClient(_client_with(BatchUniverses()), limiter)

    client.universes.batch(["CN_Equity_SW1_1"])
    client.universes.batch(["CN_Equity_SW1_2"])

    assert clock.slept == [60.0]              # 第二次批量调用等过闸门


class JsonResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


def test_static_taxonomy_fixture_fetches_and_merges_concepts(tmp_path) -> None:
    files = {
        "industries.json": [{
            "symbol": "600000",
            "name": "测试银行",
            "industries": ["银行", "银行Ⅱ"],
        }],
        "concepts.json": [{"symbol": "600000.SH", "concepts": ["金融科技", "融资融券"]}],
    }

    def opener(request, timeout):
        return JsonResponse(files[request.full_url.rsplit("/", 1)[-1]])

    snapshot = ThsTaxonomySource(opener=opener, cache_dir=tmp_path).load(
        force_refresh=True
    )
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
