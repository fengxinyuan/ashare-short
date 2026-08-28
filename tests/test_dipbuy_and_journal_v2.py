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
from ashare_decision_support.journal.stats import rolling_validation, summarize
from ashare_decision_support.journal.store import Journal
from ashare_decision_support.journal.verify import verify_pending
from ashare_decision_support.signals.dipbuy import measure_dip
from ashare_decision_support.state.positions import Positions


def bars(
    closes: list[float],
    volumes: list[float] | None = None,
    amplitude: float = 0.02,
) -> list[dict]:
    """构造日线。`amplitude` 是每根的日内振幅，用来控制 ATR ——
    回调档位现在按「几个日常波动」判定，所以振幅必须能被测试指定。"""
    vols = volumes or [1000.0] * len(closes)
    return [
        {
            "date": f"2026-06-{index + 1:02d}",
            "open": close,
            "high": close * (1 + amplitude / 2),
            "low": close * (1 - amplitude / 2),
            "close": close,
            "volume": vol,
            "amount": close * vol,
        }
        for index, (close, vol) in enumerate(zip(closes, vols, strict=True))
    ]


# ---- 低吸特征 -------------------------------------------------------------


def test_pullback_is_measured_from_the_20_day_high() -> None:
    # 前 20 根爬到 110，最后回落到 99 —— 相对高点回撤 10%。
    closes = [100.0] * 5 + [110.0] + [105.0, 103.0, 101.0, 99.0]
    features = measure_dip(bars(closes))

    assert features.pullback_pct is not None
    # 高点用的是 bar 的 high（110 × 1.01），不是收盘价。
    assert -0.12 < features.pullback_pct < -0.09
    # 高点在倒数第 5 根，之后走了 4 天。
    assert features.down_days == 4


def test_pullback_bands_are_measured_in_the_stock_s_own_volatility() -> None:
    """同样跌一截，低波票是深度回调，高波票只是日常噪声。

    这是 2026-08-02 实测出来的问题：用固定百分比分档，长江电力（日振幅 1.82%）
    一年里 `ideal` 只占 8%、`deep` 一次没到过；而计算机题材股（日振幅 6.23%）
    49% 的交易日待在 `deep`——那个本该报警的档位成了它的常态。
    """
    closes = [100.0] * 19 + [94.0]  # 名义上一律跌 6%

    calm = measure_dip(bars(closes, amplitude=0.01))     # 日振幅 1%
    wild = measure_dip(bars(closes, amplitude=0.15))     # 日振幅 15%

    # 名义跌幅接近（夹具的 high 随振幅略有差异），但归一化深度差近 6 倍。
    assert calm.pullback_atr > wild.pullback_atr * 4
    # 低波票该报警，高波票这点回撤还没超出日常波动。
    assert calm.pullback_band == "deep"
    assert wild.pullback_band in ("none", "shallow")


def test_bands_cover_the_full_range_as_normalised_depth_grows() -> None:
    def band(drop: float) -> str | None:
        return measure_dip(bars([100.0] * 19 + [100.0 * (1 - drop)], amplitude=0.02)).pullback_band

    # 日振幅 2%，所以档位边界约在 1.6% / 4% / 8% 的回撤上。
    assert band(0.005) == "none"
    assert band(0.03) == "shallow"
    assert band(0.06) == "ideal"
    assert band(0.20) == "deep"


def test_true_range_counts_gaps_not_just_the_intraday_span() -> None:
    """一字板和跳空开盘时日内区间几乎为零，只看 high-low 会把大跳空读成没波动。"""
    rows = [
        {"date": "2026-06-01", "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
         "volume": 1000.0},
    ]
    # 每根都平走，但每天跳空 5%。
    price = 100.0
    for day in range(2, 22):
        price *= 1.05
        rows.append({"date": f"2026-06-{day:02d}", "open": price, "high": price,
                     "low": price, "close": price, "volume": 1000.0})

    features = measure_dip(rows)
    assert features.atr_pct is not None
    # 日内区间恒为 0，但真实波幅认出了跳空。
    assert features.atr_pct > 0.03


def test_a_rally_to_a_new_high_has_zero_pullback_not_a_positive_one() -> None:
    """创新高时回撤是 0，不能因为现价高于区间内某根高点而算出正数。"""
    features = measure_dip(bars([100.0, 101.0, 102.0, 103.0]))
    assert features.pullback_pct <= 0.0
    assert features.pullback_band in (None, "none")


def test_volume_contraction_tells_a_healthy_dip_from_distribution() -> None:
    """低吸最关键的一个区分：回调缩量是健康的，回调放量是出货。"""
    closes = [100.0] * 17 + [98.0, 96.0, 94.0]

    shrinking = measure_dip(bars(closes, [1000.0] * 17 + [400.0, 400.0, 400.0]))
    assert shrinking.volume_contraction is not None
    assert shrinking.volume_contraction < 1.0
    assert shrinking.shrinking is True

    expanding = measure_dip(bars(closes, [1000.0] * 17 + [3000.0, 3000.0, 3000.0]))
    assert expanding.shrinking is False


def test_turnover_cost_handles_shares_and_tickflow_lots() -> None:
    """成本代理不能因上游把成交量写成手而放大 100 倍。"""
    shares_rows = bars([100.0] * 30, [1000.0] * 30)
    lots_rows = [
        {**row, "volume": row["volume"] / 100.0, "amount": row["amount"]}
        for row in shares_rows
    ]

    shares = measure_dip(shares_rows)
    lots = measure_dip(lots_rows)

    assert shares.turnover_cost_15 == 100.0
    assert shares.turnover_cost_30 == 100.0
    assert lots.turnover_cost_15 == 100.0
    assert lots.turnover_cost_30 == 100.0


def test_cost_proxy_marks_a_down_day_that_reclaims_the_30_day_cost() -> None:
    rows = bars([100.0] * 28 + [101.0, 100.5], [1000.0] * 30)
    rows[-1]["low"] = 99.0

    features = measure_dip(rows)

    assert features.ret_1d is not None and features.ret_1d < 0
    assert features.turnover_cost_30 is not None
    assert features.turnover_cost_30_reclaimed is True
    assert features.dist_to_turnover_cost_30_pct is not None
    assert features.dist_to_turnover_cost_30_pct > 0


def test_cost_proxy_is_unknown_without_amount() -> None:
    rows = bars([100.0] * 30)
    for row in rows:
        row.pop("amount")

    features = measure_dip(rows)

    assert features.turnover_cost_15 is None
    assert features.turnover_cost_30 is None
    assert features.turnover_cost_30_reclaimed is None


def test_cost_proxy_enters_the_score_as_a_support_reading() -> None:
    from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput

    rows = bars([100.0] * 28 + [101.0, 100.5], [1000.0] * 30)
    rows[-1]["low"] = 99.0
    features = measure_dip(rows)

    result = RuleBasedScoreProvider().score(
        ScoreInput(symbol="A", bars=rows, snapshot=snap(), dip=features)
    )

    assert any("当日下跌踩回30日成交成本代理" in reason for reason in result.reasons)


def test_missing_data_yields_none_not_zero() -> None:
    """「没算出来」和「算出来是零」在事后统计里必须能分开。"""
    features = measure_dip(bars([100.0, 101.0]))
    assert features.ret_20d is None
    assert features.volume_contraction is None
    assert features.above_ma20 is None
    # 且不抛异常——一只票算不出特征不该让整条盘前链路停下来。
    assert measure_dip([]).pullback_pct is None


def test_a_flat_line_has_no_band_rather_than_a_fake_one() -> None:
    """振幅为零时分母是零，不该拿绝对值凑一个档位出来。"""
    rows = [
        {"date": f"2026-06-{i + 1:02d}", "open": 100.0, "high": 100.0, "low": 100.0,
         "close": 100.0, "volume": 1000.0}
        for i in range(25)
    ]
    features = measure_dip(rows)
    assert features.atr_pct in (None, 0.0)
    assert features.pullback_atr is None
    assert features.pullback_band is None


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
            return dict.fromkeys(symbols, rows)

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
            return dict.fromkeys(symbols, rows)

    outcome = verify_pending(book, Adapter(), today=date(2026, 6, 20))["outcomes"][0]

    # 隔夜跳空单独可见——它常常比日内择时贡献更大，混在 MFE 里就归因不出来。
    assert abs(outcome["next_open_pct"] - 0.03) < 1e-6
    assert abs(outcome["next_high_pct"] - 0.06) < 1e-6
    assert abs(outcome["next_close_pct"] - 0.04) < 1e-6
    # 五日窗口照旧存在，两者互不覆盖。
    assert outcome["mfe_pct"] >= outcome["next_high_pct"]


def test_verification_accepts_normalized_timestamp_bars(tmp_path) -> None:
    """适配器标准输出是 timestamp，不应因为没有 date 而丢掉兑现结果。"""
    book = Journal(root=tmp_path)
    book.record(make_record("600000.SH", KIND_RECOMMENDATION, tmp_path))
    rows = [
        {
            "timestamp": f"2026-06-{day:02d}T15:00:00",
            "open": 100.0,
            "high": 104.0,
            "low": 98.0,
            "close": 100.0 if day == 1 else 102.0,
        }
        for day in range(1, 10)
    ]

    class Adapter:
        def get_historical_bars(self, symbols, start, end):
            return {symbol: list(reversed(rows)) for symbol in symbols}

    result = verify_pending(book, Adapter(), today=date(2026, 6, 20))

    assert result["resolved"] == 1
    assert result["outcomes"][0]["next_close_pct"] == 0.02


# ---- 持仓表 ---------------------------------------------------------------


def test_opening_a_position_makes_it_the_thing_being_watched(tmp_path) -> None:
    book = Positions(path=tmp_path / "positions.json")
    assert len(book) == 0

    book.open("600519", cost_price=1300.0, shares=100, today=date(2026, 8, 3))

    assert book.symbols() == ["600519.SH"]
    entry = book.get("600519")
    assert entry.opened_on == "2026-08-03"
    assert abs(entry.unrealised_pct(1352.0) - (1352.0 / 1300.0 - 1.0)) < 1e-6


def test_reopening_the_same_symbol_updates_in_place(tmp_path) -> None:
    """改仓不该变成第二条记录——两条同代码的持仓会让卖点判断按哪一条都不对。"""
    book = Positions(path=tmp_path / "positions.json")
    book.open("600519", cost_price=1300.0, plan_stop=1280.0)
    book.open("600519", cost_price=1310.0)

    assert len(book) == 1
    entry = book.get("600519")
    assert entry.cost_price == 1310.0
    # 不带 --stop 的那次改仓不该抹掉上次填的止损。
    assert entry.plan_stop == 1280.0


def test_closing_a_position_removes_it(tmp_path) -> None:
    """平仓即删除：卖点对一只已经卖掉的票没有意义，历史留在台账里。"""
    path = tmp_path / "positions.json"
    book = Positions(path=path)
    book.open("600519", cost_price=1300.0)

    assert book.close("600519") is True
    assert book.close("600519") is False
    assert Positions(path=path).symbols() == []


def test_a_hand_edited_file_without_cost_still_loads(tmp_path) -> None:
    """少一个数应该降级成「只报价位」，而不是让盘中监控起不来。"""
    path = tmp_path / "positions.json"
    path.write_text('[{"symbol": "600519.SH", "note": "手工加的"}]', encoding="utf-8")

    entry = Positions(path=path).get("600519")
    assert entry.cost_price is None
    assert entry.unrealised_pct(1400.0) is None
    assert entry.note == "手工加的"


# ---- 统计分层 -------------------------------------------------------------


def outcome_row(rec_kind: str, close_pct: float, blowup: float | None = None, band: str = "ideal"):
    return {
        "kind": "outcome",
        "trade_date": "2026-08-01",
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


def test_a_nan_reading_is_dropped_instead_of_silently_shifting_the_median() -> None:
    """NaN 不会让 median 报错，它会让 median 悄悄给错数。

    台账是 JSONL，`json.dumps(nan)` 写出裸 `NaN`、`json.loads` 原样读回来，所以
    一个 NaN 收盘涨幅是能一路进到这里的。它参与排序时所有比较都是 False，于是
    它落在哪、把谁挤到中位，取决于原始顺序——统计表会整张偏掉且不报任何错。
    """
    clean = [0.1, 0.9, 0.2, 0.3]
    rows = [outcome_row("recommendation", value) for value in clean]
    rows.insert(1, outcome_row("recommendation", float("nan")))

    stats = summarize(rows)["overall"]
    # 去掉 NaN 之后的真中位数是 0.25。把 NaN 留在列里，statistics.median 会给 0.2。
    assert stats["median_close_pct"] == 0.25
    assert stats["samples"] == 5


def test_a_boolean_is_not_counted_as_a_one_percent_gain() -> None:
    """`isinstance(True, (int, float))` 是 True——写错类型的字段会变成 +100%。"""
    rows = [outcome_row("recommendation", 0.05) for _ in range(2)]
    rows[0]["close_pct"] = True

    # 留着 True 的话中位数是 (1.0 + 0.05) / 2 = 0.525。
    assert summarize(rows)["overall"]["median_close_pct"] == 0.05


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


# ---- 打分口径转向低吸 -------------------------------------------------------


def snap(**kw):
    from datetime import datetime

    from ashare_decision_support.models.enums import TrendState
    from ashare_decision_support.models.schemas import DailyTechnicalSnapshot

    d = {
        "symbol": "600519.SH", "last_open": 99.0, "last_high": 101.0, "last_low": 98.0,
        "last_close": 100.0, "trend_state": TrendState.UPTREND, "bar_count": 250,
        "atr14": 2.0, "volume_ratio_20d": 1.0, "range_position_60d": 0.5,
        "stop_reference": 95.0, "target_reference": 115.0,
        "explanation": "fixture", "source_ts": datetime(2026, 8, 3, 15, 0),
    }
    d.update(kw)
    return DailyTechnicalSnapshot(**d)


def dip(**kw):
    from ashare_decision_support.signals.dipbuy import DipFeatures
    d = {"pullback_pct": -0.06, "atr_pct": 0.02, "pullback_atr": 3.0,
             "pullback_band": "ideal", "shrinking": True, "volume_contraction": 0.5}
    d.update(kw)
    return DipFeatures(**d)


def score(**kw):
    from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput
    kw.setdefault("snapshot", snap())
    return RuleBasedScoreProvider().score(ScoreInput(symbol="A", bars=[], **kw))


def test_a_stock_that_has_pulled_back_now_outranks_one_that_has_not() -> None:
    """这是整次口径转向要成立的那一条：低吸要的是回调到位的票，不是正在涨的票。

    2026-08-02 实测转向前的 30 只候选，落在理想窗口的 0 只、缩量 0 只——
    打分一直在选主升段。
    """
    pulled_back = score(dip=dip())
    at_the_high = score(dip=dip(pullback_pct=-0.002, pullback_atr=0.1, pullback_band="none",
                                shrinking=False, volume_contraction=1.1))
    assert pulled_back.score > at_the_high.score


def test_a_pullback_too_deep_is_a_risk_not_an_opportunity() -> None:
    """回调和趋势走坏分不开时，低吸就变成了接刀。"""
    ideal = score(dip=dip())
    knife = score(dip=dip(pullback_pct=-0.25, pullback_atr=9.0, pullback_band="deep"))

    assert knife.score < ideal.score
    assert any("接刀" in w for w in knife.warnings)


def test_volume_flips_meaning_between_a_pullback_and_a_breakout() -> None:
    """同一个量比，回调段要缩量（抛压有限），未回调段要放量（启动确认）。

    转向前这里只有一个静态阈值：`ratio >= 1.0` 一律 +8、缩量 -5，对低吸恰好选反。
    """
    quiet_dip = score(snapshot=snap(volume_ratio_20d=0.5), dip=dip())
    loud_dip = score(snapshot=snap(volume_ratio_20d=2.5), dip=dip())
    assert quiet_dip.score > loud_dip.score
    assert any("疑似出货" in r for r in loud_dip.reasons)

    no_dip = {"pullback_band": "none", "pullback_atr": 0.2, "pullback_pct": -0.004}
    quiet_flat = score(snapshot=snap(volume_ratio_20d=0.5), dip=dip(**no_dip))
    loud_flat = score(snapshot=snap(volume_ratio_20d=1.8), dip=dip(**no_dip))
    assert loud_flat.score > quiet_flat.score


def test_wyckoff_pullback_events_now_outweigh_the_breakout_event() -> None:
    """Spring / LPS 是低吸信号，SOS 是「已经涨上去了」。"""
    from ashare_decision_support.scoring.rule_based import (
        LPS_POINTS,
        SOS_POINTS,
        SPRING_POINTS,
    )
    assert SPRING_POINTS > SOS_POINTS
    assert LPS_POINTS > SOS_POINTS


def test_a_hair_thin_stop_can_no_longer_earn_a_full_risk_reward_score() -> None:
    """止损贴着现价时盈亏比会算出天文数字。

    2026-08-02 实测 603508.SH：现价 23.16、止损 23.15，距离 0.04%，
    盈亏比 172.84 直接拿满分。
    """
    absurd = score(snapshot=snap(stop_reference=99.96, target_reference=115.0))
    sane = score(snapshot=snap(stop_reference=95.0, target_reference=115.0))

    assert absurd.score <= sane.score
    assert any("止损距离" in w for w in absurd.warnings)


def test_the_stop_itself_keeps_a_minimum_distance_from_the_price() -> None:
    """源头也要修——简报里报出一个 0.04% 的止损位本身就是误导。"""
    from datetime import timedelta

    from ashare_decision_support.signals.daily_technical import DailyTechnicalAnalyzer

    start = date(2026, 3, 2)
    rows = [
        {"date": str(start + timedelta(days=i)), "open": 100.0, "high": 101.0,
         "low": 99.0, "close": 100.0, "volume": 1000.0}
        for i in range(70)
    ]
    s = DailyTechnicalAnalyzer().analyze("600000.SH", rows)
    distance = 1.0 - s.stop_reference / s.last_close
    assert distance >= 0.005          # 至少不是零距离
    assert s.stop_reference < s.last_close


def test_scoring_ramps_instead_of_jumping_at_a_threshold() -> None:
    """阶跃在阈值两侧给出差很多的分，而阈值本身是拍脑袋定的。

    项目在大盘层（sectors/predict.py）早就否定过阶跃，这里是补上个股层。
    """
    at = score(snapshot=snap(volume_ratio_20d=3.99), dip=dip(pullback_band="none"))
    over = score(snapshot=snap(volume_ratio_20d=4.01), dip=dip(pullback_band="none"))
    # 转向前 4.0 处有 13 分悬崖（+8 → -5）。
    assert abs(at.score - over.score) < 1.0


def test_a_missing_dip_reading_scores_zero_rather_than_penalising() -> None:
    """dip=None 意思是「没算」，不是「没回调」。"""
    absent = score()
    assert not any("回调" in r for r in absent.reasons)
    assert 0.0 <= absent.score <= 100.0


def test_a_stock_at_its_high_is_not_a_dip_candidate_regardless_of_score() -> None:
    """打法门槛是**门槛不是权重**。

    dip 在打分里只占 10/85，trend + wyckoff 占 50，靠加权永远压不过「趋势强、
    位置高」的票。2026-08-02 实测：全市场随机 278 只里 27% 在 none 档、29% 在
    ideal 档，而打分选出的 15 只有 10 只是 none——分布差这么远，说明问题出在
    「要不要它」而不是「给它几分」。

    门槛现在按打法分（`strategy/playbook.py`）：没回调的票进不了「回踩」，
    但在 UPTREND 且情绪周期允许时可以走「突破」——这是改造前不存在的那条路。
    """
    from ashare_decision_support.models.enums import Playbook
    from ashare_decision_support.signals.dipbuy import DipFeatures
    from ashare_decision_support.strategy.playbook import evaluate_playbooks

    at_high = DipFeatures(pullback_band="none", volume_contraction=1.4, dist_to_ma20_pct=0.08)
    snapshot = snap(range_position_60d=0.97, volume_ratio_20d=1.6)

    match = evaluate_playbooks(snapshot, at_high, None, frozenset({Playbook.PULLBACK}))
    assert not match.ok, "贴在 20 日高的票不能进回踩候选"

    # 同一只票在允许突破的周期里走另一条判据链，并且命中。
    both = evaluate_playbooks(snapshot, at_high, None, None)
    assert both.primary is Playbook.BREAKOUT


def test_sector_ranking_no_longer_rewards_having_already_run() -> None:
    """板块层此前 `ret_5d * 120` 单调递增，涨得越多分越高。

    2026-08-02 实测：前五名板块的 5 日中位是 +10.16%/+10.48%/+9.48%/+8.03%/
    +4.80%，排序几乎就是已涨幅度倒序——那是追涨的排法，不是低吸的。
    """
    from ashare_decision_support.sectors.base import SectorMetrics
    from ashare_decision_support.sectors.predict import SectorPredictor

    predictor = SectorPredictor(None)

    def momentum(**kw):
        d = {"name": "X", "constituents": 50, "covered": 50, "ret_1d": 0.005, "ret_5d": 0.03,
                 "ret_20d": 0.08, "breadth": 0.6, "above_ma20": 0.7, "volume_ratio": 1.0}
        d.update(kw)
        return predictor._momentum_component(SectorMetrics(**d), [], [])

    # 中期向上但已回调 > 中期向上却贴在高点：低吸要的是前者。
    pulled_back = momentum(pullback_atr=2.0, pullback=-0.05)
    at_high = momentum(pullback_atr=0.1, pullback=-0.002)
    assert pulled_back > at_high

    # 回撤过深转为扣分。
    assert momentum(pullback_atr=6.0, pullback=-0.18) < pulled_back

    # 中期趋势仍然是前提：跌了 20 日的板块不该因为「回调到位」而被选中。
    assert momentum(ret_20d=-0.15, pullback_atr=2.0) < pulled_back


# ---- 只出读数那几层的验收位 -----------------------------------------------


def state_row(close_pct: float, **market_state):
    """带自定义 `market_state` 的兑现记录。"""
    row = outcome_row("recommendation", close_pct)
    row["market_state"] = dict(market_state)
    return row


def test_unscored_layers_get_their_own_buckets() -> None:
    """三层「只出读数」的必须各有一组分层统计。

    没有这三组的话，它们永远停在读数状态：`review` 回答不了「这一层报出来的东西
    有没有区分度」，于是也永远谈不上该给权重还是该撤掉。
    """
    rows = [
        state_row(0.05, rotation_adjacent="高低切换", turnover_total_change=-0.14,
                  index_divergence="等权强加权弱")
        for _ in range(3)
    ]
    rows += [
        state_row(0.01, rotation_adjacent="强者恒强", turnover_total_change=0.08,
                  index_divergence="等权与加权一致")
        for _ in range(3)
    ]

    stats = summarize(rows)

    rotation = {item["bucket"]: item for item in stats["by_rotation"]}
    assert set(rotation) == {"高低切换", "强者恒强"}
    assert rotation["高低切换"]["samples"] == 3

    turnover = {item["bucket"]: item for item in stats["by_turnover"]}
    assert any("缩量" in name for name in turnover)
    assert any("放量" in name for name in turnover)

    index = {item["bucket"]: item for item in stats["by_index_divergence"]}
    assert set(index) == {"等权强加权弱", "等权与加权一致"}


def test_old_records_land_in_unrecorded_instead_of_a_real_bucket() -> None:
    """这一层上线之前写的记录没有这些字段，必须落进「未记录」。

    把它们混进任何一个真实档位，都是**用一段从没测过的历史去支持一个结论**——
    而台账里那批待兑现记录恰好全是这种，数量还远多于新记录。
    """
    rows = [outcome_row("recommendation", 0.03) for _ in range(4)]

    stats = summarize(rows)

    assert [item["bucket"] for item in stats["by_rotation"]] == ["未记录"]
    assert [item["bucket"] for item in stats["by_turnover"]] == ["未记录"]
    assert [item["bucket"] for item in stats["by_index_divergence"]] == ["未记录"]


def test_macro_buckets_distinguish_recorded_empty_from_unrecorded() -> None:
    rows = [
        {
            **outcome_row("recommendation", 0.05),
            "trade_date": "2026-08-01",
            "macro_context": {
                "recorded": True,
                "topics": ["oil"],
                "signals": ["oil:bullish"],
                "expectations": ["oil:beat"],
            },
        },
        {
            **outcome_row("recommendation", 0.01),
            "trade_date": "2026-08-02",
            "macro_context": {"recorded": True, "topics": [], "signals": [], "expectations": []},
        },
        {**outcome_row("recommendation", -0.01), "trade_date": "2026-08-03"},
    ]

    stats = summarize(rows)

    topic = {item["bucket"]: item for item in stats["by_macro_topic"]}
    assert topic["oil"]["samples"] == 1
    assert topic["无明确宏观链路"]["samples"] == 1
    assert topic["未记录"]["samples"] == 1

    signal = {item["bucket"]: item for item in stats["by_macro_signal"]}
    assert signal["oil:bullish"]["samples"] == 1
    assert signal["无方向信号"]["samples"] == 1
    assert signal["未记录"]["samples"] == 1

    expectation = {item["bucket"]: item for item in stats["by_macro_expectation"]}
    assert expectation["oil:beat"]["samples"] == 1
    assert expectation["无预期差"]["samples"] == 1
    assert expectation["未记录"]["samples"] == 1


def test_rolling_validation_uses_distinct_trade_sessions_not_last_rows() -> None:
    rows: list[dict] = []
    for day, close_pct in (
        ("2026-08-01", 0.01),
        ("2026-08-02", 0.03),
        ("2026-08-03", -0.02),
    ):
        rows.extend(
            [
                {**outcome_row("recommendation", close_pct), "trade_date": day},
                {**outcome_row("control", close_pct / 2), "trade_date": day},
            ]
        )

    validation = rolling_validation(rows, windows=(2,))
    window = validation["windows"]["last_2_sessions"]

    assert validation["available_sessions"] == 3
    assert window["from"] == "2026-08-02"
    assert window["to"] == "2026-08-03"
    assert window["sessions"] == 2
    assert window["samples"] == 4
    assert window["selection_edge"]["available"] is True
    assert window["selection_edge"]["next_close_gap"] == 0.0025


def test_outcome_carries_macro_context_from_the_recommendation(tmp_path) -> None:
    book = Journal(root=tmp_path)
    record = make_record("600002.SH", KIND_RECOMMENDATION, tmp_path)
    record = Recommendation(
        **{
            **record.to_dict(),
            "macro_context": {
                "recorded": True,
                "topics": ["china_macro"],
                "signals": ["china_macro:bullish"],
                "expectations": ["china_macro:beat"],
            },
        }
    )
    book.record(record)

    rows = [
        {"date": f"2026-06-{day:02d}", "open": 100.0, "high": 104.0, "low": 98.0, "close": 102.0}
        for day in range(1, 12)
    ]

    class Adapter:
        def get_historical_bars(self, symbols, start, end):
            return dict.fromkeys(symbols, rows)

    outcome = verify_pending(book, Adapter(), today=date(2026, 6, 20))["outcomes"][0]

    assert outcome["macro_context"]["recorded"] is True
    assert outcome["macro_context"]["topics"] == ["china_macro"]
    assert outcome["macro_context"]["expectations"] == ["china_macro:beat"]


def test_incomparable_is_not_folded_into_unrecorded() -> None:
    """「跨日不可比」与「未记录」是两件事，合并会让两种相反的情况长得一样。

    前者＝这一层跑了但两边日期对不上（盘中取数的常态），后者＝压根没有这个读数。
    合成一个桶的话，「从来没生效过」和「每天都在被跳过」就分不出来了。
    """
    rows = [state_row(0.02, index_divergence="跨日不可比") for _ in range(2)]
    rows += [outcome_row("recommendation", 0.02) for _ in range(2)]

    buckets = {item["bucket"]: item for item in summarize(rows)["by_index_divergence"]}

    assert buckets["跨日不可比"]["samples"] == 2
    assert buckets["未记录"]["samples"] == 2


def test_review_renders_every_group_it_computes() -> None:
    """算了但不渲染 = 没算。

    此前 `by_playbook` / `by_entry_basis` / `by_decider` 三组一直在 payload 里躺着，
    从来没进过复盘正文。这条测试守的是「统计层新增一组，正文必须跟着出现」——
    否则下一层读数还会重复同一个结局。
    """
    from datetime import date as _date

    from ashare_decision_support.jobs.review import render_review

    rows = [
        state_row(0.05, rotation_adjacent="高低切换", turnover_total_change=-0.14,
                  index_divergence="等权强加权弱")
        for _ in range(3)
    ]
    for row in rows:
        row["playbook"] = "回踩"
        row["adjudication"] = {"chosen_entry": {"basis": "分时均价回踩"}, "used_fallback": True}

    text = render_review(
        _date(2026, 8, 4),
        {"resolved": 3, "checked": 3, "notes": []},
        summarize(rows),
    )

    for title in (
        "按打法",
        "按买入位依据",
        "按裁决方",
        "按宏观主题",
        "按宏观方向信号",
        "按预期差",
        "按周度轮动方向",
        "按全场成交额",
        "按等权与加权是否背离",
    ):
        assert title in text, f"复盘正文缺了「{title}」这一组"


def test_review_renders_recent_validation_windows() -> None:
    from datetime import date as _date

    from ashare_decision_support.jobs.review import render_review

    rows = [
        {**outcome_row("recommendation", 0.04), "trade_date": "2026-08-01"},
        {**outcome_row("control", 0.02), "trade_date": "2026-08-01"},
        {**outcome_row("recommendation", -0.01), "trade_date": "2026-08-02"},
        {**outcome_row("control", 0.01), "trade_date": "2026-08-02"},
    ]
    text = render_review(
        _date(2026, 8, 7),
        {"resolved": 4, "checked": 4, "notes": []},
        summarize(rows),
        rolling_validation(rows, windows=(2,)),
    )

    assert "## 近期验证" in text
    assert "last_2_sessions" in text


def test_trade_flow_reaches_the_stats_layer() -> None:
    """逐笔旁证必须一路走到统计层，否则存了也白存。

    链路有三段，断任何一段结果都一样（分组永远是「未记录」）：
    `Recommendation.trade_flow` → `verify.py` 带进 `Outcome` → `stats.py` 分档。
    """
    rows = [
        {**outcome_row("recommendation", 0.06), "trade_flow": {"ok": True, "large_net_amount": 9e7}}
        for _ in range(3)
    ]
    rows += [
        {**outcome_row("recommendation", -0.01), "trade_flow": {"ok": True, "large_net_amount": -4e7}}
        for _ in range(3)
    ]

    buckets = {item["bucket"]: item for item in summarize(rows)["by_large_net"]}

    assert set(buckets) == {"大单净流入", "大单净流出"}
    assert buckets["大单净流入"]["samples"] == 3
    # 只分正负不分档：金额跨标的不可比，方向才可比。
    assert buckets["大单净流出"]["samples"] == 3


def test_trade_flow_that_failed_to_fetch_is_not_a_direction() -> None:
    """`ok=False` 与 `--no-trade-flow` 跑出来的批次都落「未记录」。

    半成品读数进了统计比没有更糟：它看起来是一个可分组的维度，实际是噪声。
    """
    rows = [
        {**outcome_row("recommendation", 0.02), "trade_flow": {"ok": False, "large_net_amount": 5e7}},
        {**outcome_row("recommendation", 0.02), "trade_flow": {}},
        outcome_row("recommendation", 0.02),
    ]

    buckets = {item["bucket"]: item for item in summarize(rows)["by_large_net"]}

    assert list(buckets) == ["未记录"]
    assert buckets["未记录"]["samples"] == 3


def test_outcome_carries_trade_flow_from_the_recommendation(tmp_path) -> None:
    """链路中段：`Recommendation.trade_flow` 必须被 `verify.py` 带进 `Outcome`。

    这一段断掉的症状与另外两段一模一样——分组永远是「未记录」，而建议记录里
    那个字段明明有值。所以三段各测一条，不能只测两头。
    """
    book = Journal(root=tmp_path)
    record = make_record("600002.SH", KIND_RECOMMENDATION, tmp_path)
    record = Recommendation(
        **{
            **record.to_dict(),
            "trade_flow": {"ok": True, "large_net_amount": 1.5e8, "buy_ratio": 0.61},
        }
    )
    book.record(record)

    rows = [
        {"date": f"2026-06-{day:02d}", "open": 100.0, "high": 104.0, "low": 98.0, "close": 102.0}
        for day in range(1, 12)
    ]

    class Adapter:
        def get_historical_bars(self, symbols, start, end):
            return dict.fromkeys(symbols, rows)

    outcome = verify_pending(book, Adapter(), today=date(2026, 6, 20))["outcomes"][0]

    assert outcome["trade_flow"]["large_net_amount"] == 1.5e8
    assert outcome["trade_flow"]["ok"] is True


# ---- 状态文件里的 NaN：两个方向都要挡 -------------------------------------


def test_state_file_never_persists_an_invalid_json_constant(tmp_path) -> None:
    """`json.dump` 对 NaN 会写出裸的 `NaN`——那**不是合法 JSON**。

    症状特别隐蔽：Python 自己读得回来，所以系统内部一切正常；但文件从此 `jq`
    读不了、别的语言读不了。而持仓成本、止损、台账都存在这里。宁可在写入点抛，
    也不要写出这种文件。
    """
    from ashare_decision_support.state.store import write_json

    target = tmp_path / "holdings.json"
    try:
        write_json(target, {"600519.SH": {"cost": float("nan")}})
    except ValueError:
        # 抛了，且**没有留下半个文件**——写是原子的。
        assert not target.exists()
        assert not list(tmp_path.glob(".*"))
        return
    raise AssertionError("NaN 应当在写入点被拒绝")


def test_a_poisoned_state_file_is_treated_as_corrupt_not_as_readings(tmp_path) -> None:
    """`json.load` **默认接受** `NaN` 字面量并还原成 float。

    不挡的话，一个被写坏过一次的文件会**永久**把 NaN 喂给每一次跑批：成本是
    NaN、浮盈是 NaN、「跌破止损」的比较恒为 False。按既有约定当成文件损坏处理，
    返回 default 让调用方重建。
    """
    from ashare_decision_support.state.store import read_json

    poisoned = tmp_path / "holdings.json"
    poisoned.write_text('{"600519.SH": {"cost": NaN, "stop": Infinity}}', encoding="utf-8")

    assert read_json(poisoned, default={"rebuilt": True}) == {"rebuilt": True}


def test_a_healthy_state_file_still_round_trips(tmp_path) -> None:
    from ashare_decision_support.state.store import read_json, write_json

    target = tmp_path / "holdings.json"
    payload = {"600519.SH": {"cost": 1350.6, "stop": 1330.0, "note": "核心仓"}}
    write_json(target, payload)

    assert read_json(target) == payload


def test_one_poisoned_journal_line_costs_only_that_line(tmp_path) -> None:
    """台账是逐行独立的，含 NaN 的那条丢掉，其余照常读。"""
    from ashare_decision_support.state.store import read_jsonl

    path = tmp_path / "journal.jsonl"
    path.write_text(
        '{"symbol": "600519.SH", "pnl": 1.5}\n'
        '{"symbol": "000001.SZ", "pnl": NaN}\n'
        '{"symbol": "300750.SZ", "pnl": -2.0}\n',
        encoding="utf-8",
    )

    rows = read_jsonl(path)

    assert [row["symbol"] for row in rows] == ["600519.SH", "300750.SZ"]
