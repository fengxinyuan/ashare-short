"""龙虎榜席位：把「净买 3 亿」拆成「谁买的」。

不打外网——`FakeAk` 复刻 2026-08-04 实测到的真实字段布局（中国宝安 000009
20260731 那一条），包括同一席位同时出现在买入榜和卖出榜这个必须去重的坑。
"""

from __future__ import annotations

from ashare_decision_support.adapters.lhb_seats import (
    SEAT_BRANCH,
    SEAT_INSTITUTION,
    SEAT_NORTHBOUND,
    LhbSeatAdapter,
    classify_seat,
)

HOT_SEAT = "国信证券股份有限公司深圳泰然九路证券营业部"

# 字段名与实测一致，勿改：这是 stock_lhb_stock_detail_em 的真实布局。
BUY_ROWS = [
    {
        "交易营业部名称": HOT_SEAT,
        "买入金额": 326588076.76,
        "买入金额-占总成交比例": 0.218364,
        "卖出金额": 15125521.0,
        "卖出金额-占总成交比例": 0.010113,
        "净额": 311462555.76,
    },
    {
        "交易营业部名称": "深股通专用",
        "买入金额": 81887112.92,
        "买入金额-占总成交比例": 0.054751,
        "卖出金额": 77063527.0,
        "卖出金额-占总成交比例": 0.051526,
        "净额": 4823585.92,
    },
    {
        "交易营业部名称": "机构专用",
        "买入金额": 29243373.62,
        "买入金额-占总成交比例": 0.019553,
        "卖出金额": 20610544.0,
        "卖出金额-占总成交比例": 0.013781,
        "净额": 8632829.62,
    },
]
SELL_ROWS = [
    # 深股通与那个游资席位在两张榜上重复出现——真实数据就是这样。
    {
        "交易营业部名称": "深股通专用",
        "买入金额": 81887112.92,
        "买入金额-占总成交比例": 0.054751,
        "卖出金额": 77063527.0,
        "卖出金额-占总成交比例": 0.051526,
        "净额": 4823585.92,
    },
    {
        "交易营业部名称": HOT_SEAT,
        "买入金额": 326588076.76,
        "买入金额-占总成交比例": 0.218364,
        "卖出金额": 15125521.0,
        "卖出金额-占总成交比例": 0.010113,
        "净额": 311462555.76,
    },
    {
        "交易营业部名称": "机构专用",
        "买入金额": 11602113.12,
        "买入金额-占总成交比例": 0.007757,
        "卖出金额": 15241440.66,
        "卖出金额-占总成交比例": 0.010191,
        "净额": -3639327.54,
    },
]


class FakeAk:
    def __init__(self, buy=None, sell=None, raises: set[str] | None = None) -> None:
        self.buy = BUY_ROWS if buy is None else buy
        self.sell = SELL_ROWS if sell is None else sell
        self.raises = raises or set()
        self.calls: list[tuple[str, str, str]] = []

    def stock_lhb_stock_detail_em(self, symbol, date, flag):
        self.calls.append((symbol, date, flag))
        if flag in self.raises:
            raise ConnectionError("feed down")
        return list(self.buy if flag == "买入" else self.sell)


# 席位分类


def test_seat_categories_come_from_the_exchange_wording() -> None:
    """前三类是交易所的固定称谓，判定是查名不是猜。"""
    assert classify_seat("机构专用") == (SEAT_INSTITUTION, False)
    assert classify_seat("深股通专用") == (SEAT_NORTHBOUND, False)
    assert classify_seat("沪股通专用") == (SEAT_NORTHBOUND, False)
    assert classify_seat(HOT_SEAT) == (SEAT_BRANCH, True)
    assert classify_seat("某某证券某某营业部") == (SEAT_BRANCH, False)


def test_blank_seat_name_falls_back_without_guessing() -> None:
    assert classify_seat("") == (SEAT_BRANCH, False)
    assert classify_seat(None) == (SEAT_BRANCH, False)


# 取数与聚合


def test_a_seat_on_both_boards_is_counted_once() -> None:
    """同一席位同时出现在买入榜与卖出榜，两边字段完全一致。

    不去重的话它的净额会被算两遍——3.11 亿会变成 6.22 亿。
    """
    read = LhbSeatAdapter(ak_module=FakeAk()).fetch("000009.SZ", "20260731")

    assert read.covered is True
    assert len(read.seats) == 4  # 游资、深股通、机构(+8.6M)、机构(-3.6M)
    assert read.net_by_category[SEAT_BRANCH] == 311462555.76


def test_categories_aggregate_separately() -> None:
    """两个「机构专用」是两家机构，净额相加；和游资、北向分开算。"""
    read = LhbSeatAdapter(ak_module=FakeAk()).fetch("000009.SZ", "20260731")

    assert round(read.net_by_category[SEAT_INSTITUTION], 2) == round(8632829.62 - 3639327.54, 2)
    assert read.net_by_category[SEAT_NORTHBOUND] == 4823585.92


def test_top_buyer_is_flagged_when_it_is_a_known_hot_money_seat() -> None:
    read = LhbSeatAdapter(ak_module=FakeAk()).fetch("000009.SZ", "20260731")

    assert read.top_buyer is not None
    assert read.top_buyer.name == HOT_SEAT
    assert read.top_buyer.known_hot_money is True
    body = " ".join(read.describe())
    assert "知名游资席位" in body


def test_top_seller_is_none_when_every_seat_is_net_long() -> None:
    """净额最小的那个仍然为正时，没有「卖方居首」这回事。"""
    read = LhbSeatAdapter(ak_module=FakeAk(buy=list(BUY_ROWS), sell=[])).fetch(
        "000009.SZ", "20260731"
    )

    assert read.top_buyer is not None
    assert read.top_seller is None


def test_concentration_uses_the_session_amount_when_given() -> None:
    read = LhbSeatAdapter(ak_module=FakeAk()).fetch("000009.SZ", "20260731", market_amount=1.4956e9)

    assert read.concentration is not None
    assert 0.20 <= read.concentration <= 0.21
    assert "少数席位主导" in " ".join(read.describe())


def test_concentration_falls_back_to_the_server_share_and_says_so() -> None:
    read = LhbSeatAdapter(ak_module=FakeAk()).fetch("000009.SZ", "20260731")

    assert read.concentration is not None
    assert any("占总成交比例" in note for note in read.notes)


# 弃权路径


def test_a_stock_that_never_made_the_board_is_not_an_error() -> None:
    """牧原这种流通市值 1248 亿的票够不到上榜门槛，取不到是常态不是故障。"""
    read = LhbSeatAdapter(ak_module=FakeAk(buy=[], sell=[])).fetch("002714.SZ", "20260804")

    assert read.covered is False
    assert read.seats == []
    assert "未上榜" in " ".join(read.describe())


def test_one_side_failing_still_yields_the_other_with_a_note() -> None:
    read = LhbSeatAdapter(ak_module=FakeAk(raises={"卖出"})).fetch("000009.SZ", "20260731")

    assert read.covered is True
    assert any("取数失败" in note for note in read.notes)


def test_missing_akshare_abstains_rather_than_raising() -> None:
    read = LhbSeatAdapter(ak_module=object()).fetch("000009.SZ", "20260731")

    assert read.covered is False
    assert read.reason is not None


def test_the_read_never_claims_hot_money_direction_predicts() -> None:
    """席位是事实，「跟着游资做」是判断。简报文案里必须有这条界线。"""
    read = LhbSeatAdapter(ak_module=FakeAk()).fetch("000009.SZ", "20260731")

    assert any("未经本系统验证" in line for line in read.describe())
