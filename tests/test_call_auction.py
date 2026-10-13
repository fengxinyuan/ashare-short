"""集合竞价（09:25）异动初筛单元测试。"""

from __future__ import annotations

from ashare_decision_support.signals.call_auction import (
    AuctionItem,
    evaluate_auction_stock,
    scan_call_auction,
)


def test_empty_call_auction_scan() -> None:
    result = scan_call_auction([])
    assert result.signals == []
    assert result.market_avg_change == 0.0
    assert "无集合竞价输入标的" in result.notes[0]


def test_invalid_pre_close_produces_safe_defaults() -> None:
    item = AuctionItem(
        symbol="600000.SH",
        name="测试票",
        open_price=10.0,
        pre_close=0.0,
        auction_amount=5000000.0,
    )
    signal = evaluate_auction_stock(item)
    assert signal.change_pct == 0.0
    assert signal.amount_ratio is None
    assert signal.tags == ()
    assert "昨收盘价无效" in signal.reason


def test_rush_bidding_detected_on_high_open_and_large_auction_amount() -> None:
    # 涨 3.5%，竞价成交额占昨日 5%
    item = AuctionItem(
        symbol="000001.SZ",
        name="平安银行",
        open_price=10.35,
        pre_close=10.0,
        auction_amount=50_000_000.0,
        prev_amount=1_000_000_000.0,
    )
    sig = evaluate_auction_stock(item)
    assert "抢筹爆量" in sig.tags
    assert sig.change_pct == 0.035
    assert sig.amount_ratio == 0.05
    assert sig.intensity >= 75.0


def test_weak_to_strong_detected_after_previous_loss() -> None:
    # 昨天收阴 -3.5%，今天竞价高开 2.0% 且放量
    item = AuctionItem(
        symbol="002475.SZ",
        name="立讯精密",
        open_price=30.6,
        pre_close=30.0,
        auction_amount=30_000_000.0,
        prev_amount=1_000_000_000.0,
        prev_change_pct=-0.035,
    )
    sig = evaluate_auction_stock(item)
    assert "弱转强" in sig.tags
    assert sig.intensity >= 80.0


def test_nuclear_drop_and_strong_to_weak_detected() -> None:
    # 核按钮跌停开
    item_nuclear = AuctionItem(
        symbol="600100.SH",
        name="核按钮",
        open_price=9.1,
        pre_close=10.0,
        auction_amount=10_000_000.0,
        prev_amount=500_000_000.0,
    )
    sig_nuclear = evaluate_auction_stock(item_nuclear)
    assert "一字核按钮" in sig_nuclear.tags
    assert sig_nuclear.intensity <= 10.0

    # 昨天涨停，今天低开 -2.5% 被抢跑
    item_run = AuctionItem(
        symbol="600200.SH",
        name="昨涨停今低开",
        open_price=9.75,
        pre_close=10.0,
        auction_amount=10_000_000.0,
        prev_amount=500_000_000.0,
        prev_limit_up=True,
        prev_streak=2,
    )
    sig_run = evaluate_auction_stock(item_run)
    assert "强转弱" in sig_run.tags


def test_scan_call_auction_market_summary() -> None:
    items = [
        AuctionItem(
            symbol="000001.SZ",
            name="标的1",
            open_price=10.4,
            pre_close=10.0,
            auction_amount=50_000_000.0,
            prev_amount=1_000_000_000.0,
        ),
        AuctionItem(
            symbol="000002.SZ",
            name="标的2",
            open_price=9.0,
            pre_close=10.0,
            auction_amount=10_000_000.0,
            prev_amount=500_000_000.0,
        ),
    ]
    scan = scan_call_auction(items)
    assert len(scan.signals) == 2
    assert scan.rush_bidding_count == 1
    assert scan.nuclear_drop_count == 1
    top = scan.top_rush_stocks(top=1)
    assert len(top) == 1
    assert top[0].symbol == "000001.SZ"
