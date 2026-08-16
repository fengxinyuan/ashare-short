"""组合层读数：三只票什么时候其实是一笔仓位。

这一层要钉住的不是算术（权重求和不会错），是**三处会静默给出错误安全感的地方**：

1. 缺股数时按金额算 → 缺的那几只权重变 0，集中度看起来更低；
2. 题材不过滤 → 最大的一块永远是「融资融券」，真正的题材集中被它盖住；
3. 取不到元数据 → 归并表为空，读起来和「不集中」一模一样。
"""

from __future__ import annotations

from types import SimpleNamespace

from ashare_decision_support.strategy.portfolio import (
    BASIS_AMOUNT,
    BASIS_COUNT,
    read_portfolio,
)


class _Info:
    def __init__(self, industry: str | None, concepts: tuple[str, ...]) -> None:
        self.industries = (industry,) if industry else ()
        self.concepts = concepts

    @property
    def industry(self) -> str | None:
        return self.industries[0] if self.industries else None


class _Master:
    """标的元数据的桩。`themes()` 是过滤后的那份，和真实实现一样。"""

    ok = True

    def __init__(self, table: dict[str, _Info], themes: dict[str, int] | None = None) -> None:
        self.table = table
        self._themes = themes if themes is not None else {"PCB": 40}

    def get(self, symbol: str) -> _Info | None:
        return self.table.get(symbol)

    def themes(self) -> dict[str, int]:
        return self._themes


def _position(symbol: str, cost: float | None = 10.0, shares: int | None = 1000, **kwargs):
    return SimpleNamespace(
        symbol=symbol,
        cost_price=cost,
        shares=shares,
        plan_stop=kwargs.get("stop", 9.0),
        plan_target=kwargs.get("target", 12.0),
    )


def test_empty_book_says_nothing() -> None:
    """空仓不该产出一句「暂无持仓」——那是噪声，不是读数。"""
    read = read_portfolio([])
    assert read.count == 0
    assert read.describe() == []
    assert read.top is None


def test_three_symbols_in_one_theme_are_reported_as_one_position() -> None:
    rows = [_position("300588.SZ"), _position("002138.SZ"), _position("600519.SH")]
    master = _Master(
        {
            "300588.SZ": _Info("电子", ("PCB", "融资融券")),
            "002138.SZ": _Info("电子", ("PCB",)),
            "600519.SH": _Info("食品饮料", ("融资融券",)),
        }
    )
    read = read_portfolio(rows, master=master)

    assert read.basis == BASIS_AMOUNT
    assert read.cost_value == 30000.0
    labels = {(cluster.level, cluster.label): cluster.weight for cluster in read.clusters}
    assert labels[("行业", "电子")] > 0.66
    assert labels[("题材", "PCB")] > 0.66
    # 「融资融券」覆盖 69% 的市场，是可交易属性不是题材——它进了归并表，
    # 「今天押在什么上面」这个问题的答案就永远是它。
    assert not any(cluster.label == "融资融券" for cluster in read.clusters)
    assert any("按一笔仓位看待" in line for line in read.describe())


def test_a_single_name_over_half_is_called_out() -> None:
    rows = [_position("300588.SZ", cost=90.0), _position("002138.SZ", cost=10.0)]
    read = read_portfolio(rows)
    assert read.top == ("300588.SZ", 0.9)
    assert any("单票集中" in line for line in read.describe())


def test_missing_share_count_falls_back_to_equal_weight_and_says_so() -> None:
    """一半有股数一半没有时按金额算，缺的那几只权重会变成 0——而 0 权重的方向
    刚好是「看起来更分散」。这条钉的是那个静默的方向。"""
    rows = [_position("A", shares=None), _position("B", shares=1000)]
    read = read_portfolio(rows)

    assert read.basis == BASIS_COUNT
    assert dict(read.weights) == {"A": 0.5, "B": 0.5}
    assert read.cost_value is None
    assert any("按只数等权" in note for note in read.notes)


def test_no_metadata_is_not_the_same_as_no_concentration() -> None:
    read = read_portfolio([_position("A"), _position("B")], master=None)
    assert read.clusters == ()
    assert any("元数据不可用" in note for note in read.notes)


def test_a_single_position_does_not_claim_the_metadata_is_missing() -> None:
    """一只票归并不出东西，**这不是降级**。

    写「元数据不可用」会让人以为少了一份本该有的读数，然后去查一个不存在的故障。
    """
    read = read_portfolio([_position("A")], master=None)
    assert read.clusters == ()
    assert read.notes == ()


def test_the_metadata_cache_is_not_read_for_a_book_that_cannot_cluster(monkeypatch) -> None:
    """那份缓存是 1.9MB / 5550 只，解析一次约 58ms——而装配走在每次问答上。

    空仓和单票读它是纯付出：归并至少要两只。这条钉的是「不该读的时候真的没读」，
    因为它的代价不会让任何断言变红，只会让每次 `facts` 慢一点。
    """
    from datetime import datetime

    from ashare_decision_support.adapters import security_master
    from ashare_decision_support.ask.context import assemble
    from ashare_decision_support.state import Positions

    reads: list[int] = []
    monkeypatch.setattr(
        security_master.SecurityMasterAdapter,
        "cached",
        lambda self: reads.append(1) or security_master.SecurityMaster(),
    )

    book = Positions()
    book.open("300588.SZ", cost_price=20.0, shares=1000)
    assemble(datetime(2026, 7, 31, 8, 30))
    assert reads == []

    book.open("002138.SZ", cost_price=30.0, shares=1000)
    assemble(datetime(2026, 7, 31, 8, 30))
    assert reads == [1]


def test_symbols_missing_from_the_metadata_are_named() -> None:
    master = _Master({"A": _Info("电子", ("PCB",))})
    read = read_portfolio([_position("A"), _position("B")], master=master)
    assert any("查不到" in note and "B" in note for note in read.notes)


def test_a_position_without_a_stop_is_reported_as_a_missing_exit() -> None:
    rows = [_position("A", stop=None), _position("B")]
    read = read_portfolio(rows)
    assert any("没有失效条件" in gap for gap in read.plan_gaps)
    assert any("计划缺口" in line for line in read.describe())


def test_facts_bundle_carries_the_portfolio_read() -> None:
    """事实包是外部 AI 的唯一入口，组合层必须在里面，不能只在 CLI 里。"""
    from datetime import datetime

    from ashare_decision_support.ask.context import assemble
    from ashare_decision_support.state import Positions

    book = Positions()
    book.open("300588.SZ", cost_price=20.0, shares=1000, plan_stop=18.0)
    book.open("002138.SZ", cost_price=30.0, shares=1000)

    block = assemble(datetime(2026, 7, 31, 8, 30)).blocks["持仓"]

    assert block["持仓数"] == 2
    assert block["权重口径"] == BASIS_AMOUNT
    assert [item["symbol"] for item in block["明细"]] == ["300588.SZ", "002138.SZ"]
    assert any("没有失效条件" in gap for gap in block["计划缺口"])


def test_position_list_carries_the_same_read() -> None:
    """`position list` 是唯一一个「一次看到全部持仓」的地方。

    逐行明细里看不出「这几笔合起来是什么」，而这条命令正是人会随手敲的那条。
    """
    from ashare_decision_support.cli import _position_command
    from ashare_decision_support.state import Positions

    Positions().open("300588.SZ", cost_price=20.0, shares=1000)
    payload = _position_command(SimpleNamespace(action="list"))

    assert payload["total"] == 1
    assert payload["portfolio"]["持仓数"] == 1
    assert any("持仓 1 只" in line for line in payload["portfolio_notes"])
