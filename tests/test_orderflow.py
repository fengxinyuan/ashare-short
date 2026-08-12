"""五档盘口读数：L1 事实层。

这一层最容易出的错不是算错，是**把一个可撤销的挂单当成成交证据**，以及
**在涨跌停这种退化状态下照常输出连续读数**。下面的测试大半在钉这两件事。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from ashare_decision_support.adapters.snapshot import (
    TENCENT_AMOUNT_TO_YUAN,
    TENCENT_VOLUME_TO_SHARES,
    OrderBookLevel,
    Snapshot,
    SnapshotAdapter,
)
from ashare_decision_support.monitor.triggers import TriggerEngine
from ashare_decision_support.signals.orderflow import (
    WALL_MIN_VALUE_YUAN,
    read_many,
    read_order_book,
)
from ashare_decision_support.state.alerts import AlertState

ALERT_NOW = datetime(2026, 8, 3, 10, 30)


def book(prices_volumes: list[tuple[float, float]]) -> list[OrderBookLevel]:
    return [OrderBookLevel(price=price, volume=volume) for price, volume in prices_volumes]


def snapshot(
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
    last: float = 20.0,
    limit_up: float | None = None,
    limit_down: float | None = None,
) -> Snapshot:
    return Snapshot(
        symbol="600519.SH",
        name="测试",
        last_price=last,
        prev_close=last,
        open_price=last,
        high=last,
        low=last,
        volume=1_000_000.0,
        amount=1_000_000.0 * last,
        as_of=datetime(2026, 7, 31, 14, 30, 0),
        bids=book(bids or []),
        asks=book(asks or []),
        limit_up=limit_up,
        limit_down=limit_down,
        source="tencent",
    )


# ---- 量纲 ----------------------------------------------------------------


TENCENT_LINE = (
    'v_sz300750="51~宁德时代~300750~395.30~401.88~395.55~394149~188920~205229~395.30~6~395.29~3~'
    "395.28~5~395.26~14~395.25~1~395.60~22~395.66~2~395.74~2~395.80~2~395.81~1~~20260731161442~"
    "-6.58~-1.64~400.20~389.00~395.30/394149/15602984033~394149~1560298~0.93~21.52~~400.20~389.00~"
    "2.79~16840.10~18289.17~4.82~482.26~321.50~1.01~0~395.86~21.13~25.33~~~1.17~1560298.4033~"
    '252.9920~64~ A A~GP-A-CYB~9.70~3.21~1.97~22.41~8.03~468.75~249.81~9.81~4.03~-12.47~4260081019~'
    '4626654919~0.00~6.25~4260081019~~~53.99~-0.41~~CNY~0~~396.00~-115~";'
)


class StubHttp:
    def __init__(self, body: str) -> None:
        self.body = body

    def __call__(self, request, timeout=None):
        class Response:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def read(self) -> bytes:
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Response(self.body.encode("gbk", errors="replace"))


def test_tencent_volumes_are_normalised_to_the_same_unit_as_sina() -> None:
    """同一个 `Snapshot` 字段，含义不能取决于是哪个源答的。

    腾讯报手与万元，新浪报股与元。2026-08-02 实测 600519 同一时刻两边的比值是
    99.999 和 10000.004——不是近似，是单位。归一化不做的话，任何同时消费两个源
    的代码都会拿到差 100 倍的挂单量，而且不会报错。
    """
    adapter = SnapshotAdapter(prefer="tencent", opener=StubHttp(TENCENT_LINE))
    snap = adapter.get_snapshots(["300750.SZ"])[0]["300750.SZ"]

    # 原始报文里 volume=394149（手）、amount=1560298（万元）。
    assert snap.volume == 394149 * TENCENT_VOLUME_TO_SHARES
    assert snap.amount == 1560298 * TENCENT_AMOUNT_TO_YUAN

    # 归一化之后，amount/volume 必须落回股价量级——这是量纲对不对的自证方式。
    assert 0.9 <= (snap.amount / snap.volume) / snap.last_price <= 1.1

    # 五档同样是手，同样要换。买一 6 手 = 600 股。
    assert snap.bids[0].price == 395.30
    assert snap.bids[0].volume == 6 * TENCENT_VOLUME_TO_SHARES


# ---- 委比 ----------------------------------------------------------------


def test_imbalance_is_signed_and_bounded() -> None:
    reading = read_order_book(
        snapshot(bids=[(19.99, 30000.0), (19.98, 10000.0)], asks=[(20.01, 10000.0)])
    )

    assert reading is not None
    assert reading.imbalance == 0.6  # (40000-10000)/50000
    assert reading.bid_shares == 40000.0
    assert reading.ask_shares == 10000.0
    assert reading.is_degenerate is False
    assert reading.is_tradable_now is True


def test_a_sealed_limit_up_is_flagged_degenerate_not_reported_as_maximum_strength() -> None:
    """封死涨停时卖盘五档为空，委比恒等于 +1.0。

    这不是「强到极致」的连续读数，是**封板**这个离散事实。不标出来的话，任何
    按委比排序的地方都会把涨停票顶到第一位——而它恰恰是**买不进**的那只。
    """
    reading = read_order_book(
        snapshot(bids=[(22.0, 5_000_000.0)], asks=[], last=22.0, limit_up=22.0, limit_down=18.0)
    )

    assert reading is not None
    assert reading.imbalance == 1.0
    assert reading.is_degenerate is True
    assert reading.at_limit_up is True
    assert reading.is_tradable_now is False
    assert any("封涨停" in note for note in reading.notes)


def test_limit_state_stays_unknown_when_no_limit_price_was_published() -> None:
    """新浪不发涨跌停价。此时按 9.5% 猜会把 20% 板整类判错，所以不猜。"""
    reading = read_order_book(snapshot(bids=[(19.99, 100.0)], asks=[(20.01, 100.0)]))

    assert reading is not None
    assert reading.at_limit_up is None
    assert reading.at_limit_down is None


# ---- 挂单墙 --------------------------------------------------------------


def test_a_dominant_level_is_reported_as_a_wall() -> None:
    reading = read_order_book(
        snapshot(
            bids=[(19.90, 500000.0), (19.89, 20000.0), (19.88, 15000.0)],
            asks=[(20.01, 10000.0)],
        )
    )

    assert reading is not None
    assert reading.bid_wall is not None
    assert reading.bid_wall.price == 19.90
    assert reading.bid_wall.share_of_side > 0.9
    assert "买盘墙" in reading.bid_wall.describe()


def test_an_evenly_spread_book_has_no_wall() -> None:
    """五档平均分布时报「墙」是在无中生有——那是正常盘口的样子。"""
    reading = read_order_book(
        snapshot(
            bids=[(19.9, 20000.0), (19.89, 19000.0), (19.88, 21000.0), (19.87, 20000.0)],
            asks=[(20.01, 20000.0), (20.02, 19000.0)],
        )
    )

    assert reading is not None
    assert reading.bid_wall is None
    assert reading.ask_wall is None


def test_two_level_book_does_not_call_fifty_percent_a_wall() -> None:
    """只有两档有挂单时，各占 50% 是必然，不是信号。

    只用「占本方份额」判定就会在这里误报——这正是同时要求「明显高过第二大」的
    原因。
    """
    reading = read_order_book(
        snapshot(bids=[(19.9, 100000.0), (19.89, 95000.0)], asks=[(20.01, 5000.0)])
    )

    assert reading is not None
    assert reading.bid_wall is None


def test_a_tiny_absolute_order_is_not_a_wall_however_dominant() -> None:
    """挂 200 股在任何票上都不是墙，哪怕它占本方五档的 99%。"""
    reading = read_order_book(
        snapshot(bids=[(19.9, 200.0), (19.89, 5.0)], asks=[(20.01, 100.0)], last=20.0)
    )

    assert reading is not None
    assert WALL_MIN_VALUE_YUAN > 19.9 * 200.0
    assert reading.bid_wall is None


def test_a_wall_must_be_large_relative_to_what_the_stock_actually_trades() -> None:
    """**实测教训。** 2026-08-02 收盘后的茅台：买一挂 5 手 = 68 万元，过了
    绝对门槛、占本方五档 56%，被报成「买盘墙」——而 68 万在一只当日成交 73.7
    亿的票上什么都挡不住。

    绝对金额门槛在 3 元的票和 1350 元的票之间无法同时成立，所以再卡一道相对
    当日成交额的比例。它自己会缩放。
    """
    thin = snapshot(
        bids=[(1350.50, 500.0), (1350.45, 100.0)],
        asks=[(1350.67, 3100.0), (1350.70, 100.0)],
        last=1350.60,
    )
    # 当日成交 73.7 亿：0.1% = 737 万，68 万的挂单够不着。
    busy = Snapshot(**{**thin.__dict__, "amount": 7_373_462_605.0})
    assert read_order_book(busy).bid_wall is None

    # 同样一堵挂单，放在一只当日只成交 2000 万的冷门票上就有意义了。
    quiet = Snapshot(**{**thin.__dict__, "amount": 20_000_000.0})
    assert read_order_book(quiet).bid_wall is not None


def test_a_missing_turnover_falls_back_to_the_absolute_floor_not_to_letting_it_through() -> None:
    """「没测到」不该变成「通过」——那是让数据缺失冒充判断。"""
    without_amount = Snapshot(
        symbol="600519.SH",
        name="测试",
        last_price=20.0,
        prev_close=20.0,
        open_price=20.0,
        high=20.0,
        low=20.0,
        volume=None,
        amount=None,
        as_of=None,
        bids=book([(19.9, 100.0), (19.89, 5.0)]),
        asks=book([(20.01, 100.0)]),
        source="sina",
    )
    # 19.9 × 100 股 = 1990 元，远低于绝对门槛。
    assert read_order_book(without_amount).bid_wall is None


def test_a_wall_always_carries_the_caveat_that_it_can_be_pulled() -> None:
    """撤单成本为零。托单和承接在盘口上长得一模一样，读数不能替人做这个判断。"""
    reading = read_order_book(
        snapshot(bids=[(19.90, 500000.0), (19.89, 1000.0)], asks=[(20.01, 10000.0)])
    )

    assert reading is not None
    assert any("撤单成本为零" in note for note in reading.notes)


# ---- 价差与缺数据 --------------------------------------------------------


def test_a_wide_spread_warns_against_quoting_the_book_price() -> None:
    reading = read_order_book(snapshot(bids=[(19.5, 10000.0)], asks=[(20.5, 10000.0)], last=20.0))

    assert reading is not None
    assert reading.spread_pct == 0.05
    assert any("盘口偏薄" in note for note in reading.notes)


def test_an_empty_book_is_degenerate_not_balanced() -> None:
    """一侧全空时委比没有意义。报 ±1.0 而不说明，读的人会当成极端强弱。"""
    reading = read_order_book(snapshot(bids=[(19.9, 1000.0)], asks=[]))

    assert reading is not None
    assert reading.is_degenerate is True
    assert any("单边为空" in note for note in reading.notes)


def test_a_zero_priced_snapshot_yields_nothing_rather_than_a_zero_reading() -> None:
    assert read_order_book(snapshot(last=0.0)) is None


def test_read_many_skips_unreadable_symbols_without_failing_the_batch() -> None:
    readings = read_many(
        {
            "600519.SH": snapshot(bids=[(19.9, 1000.0)], asks=[(20.1, 1000.0)]),
            "000001.SZ": snapshot(last=0.0),
        }
    )

    assert list(readings) == ["600519.SH"]


# ---- 盘口进告警 ----------------------------------------------------------
#
# 读数进了候选价位还不够。告警回答的是「现在做什么」，而五档回答的恰恰是
# **这件事此刻做不做得成**——封死涨停买不进、封死跌停卖不出。少了这一层，
# 监控会照常发一条 ACTION 催人去做一个做不到的动作。


class Advice:
    def __init__(self, entry=(19.80, 20.20), stop=19.00, targets=(21.0,)):
        self.symbol = "600519.SH"
        self.name = "测试"
        self.entry_low, self.entry_high = entry
        self.stop = stop
        self.targets = list(targets)
        self.rrr = 2.0
        self.trend_state = "REBOUND"
        self.scenarios = []
        self.warnings = []


class Intraday:
    def __init__(self, price, vwap=None, volume_ratio=1.0):
        self.last_price = price
        self.vwap = vwap if vwap is not None else price
        self.session_high = price * 1.01
        self.session_low = price * 0.99
        self.volume_ratio = volume_ratio
        self.last_timestamp = ALERT_NOW
        self.session_date = date(2026, 8, 3)
        self.bar_count = 60


def fire(engine, state, advice, intraday, book=None, holding=None):
    return engine.evaluate(
        advice=advice, intraday=intraday, state=state, now=ALERT_NOW,
        holding=holding, book=book,
    )


def fresh():
    return TriggerEngine(), AlertState(date(2026, 8, 3), Path("/dev/null"))


def sealed_up_book() -> object:
    """封死涨停：卖盘五档全空，价格贴在涨停价上。"""
    return read_order_book(
        snapshot(bids=[(22.00, 500000.0)], asks=[], last=22.00, limit_up=22.00)
    )


def test_a_sealed_limit_up_downgrades_the_buy_alert_instead_of_urging_a_trade() -> None:
    """区间是真的进了，但这一刻买不进。按 ACTION 发等于催人做一件做不成的事。

    不静默丢弃是刻意的：价位确实到了，你可能想为开板做准备——但标题必须说清
    此刻的状态，而不是让人照着一条「进入买入区间」去挂一笔挂不进的单。
    """
    engine, state = fresh()
    book = sealed_up_book()

    fire(engine, state, Advice(), Intraday(19.00), book)             # 区间外，建立基线
    alerts = fire(engine, state, Advice(), Intraday(20.00), book)    # 进区间

    assert [item.trigger for item in alerts] == ["entry_sealed"]
    assert alerts[0].level == "WARN"
    assert "买不进" in alerts[0].headline
    assert any("封涨停" in line for line in alerts[0].detail)


def test_an_unknown_limit_state_never_suppresses_a_buy_alert() -> None:
    """拿不到涨跌停价时 `at_limit_up` 是 None。**未知不能当成封板**——那会让
    一个缺失的字段悄悄压掉本该发的 ACTION。"""
    engine, state = fresh()
    book = read_order_book(snapshot(bids=[(19.9, 10000.0)], asks=[(20.1, 10000.0)], last=20.0))

    assert book.at_limit_up is None

    fire(engine, state, Advice(), Intraday(19.00), book)
    alerts = fire(engine, state, Advice(), Intraday(20.00), book)

    assert [item.trigger for item in alerts] == ["entry_zone"]
    assert alerts[0].level == "ACTION"


def test_a_stop_break_says_when_the_position_cannot_actually_be_sold() -> None:
    """封死跌停时「跌破止损」照发——你必须知道——但它必须同时说清此刻卖不出。

    漏掉这句话，读的人会以为挂单就能走掉，而实际要么等开板要么等次日。
    """
    engine, state = fresh()
    book = read_order_book(
        snapshot(bids=[], asks=[(18.00, 900000.0)], last=18.00, limit_down=18.00)
    )

    fire(engine, state, Advice(), Intraday(19.50), book)
    alerts = fire(engine, state, Advice(), Intraday(18.00), book)

    assert [item.trigger for item in alerts] == ["stop_break"]
    assert any("封跌停" in line and "卖不出" in line for line in alerts[0].detail)


def test_an_actionable_alert_carries_the_book_as_bounded_side_evidence() -> None:
    """要求下单动作的告警带上盘口：那个价位此刻有没有人挂着，是它独有的信息。

    但必须带着「可撤」的限定语一起走——没有它，一堵墙读起来就成了承接证据。
    """
    engine, state = fresh()
    book = read_order_book(
        snapshot(
            bids=[(19.90, 500000.0), (19.89, 1000.0)],
            asks=[(20.10, 10000.0)],
            last=20.0,
        )
    )

    fire(engine, state, Advice(), Intraday(19.00), book)
    alerts = fire(engine, state, Advice(), Intraday(20.00), book)

    line = next(item for item in alerts[0].detail if item.startswith("盘口"))
    assert "买盘墙" in line
    assert "挂单可撤" in line


def test_a_status_alert_does_not_get_the_book_bolted_on() -> None:
    """放量异动是状态通报，不要求任何下单动作。贴两行挂单量只是噪音——
    告警一长，真正要读的那条就沉下去了。"""
    engine, state = fresh()
    book = read_order_book(
        snapshot(bids=[(19.90, 500000.0), (19.89, 1000.0)], asks=[(20.10, 10000.0)], last=20.0)
    )

    fire(engine, state, Advice(), Intraday(20.00, volume_ratio=1.0), book)
    alerts = fire(engine, state, Advice(), Intraday(20.00, volume_ratio=4.0), book)

    spike = next(item for item in alerts if item.trigger == "volume_spike")
    assert not any(line.startswith("盘口") for line in spike.detail)


def test_a_degenerate_book_never_prints_an_imbalance_reading() -> None:
    """封板时委比恒为 ±1.0。把一个常数印成读数，读的人会当成强度。"""
    lines = sealed_up_book().describe()

    assert any("封涨停" in line for line in lines)
    assert not any("委比" in line for line in lines)
