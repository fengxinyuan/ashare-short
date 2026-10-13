"""付费档 TickFlow 行情：单位口径、全市场扫描、两路合并。

这个文件的重心是**单位**。付费档和免费档对同一件事用了不同的量纲：

- `volume` 是「手」，新浪是「股」（差 100 倍）
- `ext.change_pct` 是比值，新浪 `changepercent` 是百分数（差 100 倍）
- 流通市值付费档要自己算，新浪直接给但单位是万元（差 10000 倍）

任何一处漏换算，`LiquidityScreen` 20 亿的门槛就变成 20 万或 20 万亿，等于这层
静默失效——而链路会一声不响地跑完。所以下面每条换算都单独钉一个测试。

数值取自 2026-08-02 对同一批标的的两路实测，两边逐字段吻合到新浪的取整精度。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ashare_decision_support.adapters.market_scan import MarketRow, MarketScan
from ashare_decision_support.adapters.tickflow_market import (
    CompositeMarketScanAdapter,
    TickFlowMarketScanAdapter,
)

# 贵州茅台 2026-07-31 收盘实测值。
MAOTAI_QUOTE = {
    "symbol": "600519.SH",
    "last_price": 1350.6,
    "prev_close": 1361.76,
    "open": 1330.03,
    "high": 1355.72,
    "low": 1325.77,
    "volume": 55128,  # 手
    "amount": 7373462600.0,
    "timestamp": 1785481202000,
    "ext": {
        "name": "贵州茅台",
        "change_pct": -0.008195276700740278,  # 比值
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
# 第二只标的，用来验证缺失股本时的降级说明。
CATL_QUOTE = {
    "symbol": "300750.SZ",
    "last_price": 395.3,
    "prev_close": 401.88,
    "volume": 394149,
    "amount": 15602984000.0,
    "ext": {"name": "宁德时代", "change_pct": -0.016373, "turnover_rate": 0.009252},
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


class FakeClient:
    def __init__(self, quotes=None, instruments=None) -> None:
        self.quotes = quotes or FakeQuotes([MAOTAI_QUOTE])
        self.instruments = instruments or FakeInstruments([MAOTAI_INSTRUMENT])


def _scan(**kwargs) -> MarketScan:
    client = FakeClient(**kwargs)
    return TickFlowMarketScanAdapter(client).fetch()


# 单位口径


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
    assert row.float_cap_yi == pytest.approx(16883.6, abs=0.1)  # 实测 16883.6 亿


def test_the_liquidity_threshold_actually_bites_at_the_right_magnitude() -> None:
    """把单位错误放到它真正起作用的地方验一次，而不只验字段值。"""
    small = MarketRow(code="000001", name="小票", price=10.0, float_cap=15e8, amount=1e8)
    big = MarketRow(code="000002", name="大票", price=10.0, float_cap=150e8, amount=1e8)

    assert small.tradable() is False  # 15 亿 < 20 亿门槛
    assert big.tradable() is True


def test_a_halted_symbol_is_reported_missing_not_priced_at_zero() -> None:
    halted = dict(MAOTAI_QUOTE, last_price=0.0)
    scan = _scan(quotes=FakeQuotes([halted]))

    assert scan.get("600519") is None
    assert not scan.ok


# 全市场扫描


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
    assert scan.get("300750").tradable() is True  # 未测到 → 放行
    assert any("可交易性筛选对它们不生效" in note for note in scan.notes)


# 两路合并


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

    assert row.float_cap == 100e8  # 主源已有，不被覆盖
    assert row.pe == 32.5  # 主源为空，用兜底补上


def test_merge_takes_the_union_of_symbols() -> None:
    primary = MarketScan(rows={"600519": _row("600519")})
    fallback = MarketScan(rows={"000001": _row("000001")})

    merged = CompositeMarketScanAdapter(StubScan(primary), StubScan(fallback)).fetch()

    assert set(merged.rows) == {"600519", "000001"}


def test_merge_preserves_partial_status_and_oldest_quote_time() -> None:
    primary = MarketScan(
        rows={"600519": _row("600519")},
        complete=False,
        as_of=datetime(2026, 9, 14, 13, 51),
    )
    fallback = MarketScan(
        rows={"000001": _row("000001")},
        complete=False,
        as_of=datetime(2026, 9, 14, 13, 48),
    )

    merged = CompositeMarketScanAdapter(StubScan(primary), StubScan(fallback)).fetch()

    assert not merged.complete
    assert merged.as_of == datetime(2026, 9, 14, 13, 48)


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


# 跨源校验

# 合并有个盲区：**两路都有的字段从来没被比过**。主源某个字段悄悄换了量纲、喂了
# 昨天的数、或者标的映射错位，合并会照单全收，而兜底那一路就摆在旁边。
# docs/DATA_SOURCES.md 写着「两路逐字段吻合到新浪的取整精度，可以互为校验」——
# 那是一次手工实测的结论，下面这几条让它变成代码在做的事。


def test_a_hundred_fold_unit_gap_between_the_two_paths_is_flagged() -> None:
    """volume 手/股搞混是记录在案的三处量纲陷阱之一，且**两路都有这个字段**。"""
    primary = MarketScan(rows={"600519": _row("600519", volume=55128.0)})  # 手
    fallback = MarketScan(rows={"600519": _row("600519", volume=5512800.0)})  # 股

    merged = CompositeMarketScanAdapter(StubScan(primary), StubScan(fallback)).fetch()
    notes = " / ".join(merged.notes)

    assert "量纲" in notes
    assert "bug 不是数据分歧" in notes


def test_ordinary_vendor_drift_is_not_reported_as_a_unit_bug() -> None:
    """两家对同一件事的测量本来就有差（实测相对差最大 0.31%），那不是 bug。"""
    primary = MarketScan(rows={"600519": _row("600519", amount=7373462600.0)})
    fallback = MarketScan(rows={"600519": _row("600519", amount=7373462600.0 * 1.002)})

    merged = CompositeMarketScanAdapter(StubScan(primary), StubScan(fallback)).fetch()

    assert not any("量纲" in note for note in merged.notes)


def test_agreement_is_stated_so_it_differs_from_never_having_checked() -> None:
    """只报异常的设计里，「比过了一致」和「根本没比」长得一模一样。"""
    rows = {"600519": _row("600519", volume=55128.0, amount=7373462600.0)}
    merged = CompositeMarketScanAdapter(
        StubScan(MarketScan(rows=dict(rows))), StubScan(MarketScan(rows=dict(rows)))
    ).fetch()

    assert any("两路一致" in note for note in merged.notes)


def test_cross_check_failure_never_breaks_the_fetch() -> None:
    """校验挂了不该拖垮取数——它是旁证，不是关键路径。"""
    import ashare_decision_support.adapters.tickflow_market as module

    original = module.cross_check
    try:
        module.cross_check = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        merged = CompositeMarketScanAdapter(
            StubScan(MarketScan(rows={"600519": _row("600519")})),
            StubScan(MarketScan(rows={"600519": _row("600519")})),
        ).fetch()
    finally:
        module.cross_check = original

    assert merged.ok and len(merged) == 1
