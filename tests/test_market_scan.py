"""全市场扫描与可交易性硬筛。

候选池此前只过 ST 与致命公告——一只 15 亿流通、日成交 3000 万的票和茅台在筛选层面
完全等价。对超短线这是个真窟窿：**核心风险不是买错，是卖不掉**。
"""

from __future__ import annotations

import json

import pytest

from ashare_decision_support.adapters.market_scan import (
    MarketRow,
    MarketScan,
    MarketScanAdapter,
    _parse_row,
)
from ashare_decision_support.screens import LiquidityScreen

# ---- http double ---------------------------------------------------------


def _row(code, name, trade=10.0, changepercent=1.0, amount=5e8, nmc=500000.0, **kw):
    """nmc/mktcap 的单位是**万元**，和新浪一致。500000 万元 = 50 亿。"""
    base = {
        "symbol": f"sh{code}", "code": code, "name": name, "trade": str(trade),
        "changepercent": changepercent, "settlement": str(trade - 0.1),
        "volume": 1000000, "amount": amount, "per": 25.0, "pb": 3.0,
        "mktcap": nmc * 2, "nmc": nmc, "turnoverratio": 2.5,
    }
    base.update(kw)
    return base


class StubHttp:
    def __init__(self, pages: dict[int, list[dict]]) -> None:
        self.pages = pages
        self.asked: list[int] = []

    def __call__(self, request, timeout=None):
        page = int(request.full_url.split("page=")[1].split("&")[0])
        self.asked.append(page)
        body = json.dumps(self.pages.get(page, []), ensure_ascii=False).encode("gbk")
        return _Resp(body)


class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---- parsing -------------------------------------------------------------


def test_market_cap_is_converted_from_wan_yuan() -> None:
    """新浪的 nmc/mktcap 单位是万元。忘了换算，20 亿的门槛会变成 20 万——等于没开。

    量级用实测值：2026-07-31 贵州茅台流通市值 16884 亿元，对应 nmc = 168840000 万元。
    """
    row = _parse_row(_row("600519", "贵州茅台", nmc=168_840_000.0))

    assert row.float_cap == pytest.approx(1.6884e12)
    assert row.float_cap_yi == pytest.approx(16884.0)


def test_a_row_without_a_usable_price_is_dropped() -> None:
    assert _parse_row(_row("600000", "浦发银行", trade=0.0)) is None
    assert _parse_row({"code": "", "name": "", "trade": "1"}) is None


def test_missing_optional_fields_become_none_not_zero() -> None:
    """把「没测到」写成 0，下游就分不出「市值为零」和「不知道市值」。"""
    row = _parse_row(_row("600000", "浦发银行", per="-", pb=None, amount=""))

    assert row.pe is None and row.pb is None and row.amount is None
    assert row.price > 0


def test_st_is_detected_from_the_name() -> None:
    assert _parse_row(_row("000001", "*ST测试")).is_st
    assert _parse_row(_row("000002", "退市样本")).is_st
    assert not _parse_row(_row("000003", "平安银行")).is_st


# ---- scan ----------------------------------------------------------------


def test_scan_collects_pages_until_they_run_dry() -> None:
    http = StubHttp({1: [_row("600000", "A"), _row("600001", "B")], 2: [_row("600002", "C")]})
    scan = MarketScanAdapter(opener=http).fetch(max_pages=4)

    assert len(scan) == 3
    assert scan.get("600002.SH").name == "C"


def test_breadth_counts_the_whole_market_not_a_sample() -> None:
    http = StubHttp({1: [
        _row("600000", "涨", changepercent=2.0),
        _row("600001", "涨2", changepercent=0.5),
        _row("600002", "跌", changepercent=-1.0),
        _row("600003", "平", changepercent=0.0),
    ]})
    scan = MarketScanAdapter(opener=http).fetch(max_pages=1)

    assert scan.advancers == 2
    assert scan.decliners == 1
    # 平盘不进分母：上涨面问的是「动了的票里多少是涨的」。
    assert scan.breadth == pytest.approx(2 / 3, abs=1e-4)


def test_one_dead_page_costs_only_its_own_rows() -> None:
    class HalfDead(StubHttp):
        def __call__(self, request, timeout=None):
            if "page=2&" in request.full_url:
                raise OSError("boom")
            return super().__call__(request, timeout=timeout)

    http = HalfDead({1: [_row("600000", "A")], 2: [_row("600001", "B")]})
    scan = MarketScanAdapter(opener=http).fetch(max_pages=2)

    assert scan.ok and len(scan) == 1
    assert any("1/2 页取数失败" in note for note in scan.notes)


def test_everything_down_reports_not_ok() -> None:
    class Dead(StubHttp):
        def __call__(self, request, timeout=None):
            raise OSError("boom")

    scan = MarketScanAdapter(opener=Dead({})).fetch(max_pages=2)

    assert not scan.ok and len(scan) == 0


def test_symbols_resolve_with_or_without_an_exchange_suffix() -> None:
    http = StubHttp({1: [_row("600519", "贵州茅台")]})
    scan = MarketScanAdapter(opener=http).fetch(max_pages=1)

    assert scan.get("600519") is scan.get("600519.SH")


# ---- liquidity screen ----------------------------------------------------


def _scan_of(*rows: MarketRow) -> MarketScan:
    return MarketScan(rows={r.code: r for r in rows}, ok=True)


BIG = _parse_row(_row("600519", "贵州茅台", nmc=168_840_000.0, amount=73.7e8))
TINY_CAP = _parse_row(_row("301000", "小盘票", nmc=80000.0, amount=5e8))       # 8 亿流通
THIN = _parse_row(_row("600001", "地量票", nmc=500000.0, amount=1900_0000.0))  # 0.19 亿成交


def test_screen_removes_names_you_cannot_get_out_of() -> None:
    screen = LiquidityScreen(_scan_of(BIG, TINY_CAP, THIN))
    kept, rejected, stats = screen.filter(["600519.SH", "301000.SZ", "600001.SH"])

    assert kept == ["600519.SH"]
    assert stats.rejected == 2
    assert any("流通市值" in v.reason for v in rejected)
    assert any("成交额" in v.reason for v in rejected)


def test_thresholds_are_caller_tunable() -> None:
    screen = LiquidityScreen(_scan_of(TINY_CAP), min_float_cap_yi=5.0, min_amount_yi=0.1)
    kept, rejected, _ = screen.filter(["301000.SZ"])

    assert kept == ["301000.SZ"] and rejected == []


def test_a_symbol_with_no_market_row_is_passed_not_vetoed() -> None:
    """拿不到行情去否决标的，等于让数据缺失冒充风险判断。"""
    screen = LiquidityScreen(_scan_of(BIG))
    kept, rejected, stats = screen.filter(["600519.SH", "000999.SZ"])

    assert kept == ["600519.SH", "000999.SZ"]
    assert rejected == []
    assert stats.unmeasured == 1
    assert "未经该层过滤" in stats.note()


def test_no_scan_at_all_degrades_to_pass_through_and_says_so() -> None:
    kept, rejected, stats = LiquidityScreen(None).filter(["600519.SH", "301000.SZ"])

    assert kept == ["600519.SH", "301000.SZ"]
    assert rejected == []
    assert stats.unmeasured == 2
    assert "未经该层过滤" in stats.note()


def test_a_clean_pass_produces_no_note() -> None:
    """全过就别在简报里留噪音。"""
    _, _, stats = LiquidityScreen(_scan_of(BIG)).filter(["600519.SH"])

    assert stats.note() is None


def test_a_row_missing_both_measures_is_treated_as_tradable() -> None:
    blind = _parse_row(_row("600002", "无数据", amount="", nmc=""))
    kept, rejected, _ = LiquidityScreen(_scan_of(blind)).filter(["600002.SH"])

    assert kept == ["600002.SH"] and rejected == []
