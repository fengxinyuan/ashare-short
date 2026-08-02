"""低吸特征、对照组、T+1 口径、持仓状态。

这四样是同一件事的四个面：**让明天开始采集的数据，两个月后还能回答问题**。
所以测的重点不是「函数跑通了」，而是「缺数据时是不是老实说不知道」「两组样本
是不是真的分得开」。
"""

from __future__ import annotations

from datetime import date

from ashare_decision_support.journal.records import (
    KIND_CONTROL,
    KIND_RECOMMENDATION,
    Recommendation,
)
from ashare_decision_support.journal.stats import summarize
from ashare_decision_support.journal.store import Journal
from ashare_decision_support.journal.verify import verify_pending
from ashare_decision_support.signals.dipbuy import measure_dip
from ashare_decision_support.state.watchlist import Watchlist


def bars(closes: list[float], volumes: list[float] | None = None) -> list[dict]:
    vols = volumes or [1000.0] * len(closes)
    return [
        {
            "date": f"2026-06-{index + 1:02d}",
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": vol,
        }
        for index, (close, vol) in enumerate(zip(closes, vols))
    ]


# ---- 低吸特征 -------------------------------------------------------------


def test_pullback_is_measured_from_the_20_day_high() -> None:
    # 前 20 根爬到 110，最后回落到 99 —— 相对高点回撤 10%。
    closes = [100.0] * 5 + [110.0] + [105.0, 103.0, 101.0, 99.0]
    features = measure_dip(bars(closes))

    assert features.pullback_pct is not None
    assert abs(features.pullback_pct - (99.0 / 110.0 - 1.0)) < 1e-6
    # 高点在倒数第 5 根，之后走了 4 天。
    assert features.down_days == 4


def test_pullback_bands_separate_no_dip_from_a_broken_trend() -> None:
    """三段划分要真的把「没调」「理想」「太深」分开——它是台账分层的第一刀。"""

    def band(last: float) -> str | None:
        return measure_dip(bars([100.0] * 19 + [last])).pullback_band

    assert band(99.5) == "none"      # -0.5%，根本没回调
    assert band(97.0) == "shallow"   # -3%
    assert band(92.0) == "ideal"     # -8%，低吸目标窗口
    assert band(80.0) == "deep"      # -20%，趋势是不是还在已经说不清


def test_a_rally_to_a_new_high_has_zero_pullback_not_a_positive_one() -> None:
    """创新高时回撤是 0，不能因为现价高于区间内某根高点而算出正数。"""
    features = measure_dip(bars([100.0, 101.0, 102.0, 103.0]))
    assert features.pullback_pct == 0.0
    assert features.pullback_band == "none"


def test_volume_contraction_tells_a_healthy_dip_from_distribution() -> None:
    """低吸最关键的一个区分：回调缩量是健康的，回调放量是出货。"""
    closes = [100.0] * 17 + [98.0, 96.0, 94.0]

    shrinking = measure_dip(bars(closes, [1000.0] * 17 + [400.0, 400.0, 400.0]))
    assert shrinking.volume_contraction is not None
    assert shrinking.volume_contraction < 1.0
    assert shrinking.shrinking is True

    expanding = measure_dip(bars(closes, [1000.0] * 17 + [3000.0, 3000.0, 3000.0]))
    assert expanding.shrinking is False


def test_missing_data_yields_none_not_zero() -> None:
    """「没算出来」和「算出来是零」在事后统计里必须能分开。"""
    features = measure_dip(bars([100.0, 101.0]))
    assert features.ret_20d is None
    assert features.volume_contraction is None
    assert features.above_ma20 is None
    # 且不抛异常——一只票算不出特征不该让整条盘前链路停下来。
    assert measure_dip([]).pullback_pct is None


def test_support_distance_only_counts_supports_below_the_price() -> None:
    class Level:
        def __init__(self, price: float) -> None:
            self.price = price

    class Snapshot:
        supports = [Level(90.0), Level(95.0), Level(120.0)]

    features = measure_dip(bars([100.0] * 20), Snapshot())
    # 最近的下方支撑是 95，不是 120（那是压力）也不是 90（更远）。
    assert features.dist_to_support_pct is not None
    assert abs(features.dist_to_support_pct - (100.0 / 95.0 - 1.0)) < 1e-6


# ---- 对照组 ---------------------------------------------------------------


def make_record(symbol: str, kind: str, tmp_path) -> Recommendation:
    return Recommendation(
        symbol=symbol,
        trade_date="2026-06-01",
        source="morning",
        reference_price=100.0,
        stop_pct=-0.05,
        target_pcts=[0.08],
        kind=kind,
        dip={"pullback_band": "ideal"},
        market_state={"blowup_rate": 0.52, "max_streak": 9},
    )


def test_control_records_are_verified_alongside_recommendations(tmp_path) -> None:
    """只核对推荐、不核对对照，等于把对照组记了个寂寞。"""
    book = Journal(root=tmp_path)
    book.record(make_record("600000.SH", KIND_RECOMMENDATION, tmp_path))
    book.record(make_record("600001.SH", KIND_CONTROL, tmp_path))

    pending = book.pending(date(2026, 6, 20))
    assert {row["symbol"] for row in pending} == {"600000.SH", "600001.SH"}


def test_outcome_remembers_which_group_it_came_from(tmp_path) -> None:
    book = Journal(root=tmp_path)
    book.record(make_record("600001.SH", KIND_CONTROL, tmp_path))

    rows = [
        {"date": f"2026-06-{day:02d}", "open": 100.0, "high": 104.0, "low": 98.0, "close": 102.0}
        for day in range(1, 12)
    ]

    class Adapter:
        def get_historical_bars(self, symbols, start, end):
            return {symbol: rows for symbol in symbols}

    result = verify_pending(book, Adapter(), today=date(2026, 6, 20))
    assert result["resolved"] == 1
    outcome = result["outcomes"][0]
    assert outcome["rec_kind"] == KIND_CONTROL
    # 特征与市场状态原样带到兑现记录上，统计时不必回头关联建议。
    assert outcome["dip"]["pullback_band"] == "ideal"
    assert outcome["market_state"]["max_streak"] == 9


def test_next_session_readings_are_recorded_separately_from_the_five_day_window(tmp_path) -> None:
    """持有期该多长还没定论，两套口径必须并存。"""
    book = Journal(root=tmp_path)
    book.record(make_record("600000.SH", KIND_RECOMMENDATION, tmp_path))

    # 参考价 100（06-01 收盘）。次日跳空高开到 103，冲 106，收 104。
    rows = [{"date": "2026-06-01", "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0}]
    rows.append({"date": "2026-06-02", "open": 103.0, "high": 106.0, "low": 102.0, "close": 104.0})
    rows += [
        {"date": f"2026-06-{day:02d}", "open": 104.0, "high": 105.0, "low": 103.0, "close": 104.0}
        for day in range(3, 10)
    ]

    class Adapter:
        def get_historical_bars(self, symbols, start, end):
            return {symbol: rows for symbol in symbols}

    outcome = verify_pending(book, Adapter(), today=date(2026, 6, 20))["outcomes"][0]

    # 隔夜跳空单独可见——它常常比日内择时贡献更大，混在 MFE 里就归因不出来。
    assert abs(outcome["next_open_pct"] - 0.03) < 1e-6
    assert abs(outcome["next_high_pct"] - 0.06) < 1e-6
    assert abs(outcome["next_close_pct"] - 0.04) < 1e-6
    # 五日窗口照旧存在，两者互不覆盖。
    assert outcome["mfe_pct"] >= outcome["next_high_pct"]


# ---- 持仓状态 -------------------------------------------------------------


def test_holding_state_drives_which_question_gets_asked(tmp_path) -> None:
    book = Watchlist(path=tmp_path / "watchlist.json")
    book.add("600519")
    book.add("000001.SZ")

    assert len(book.holdings()) == 0
    book.set_position("600519", cost_price=1300.0, shares=100, today=date(2026, 8, 3))

    assert [entry.symbol for entry in book.holdings()] == ["600519.SH"]
    assert [entry.symbol for entry in book.unheld()] == ["000001.SZ"]

    entry = book.get("600519")
    assert abs(entry.unrealised_pct(1352.0) - (1352.0 / 1300.0 - 1.0)) < 1e-6


def test_buying_a_stock_not_yet_watched_adds_it(tmp_path) -> None:
    """你买了它，它当然该被盯着——不该因为忘了先 add 就漏掉。"""
    book = Watchlist(path=tmp_path / "watchlist.json")
    book.set_position("600519", cost_price=1300.0)
    assert book.get("600519").holding is True


def test_closing_a_position_keeps_the_stock_on_the_watchlist(tmp_path) -> None:
    """卖掉不等于不再关注。"""
    book = Watchlist(path=tmp_path / "watchlist.json")
    book.set_position("600519", cost_price=1300.0)
    assert book.close_position("600519") is True

    entry = book.get("600519")
    assert entry is not None
    assert entry.holding is False
    assert entry.cost_price is None
    assert entry.unrealised_pct(1400.0) is None


def test_old_watchlist_files_load_as_unheld(tmp_path) -> None:
    """老 watchlist.json 没有持仓字段，读它不该炸也不该猜。"""
    path = tmp_path / "watchlist.json"
    path.write_text('[{"symbol": "600519.SH", "note": "旧格式"}]', encoding="utf-8")

    entry = Watchlist(path=path).get("600519")
    assert entry.holding is False
    assert entry.cost_price is None
    assert entry.note == "旧格式"


# ---- 统计分层 -------------------------------------------------------------


def outcome_row(rec_kind: str, close_pct: float, blowup: float | None = None, band: str = "ideal"):
    return {
        "kind": "outcome",
        "rec_kind": rec_kind,
        "mfe_pct": close_pct + 0.02,
        "mae_pct": -0.01,
        "close_pct": close_pct,
        "next_open_pct": close_pct / 2,
        "next_close_pct": close_pct,
        "entered": True,
        "dip": {"pullback_band": band},
        "market_state": {} if blowup is None else {"blowup_rate": blowup},
    }


def test_selection_edge_is_the_difference_not_the_level() -> None:
    """普涨的日子里随便选都赚——只有减掉对照组才看得出筛选有没有用。"""
    rows = [outcome_row("recommendation", 0.05) for _ in range(5)]
    rows += [outcome_row("control", 0.03) for _ in range(5)]

    edge = summarize(rows)["selection_edge"]
    assert edge["available"] is True
    assert abs(edge["close_gap"] - 0.02) < 1e-9
    # 每组 5 条远低于 MIN_BUCKET，差值照算但明确标注不可信。
    assert edge["trusted"] is False


def test_a_rule_that_underperforms_its_control_shows_a_negative_edge() -> None:
    """这是这套统计存在的全部意义：它必须能说出「规则是负价值」。"""
    rows = [outcome_row("recommendation", 0.01) for _ in range(3)]
    rows += [outcome_row("control", 0.04) for _ in range(3)]

    assert summarize(rows)["selection_edge"]["close_gap"] < 0


def test_edge_is_unavailable_rather_than_zero_when_one_group_is_missing() -> None:
    rows = [outcome_row("recommendation", 0.05) for _ in range(5)]
    edge = summarize(rows)["selection_edge"]
    assert edge["available"] is False


def test_regime_buckets_separate_a_healthy_tape_from_a_fading_one() -> None:
    """同一套规则在退潮期和启动期的期望值可以是反号的。"""
    rows = [outcome_row("recommendation", 0.05, blowup=0.15) for _ in range(3)]
    rows += [outcome_row("recommendation", -0.03, blowup=0.62) for _ in range(3)]
    rows += [outcome_row("recommendation", 0.01) for _ in range(2)]  # 没记市场状态

    buckets = {item["bucket"]: item for item in summarize(rows)["by_regime"]}
    assert len(buckets) == 3
    assert buckets["未记录"]["samples"] == 2
    healthy = next(name for name in buckets if name.startswith("封板结实"))
    fading = next(name for name in buckets if name.startswith("退潮"))
    assert buckets[healthy]["median_close_pct"] > 0
    assert buckets[fading]["median_close_pct"] < 0


def test_pullback_bands_become_a_statistical_layer() -> None:
    """「回撤 5% 买还是 8% 买」是低吸的核心问题，只有这一组能回答。"""
    rows = [outcome_row("recommendation", 0.06, band="ideal") for _ in range(3)]
    rows += [outcome_row("recommendation", -0.02, band="deep") for _ in range(3)]

    buckets = {item["bucket"]: item for item in summarize(rows)["by_pullback"]}
    assert buckets["ideal"]["median_close_pct"] > buckets["deep"]["median_close_pct"]
