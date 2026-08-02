"""付费档 TickFlow 行情：单位口径、涨跌停判定、两路合并。

这个文件的重心是**单位**。付费档和免费档对同一件事用了不同的量纲：

- `volume` 是「手」，新浪是「股」（差 100 倍）
- `ext.change_pct` 是比值，新浪 `changepercent` 是百分数（差 100 倍）
- 流通市值付费档要自己算，新浪直接给但单位是万元（差 10000 倍）

任何一处漏换算，`LiquidityScreen` 20 亿的门槛就变成 20 万或 20 万亿，等于这层
静默失效——而链路会一声不响地跑完。所以下面每条换算都单独钉一个测试。

数值取自 2026-08-02 对同一批标的的两路实测，两边逐字段吻合到新浪的取整精度。
"""

from __future__ import annotations

import pytest

from ashare_decision_support.adapters.market_scan import MarketRow, MarketScan
from ashare_decision_support.adapters.tickflow_market import (
    CompositeMarketScanAdapter,
    TickFlowMarketScanAdapter,
    TickFlowSnapshotAdapter,
)

# 贵州茅台 2026-07-31 收盘实测值。
MAOTAI_QUOTE = {
    "symbol": "600519.SH",
    "last_price": 1350.6,
    "prev_close": 1361.76,
    "open": 1330.03,
    "high": 1355.72,
    "low": 1325.77,
    "volume": 55128,                     # 手
    "amount": 7373462600.0,
    "timestamp": 1785481202000,
    "ext": {
        "name": "贵州茅台",
        "change_pct": -0.008195276700740278,   # 比值
        "turnover_rate": 0.0044099521147979845,
    },
}
MAOTAI_INSTRUMENT = {
    "symbol": "600519.SH",
    "name": "贵州茅台",
    "ext": {
        "total_shares": 1250081601.0,
        "float_shares": 1250081601.0,
        "limit_up": 1497.94,
        "limit_down": 1225.58,
    },
}
MAOTAI_DEPTH = {
    "symbol": "600519.SH",
    "timestamp": 1785483056001,
    "bid_prices": [1350.6, 1350.55, 1350.5, 1350.4, 1350.35],
    "bid_volumes": [1, 1, 5, 1, 1],      # 手
    "ask_prices": [1350.67, 1350.7, 1350.88, 1350.98, 1351.0],
    "ask_volumes": [31, 1, 11, 3, 6],
}

# 宁德时代：创业板 20% 涨停，用来验证「不是所有票都是 10%」。
CATL_QUOTE = {
    "symbol": "300750.SZ",
    "last_price": 395.3,
    "prev_close": 401.88,
    "volume": 394149,
    "amount": 15602984000.0,
    "ext": {"name": "宁德时代", "change_pct": -0.016373, "turnover_rate": 0.009252},
}
CATL_INSTRUMENT = {
    "symbol": "300750.SZ",
    "name": "宁德时代",
    "ext": {
        "total_shares": 4400000000.0,
        "float_shares": 4260081019.0,
        "limit_up": 482.26,              # 20%，不是 10%
        "limit_down": 321.5,
    },
}


class FakeQuotes:
    def __init__(self, rows, boom: bool = False) -> None:
        self.rows = rows
        self.boom = boom
        self.calls: list[dict] = []

    def get(self, *, symbols=None, universes=None, as_dataframe=False):
        self.calls.append({"symbols": symbols, "universes": universes})
        if self.boom:
            raise RuntimeError("quotes down")
        if symbols is None:
            return list(self.rows)
        wanted = set(symbols)
        return [row for row in self.rows if row["symbol"] in wanted]


class FakeInstruments:
    def __init__(self, rows, boom: bool = False) -> None:
        self.rows = {row["symbol"]: row for row in rows}
        self.boom = boom
        self.calls: list[list[str]] = []

    def batch(self, symbols):
        self.calls.append(list(symbols))
        if self.boom:
            raise RuntimeError("instruments down")
        return [self.rows[s] for s in symbols if s in self.rows]


class FakeDepth:
    def __init__(self, rows, boom: bool = False) -> None:
        self.rows = {row["symbol"]: row for row in rows}
        self.boom = boom

    def batch(self, *, symbols, **_):
        if self.boom:
            raise RuntimeError("depth down")
        return {s: self.rows[s] for s in symbols if s in self.rows}


class FakeClient:
    def __init__(self, quotes=None, instruments=None, depth=None) -> None:
        self.quotes = quotes or FakeQuotes([MAOTAI_QUOTE])
        self.instruments = instruments or FakeInstruments([MAOTAI_INSTRUMENT])
        self.depth = depth or FakeDepth([MAOTAI_DEPTH])


def _scan(**kwargs) -> MarketScan:
    client = FakeClient(**kwargs)
    return TickFlowMarketScanAdapter(client).fetch()


# ---- 单位口径 -------------------------------------------------------------


def test_volume_is_converted_from_lots_to_shares() -> None:
    """TickFlow 报「手」，新浪报「股」。同一时刻茅台 55128 手 = 5512800 股。"""
    row = _scan().get("600519")

    assert row.volume == pytest.approx(5_512_800.0)


def test_change_pct_is_converted_from_ratio_to_percent() -> None:
    """`MarketRow.change_pct` 沿用新浪的百分数口径，TickFlow 给的是比值。"""
    row = _scan().get("600519")

    assert row.change_pct == pytest.approx(-0.82, abs=1e-2)


def test_turnover_is_converted_from_ratio_to_percent() -> None:
    row = _scan().get("600519")

    assert row.turnover_ratio == pytest.approx(0.441, abs=1e-3)


def test_float_cap_is_computed_in_yuan_not_in_wan() -> None:
    """流通市值 = 流通股本 × 现价，单位元。

    新浪那路的 `nmc` 是万元，两条路径必须归一到元——差 10000 倍的话，20 亿的
    可交易性门槛会变成 20 万，等于这层筛选没开。
    """
    row = _scan().get("600519")

    assert row.float_cap == pytest.approx(1250081601.0 * 1350.6)
    assert row.float_cap_yi == pytest.approx(16883.6, abs=0.1)   # 实测 16883.6 亿


def test_the_liquidity_threshold_actually_bites_at_the_right_magnitude() -> None:
    """把单位错误放到它真正起作用的地方验一次，而不只验字段值。"""
    small = MarketRow(code="000001", name="小票", price=10.0, float_cap=15e8, amount=1e8)
    big = MarketRow(code="000002", name="大票", price=10.0, float_cap=150e8, amount=1e8)

    assert small.tradable() is False       # 15 亿 < 20 亿门槛
    assert big.tradable() is True


# ---- 涨跌停 ---------------------------------------------------------------


def test_real_limit_prices_replace_the_nine_point_five_percent_guess() -> None:
    """此前用「涨幅 ≥ 9.5%」近似涨停，分不清主板 10% 和创业板 20%。"""
    client = FakeClient(
        quotes=FakeQuotes([MAOTAI_QUOTE, CATL_QUOTE]),
        instruments=FakeInstruments([MAOTAI_INSTRUMENT, CATL_INSTRUMENT]),
        depth=FakeDepth([]),
    )
    snapshots, _ = TickFlowSnapshotAdapter(client).get_snapshots(["600519.SH", "300750.SZ"])

    maotai_pct = snapshots["600519.SH"].limit_up / MAOTAI_QUOTE["prev_close"] - 1
    catl_pct = snapshots["300750.SZ"].limit_up / CATL_QUOTE["prev_close"] - 1
    assert maotai_pct == pytest.approx(0.10, abs=1e-3)     # 主板
    assert catl_pct == pytest.approx(0.20, abs=1e-3)       # 创业板


def test_at_limit_up_is_decided_by_the_real_price() -> None:
    sealed = dict(CATL_QUOTE, last_price=482.26)
    client = FakeClient(
        quotes=FakeQuotes([sealed]),
        instruments=FakeInstruments([CATL_INSTRUMENT]),
        depth=FakeDepth([]),
    )
    snapshots, _ = TickFlowSnapshotAdapter(client).get_snapshots(["300750.SZ"])

    assert snapshots["300750.SZ"].at_limit_up is True


def test_missing_instruments_leave_the_limit_verdict_unknown_not_false() -> None:
    """三态是刻意的：读不到涨停价时说「不知道」，不许退回百分比猜测。"""
    client = FakeClient(
        quotes=FakeQuotes([MAOTAI_QUOTE]),
        instruments=FakeInstruments([], boom=True),
        depth=FakeDepth([]),
    )
    snapshots, notes = TickFlowSnapshotAdapter(client).get_snapshots(["600519.SH"])

    assert snapshots["600519.SH"].at_limit_up is None
    assert any("涨跌停价" in note for note in notes)


# ---- 盘口 -----------------------------------------------------------------


def test_order_book_volumes_are_also_lots() -> None:
    """盘口量和成交量是同一个量纲陷阱，实测 TickFlow [1,1,5] 对新浪 [100,100,500]。"""
    snapshots, _ = TickFlowSnapshotAdapter(FakeClient()).get_snapshots(["600519.SH"])
    book = snapshots["600519.SH"]

    assert [level.volume for level in book.bids[:3]] == [100.0, 100.0, 500.0]


def test_depth_failure_degrades_to_a_quote_without_a_book() -> None:
    """缺盘口不是缺价格——快照照发，只是价差读不到。"""
    client = FakeClient(depth=FakeDepth([], boom=True))
    snapshots, notes = TickFlowSnapshotAdapter(client).get_snapshots(["600519.SH"])

    snapshot = snapshots["600519.SH"]
    assert snapshot.last_price == 1350.6
    assert snapshot.bids == [] and snapshot.spread_pct is None
    assert any("五档" in note for note in notes)


def test_a_halted_symbol_is_reported_missing_not_priced_at_zero() -> None:
    halted = dict(MAOTAI_QUOTE, last_price=0.0)
    scan = _scan(quotes=FakeQuotes([halted]))

    assert scan.get("600519") is None
    assert not scan.ok


# ---- 全市场扫描 -----------------------------------------------------------


def test_scan_uses_the_all_a_universe_in_one_request() -> None:
    """按标的池一次拿全市场，而不是像新浪那样翻 56 页。"""
    client = FakeClient()
    TickFlowMarketScanAdapter(client).fetch()

    assert client.quotes.calls == [{"symbols": None, "universes": ["CN_Equity_A"]}]


def test_pe_and_pb_are_left_empty_rather_than_zero() -> None:
    """`financials.*` 在 Pro 档无权限。填 0 会被下游当成真实读数。"""
    row = _scan().get("600519")

    assert row.pe is None and row.pb is None


def test_scan_failure_is_reported_not_raised() -> None:
    scan = _scan(quotes=FakeQuotes([], boom=True))

    assert scan.ok is False and scan.rows == {}
    assert any("不可用" in note for note in scan.notes)


def test_symbols_without_share_counts_are_counted_and_announced() -> None:
    """静默放行和「因为缺数据而放行」是两件事。"""
    scan = _scan(
        quotes=FakeQuotes([MAOTAI_QUOTE, CATL_QUOTE]),
        instruments=FakeInstruments([MAOTAI_INSTRUMENT]),
    )

    assert scan.get("300750").float_cap is None
    assert scan.get("300750").tradable() is True          # 未测到 → 放行
    assert any("可交易性筛选对它们不生效" in note for note in scan.notes)


# ---- 两路合并 -------------------------------------------------------------


class StubScan:
    def __init__(self, scan: MarketScan | None, boom: bool = False) -> None:
        self.scan = scan
        self.boom = boom

    def fetch(self) -> MarketScan:
        if self.boom:
            raise RuntimeError("down")
        return self.scan


def _row(code: str, **kwargs) -> MarketRow:
    return MarketRow(code=code, name=code, price=10.0, **kwargs)


def test_fallback_only_fills_gaps_it_does_not_override() -> None:
    """主源整行为准。逐字段挑「哪个新用哪个」会造出一份哪一路都不等于的读数。"""
    primary = MarketScan(rows={"600519": _row("600519", float_cap=100e8, pe=None)})
    fallback = MarketScan(rows={"600519": _row("600519", float_cap=999e8, pe=32.5)})

    merged = CompositeMarketScanAdapter(StubScan(primary), StubScan(fallback)).fetch()
    row = merged.get("600519")

    assert row.float_cap == 100e8      # 主源已有，不被覆盖
    assert row.pe == 32.5              # 主源为空，用兜底补上


def test_merge_takes_the_union_of_symbols() -> None:
    primary = MarketScan(rows={"600519": _row("600519")})
    fallback = MarketScan(rows={"000001": _row("000001")})

    merged = CompositeMarketScanAdapter(StubScan(primary), StubScan(fallback)).fetch()

    assert set(merged.rows) == {"600519", "000001"}


def test_a_dead_primary_falls_back_instead_of_failing() -> None:
    fallback = MarketScan(rows={"600519": _row("600519")})

    merged = CompositeMarketScanAdapter(StubScan(None, boom=True), StubScan(fallback)).fetch()

    assert merged.ok and len(merged) == 1
    assert any("走兜底源" in note for note in merged.notes)


def test_a_dead_fallback_still_returns_the_primary_but_says_pe_is_missing() -> None:
    primary = MarketScan(rows={"600519": _row("600519")})

    merged = CompositeMarketScanAdapter(StubScan(primary), StubScan(None, boom=True)).fetch()

    assert merged.ok and len(merged) == 1
    assert any("缺 PE/PB" in note for note in merged.notes)


def test_both_dead_is_reported_as_not_ok() -> None:
    merged = CompositeMarketScanAdapter(
        StubScan(None, boom=True), StubScan(None, boom=True)
    ).fetch()

    assert merged.ok is False
