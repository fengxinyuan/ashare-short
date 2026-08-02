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
        }
        for index, (close, vol) in enumerate(zip(closes, vols))
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


# ---- 打分口径转向低吸 -------------------------------------------------------


def snap(**kw):
    from datetime import datetime
    from ashare_decision_support.models.enums import TrendState
    from ashare_decision_support.models.schemas import DailyTechnicalSnapshot

    d = dict(
        symbol="600519.SH", last_open=99.0, last_high=101.0, last_low=98.0,
        last_close=100.0, trend_state=TrendState.UPTREND, bar_count=250,
        atr14=2.0, volume_ratio_20d=1.0, range_position_60d=0.5,
        stop_reference=95.0, target_reference=115.0,
        explanation="fixture", source_ts=datetime(2026, 8, 3, 15, 0),
    )
    d.update(kw)
    return DailyTechnicalSnapshot(**d)


def dip(**kw):
    from ashare_decision_support.signals.dipbuy import DipFeatures
    d = dict(pullback_pct=-0.06, atr_pct=0.02, pullback_atr=3.0,
             pullback_band="ideal", shrinking=True, volume_contraction=0.5)
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

    no_dip = dict(pullback_band="none", pullback_atr=0.2, pullback_pct=-0.004)
    quiet_flat = score(snapshot=snap(volume_ratio_20d=0.5), dip=dip(**no_dip))
    loud_flat = score(snapshot=snap(volume_ratio_20d=1.8), dip=dip(**no_dip))
    assert loud_flat.score > quiet_flat.score


def test_wyckoff_pullback_events_now_outweigh_the_breakout_event() -> None:
    """Spring / LPS 是低吸信号，SOS 是「已经涨上去了」。"""
    from ashare_decision_support.scoring.rule_based import (
        LPS_POINTS, SOS_POINTS, SPRING_POINTS,
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
    """低吸门槛是**门槛不是权重**。

    dip 在打分里只占 10/85，trend + wyckoff 占 50，靠加权永远压不过「趋势强、
    位置高」的票。2026-08-02 实测：全市场随机 278 只里 27% 在 none 档、29% 在
    ideal 档，而打分选出的 15 只有 10 只是 none——分布差这么远，说明问题出在
    「要不要它」而不是「给它几分」。
    """
    from ashare_decision_support.jobs.morning import _is_dip_candidate
    from ashare_decision_support.signals.dipbuy import DipFeatures

    assert _is_dip_candidate(DipFeatures(pullback_band="ideal")) is True
    assert _is_dip_candidate(DipFeatures(pullback_band="shallow")) is True
    # deep 放行：是否值得买交给分数（已扣分并告警），门槛不替它判死。
    assert _is_dip_candidate(DipFeatures(pullback_band="deep")) is True
    assert _is_dip_candidate(DipFeatures(pullback_band="none")) is False
    # 算不出来的放行——门槛挡的是「明确没回调」，不是「不知道」。
    assert _is_dip_candidate(DipFeatures()) is True
    assert _is_dip_candidate(None) is True


def test_sector_ranking_no_longer_rewards_having_already_run() -> None:
    """板块层此前 `ret_5d * 120` 单调递增，涨得越多分越高。

    2026-08-02 实测：前五名板块的 5 日中位是 +10.16%/+10.48%/+9.48%/+8.03%/
    +4.80%，排序几乎就是已涨幅度倒序——那是追涨的排法，不是低吸的。
    """
    from ashare_decision_support.sectors.predict import SectorPredictor
    from ashare_decision_support.sectors.base import SectorMetrics

    predictor = SectorPredictor(None)

    def momentum(**kw):
        d = dict(name="X", constituents=50, covered=50, ret_1d=0.005, ret_5d=0.03,
                 ret_20d=0.08, breadth=0.6, above_ma20=0.7, volume_ratio=1.0)
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
