"""舆情源的并发扇出层：保序、失败隔离、以及「真的并发了」。

这三条是 `fanout.py` 对五个调用方的全部承诺。分开测是因为它们的失效方式
完全不同：不保序表现为盘前跑两次拿到两批文档（不报错），失败不隔离表现为一路
挂掉带走整批（报错但归因错），没真并发表现为盘前慢了一分钟（什么都不报）。
"""

from __future__ import annotations

import threading
import time
from datetime import date

from ashare_decision_support.fanout import gather
from ashare_decision_support.news.base import NewsDoc, NewsSource
from ashare_decision_support.news.composite import CompositeNewsSource


def test_results_follow_input_order_not_completion_order() -> None:
    """先完成的排在后面——顺序必须由入参决定。

    这条守的是可复现性：下游 `retain_market_docs` 按来源发配额、同一来源内取
    列表前缀，拼接顺序一变，同一个盘前跑两次就会得到两批不同的文档。
    """
    delays = {"slow": 0.05, "medium": 0.02, "fast": 0.0}

    def fetch(name: str) -> str:
        time.sleep(delays[name])
        return name.upper()

    done, failed = gather(["slow", "medium", "fast"], fetch)

    assert [item for item, _ in done] == ["slow", "medium", "fast"]
    assert [value for _, value in done] == ["SLOW", "MEDIUM", "FAST"]
    assert failed == []


def test_one_dead_feed_costs_only_its_own_documents() -> None:
    """抛异常的那一路进 failed 并带着异常，其余照常返回。"""

    def fetch(name: str) -> list[str]:
        if name == "dead":
            raise ConnectionError("refused")
        return [name]

    done, failed = gather(["alive", "dead", "other"], fetch)

    assert [item for item, _ in done] == ["alive", "other"]
    assert [item for item, _ in failed] == ["dead"]
    # 异常本身要交还：note 里那个类名是排查时唯一的线索。
    assert isinstance(failed[0][1], ConnectionError)


def test_empty_result_is_success_not_failure() -> None:
    """「今天这个栏目没更新」和「这个栏目取不到」必须分得开。

    合并成一种的代价是 note 说谎：一个正常但当天没内容的官方栏目会被报成不可用，
    而真正挂掉的那天你就不会再多看一眼。
    """
    done, failed = gather(["empty"], lambda _name: [])

    assert done == [("empty", [])]
    assert failed == []


def test_feeds_actually_run_concurrently() -> None:
    """墙钟时间由最慢的一路决定，不是各路之和。

    断言并发度而不只是断言耗时：一个 `max_workers=1` 的回归会让耗时变成三倍，
    但在慢机器上单纯的时间阈值也会飘。这里两条一起断。
    """
    peak = 0
    live = 0
    lock = threading.Lock()

    def fetch(_name: str) -> str:
        nonlocal peak, live
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return "ok"

    started = time.monotonic()
    done, _failed = gather(["a", "b", "c", "d"], fetch)
    elapsed = time.monotonic() - started

    assert len(done) == 4
    assert peak >= 2
    assert elapsed < 4 * 0.05


class _Slow(NewsSource):
    """一路源：固定延迟，可选地直接抛异常。"""

    def __init__(self, name: str, delay: float, boom: bool = False) -> None:
        self.name = name
        self.delay = delay
        self.boom = boom

    def fetch_market(self, trade_date: date, limit: int = 200):
        time.sleep(self.delay)
        if self.boom:
            raise TimeoutError("upstream timeout")
        return [NewsDoc(title=f"{self.name} 快讯", source=self.name)], []

    def fetch_for_symbols(self, symbols: list[str], limit: int = 10):
        return {}, []


def test_composite_overlaps_its_sources_and_names_the_dead_one() -> None:
    """四路源之间也要重叠，且挂掉的那一路要连异常类型一起写进 note。"""
    sources = [_Slow("甲", 0.05), _Slow("乙", 0.05), _Slow("丙", 0.05, boom=True)]
    composite = CompositeNewsSource(*sources)

    started = time.monotonic()
    docs, notes = composite.fetch_market(date(2026, 8, 8), limit=50)
    elapsed = time.monotonic() - started

    assert elapsed < 3 * 0.05
    assert {doc.source for doc in docs} == {"甲", "乙"}
    assert any("丙" in note and "TimeoutError" in note for note in notes)
    # 少一路就是少一块覆盖面，简报里不能读起来像「都取到了」。
    assert any("2/3 路源" in note for note in notes)


# ---- health probes -------------------------------------------------------


def test_probe_reports_each_feed_separately() -> None:
    """整源一个布尔值不够：少哪一路决定的是简报少哪一块。"""
    from ashare_decision_support.news.health import probe_feeds

    def fetch(name: str) -> list[str]:
        if name == "政策":
            raise OSError("refused")
        return ["a", "b"] if name == "快讯" else []

    report = probe_feeds("demo", ["快讯", "政策", "空栏目"], lambda n: n, fetch)

    assert report["source"] == "demo"
    assert report["reachable"] is True
    assert report["unreachable_feeds"] == ["政策"]
    # 「可达但今天没内容」不是故障——空栏目仍算 reachable。
    assert report["reachable_feeds"] == ["快讯", "空栏目"]
    assert report["feeds"]["快讯"]["count"] == 2
    assert report["feeds"]["空栏目"]["count"] == 0
    assert "OSError" in report["feeds"]["政策"]["error"]
    assert report["feed_count"] == 3


def test_probe_calls_every_feed_dead_when_nothing_answers() -> None:
    from ashare_decision_support.news.health import probe_feeds

    def dead(_name: str) -> list[str]:
        raise TimeoutError("no route")

    report = probe_feeds("demo", ["甲", "乙"], lambda n: n, dead)

    assert report["reachable"] is False
    assert report["unreachable_feeds"] == ["乙", "甲"]


def test_composite_health_rolls_up_and_admits_what_it_did_not_probe() -> None:
    """没实现 health_check 的源要列进 unprobed，不能算它健康。"""
    class _NoProbe(_Slow):
        health_check = None  # type: ignore[assignment]

    live = _Slow("甲", 0.0)
    live.health_check = lambda: {  # type: ignore[method-assign]
        "source": "甲", "reachable": True, "unreachable_feeds": []
    }
    blind = _NoProbe("乙", 0.0)
    composite = CompositeNewsSource(live, blind)

    report = composite.health_check()

    assert report["reachable"] is True
    assert report["unprobed"] == ["乙"]
    assert set(report["sources"]) == {"甲"}


def test_the_thread_pool_has_exactly_one_home() -> None:
    """`fanout.py` 是仓库里唯一允许 import ThreadPoolExecutor 的模块。

    上面三条钉的是行为，这条钉的是**结构**。收口之前仓库里有十份手写线程池，
    它们的差别从来不在并发上，而在保序与失败语义上——而那两样只在盘前跑批时
    表现出来，且不报错。谁再手写一个池子，这条会先红。
    """
    import ast
    import pathlib

    from ashare_decision_support import fanout

    src = pathlib.Path(fanout.__file__).parent
    importers = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module == "concurrent.futures"
        if any(alias.name == "ThreadPoolExecutor" for alias in node.names)
    )
    assert importers == ["fanout.py"]
