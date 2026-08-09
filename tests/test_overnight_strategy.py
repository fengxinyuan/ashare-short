from datetime import date, datetime, timedelta

from ashare_decision_support.strategy.overnight import (
    PROFILES,
    StrategyCandidate,
    StrategyScreenConfig,
    StrategyScreenResult,
    _ordered_bars,
    diversified_shortlist,
    is_main_board,
    screen_overnight,
)


def make_bars(symbol: str, close: float = 20.0, amount: float = 800_000_000.0) -> list[dict]:
    rows = []
    for index in range(90):
        day = date(2026, 1, 1) + timedelta(days=index)
        price = close * (0.92 + index * 0.0009)
        rows.append(
            {
                "symbol": symbol,
                "timestamp": datetime.combine(day, datetime.min.time()),
                "open": price * 0.995,
                "high": price * 1.02,
                "low": price * 0.98,
                "close": price,
                "volume": 1_000_000,
                "amount": amount,
            }
        )
    return rows


def test_main_board_scope_is_explicit() -> None:
    assert is_main_board("600519.SH")
    assert is_main_board("000001.SZ")
    assert not is_main_board("300750.SZ")
    assert not is_main_board("688981.SH")
    assert not is_main_board("830799.BJ")


def test_profiles_are_named_and_unknown_profile_fails() -> None:
    assert {"balanced-pullback", "high-elasticity", "breakout-continuation"} <= set(PROFILES)
    try:
        StrategyScreenConfig(profile="missing").resolved_profile()
    except ValueError as exc:
        assert "unknown strategy profile" in str(exc)
    else:
        raise AssertionError("unknown profile must fail explicitly")


def test_config_rejects_invalid_ranges() -> None:
    for config in (
        StrategyScreenConfig(price_min=20, price_max=10),
        StrategyScreenConfig(min_amount_yi=-1),
        StrategyScreenConfig(min_atr_pct=0.2, max_atr_pct=0.1),
        StrategyScreenConfig(min_range_position=0.9, max_range_position=0.2),
    ):
        try:
            config.resolved_profile()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid screen configuration must fail")


def test_scanner_reports_scope_and_serializable_candidate_shape() -> None:
    bars = {
        "600519.SH": make_bars("600519.SH"),
        "300750.SZ": make_bars("300750.SZ"),
    }
    result = screen_overnight(
        bars,
        names={"600519.SH": "测试股", "300750.SZ": "创业板测试"},
        sectors={"600519.SH": "食品饮料"},
        config=StrategyScreenConfig(profile="balanced-pullback", limit=5),
    )
    payload = result.to_dict()
    assert payload["schema"] == "ashare-strategy-screen-v1"
    assert payload["summary"]["scanned"] == 2
    assert payload["summary"]["excluded_scope"] == 1
    assert "config" in payload and "profile" in payload


def test_st_is_a_configurable_scope_rule() -> None:
    bars = {"600519.SH": make_bars("600519.SH")}
    blocked = screen_overnight(
        bars,
        names={"600519.SH": "*ST测试"},
        config=StrategyScreenConfig(profile="balanced-pullback", allow_st=False),
    )
    allowed = screen_overnight(
        bars,
        names={"600519.SH": "*ST测试"},
        config=StrategyScreenConfig(profile="balanced-pullback", allow_st=True),
    )
    assert blocked.excluded_st == 1
    assert allowed.excluded_st == 0


def test_st_is_included_by_default() -> None:
    result = screen_overnight(
        {"600519.SH": make_bars("600519.SH")},
        names={"600519.SH": "*ST测试"},
        config=StrategyScreenConfig(profile="balanced-pullback"),
    )
    assert result.excluded_st == 0


def test_symbol_and_metadata_keys_are_normalized() -> None:
    result = screen_overnight(
        {"600519": make_bars("600519")},
        names={"600519": "测试股"},
        sectors={"600519": "食品饮料"},
        config=StrategyScreenConfig(profile="balanced-pullback", limit=5),
    )
    assert result.scanned == 1
    assert result.candidates or result.rejected_shape == 1


def test_bars_are_ordered_before_signal_day_is_selected() -> None:
    rows = make_bars("600519.SH")
    ordered = _ordered_bars(list(reversed(rows)))
    assert [row["timestamp"] for row in ordered] == sorted(row["timestamp"] for row in rows)


def test_scope_gate_runs_before_technical_analysis(monkeypatch) -> None:
    seen: list[str] = []

    def analyze_many(self, bars_by_symbol):
        seen.extend(sorted(bars_by_symbol))
        return {}

    monkeypatch.setattr("ashare_decision_support.strategy.overnight.DailyTechnicalAnalyzer.analyze_many", analyze_many)
    screen_overnight(
        {"600519.SH": make_bars("600519.SH"), "300750.SZ": make_bars("300750.SZ")},
        config=StrategyScreenConfig(profile="balanced-pullback"),
    )
    assert seen == ["600519.SH"]


def test_rejection_reason_is_exposed_for_tuning() -> None:
    result = screen_overnight(
        {"600519.SH": make_bars("600519.SH")},
        config=StrategyScreenConfig(profile="balanced-pullback", limit=5),
    )
    assert result.rejection_reasons
    assert result.to_dict()["summary"]["rejection_reasons"] == result.rejection_reasons


def test_multi_profile_shortlist_is_diversified_without_cross_scale_rank() -> None:
    def candidate(symbol: str, profile: str, rank: float) -> StrategyCandidate:
        return StrategyCandidate(
            symbol=symbol, name=None, sector=None, profile=profile, profile_label=profile,
            setup_rank=rank, rank_components={}, signal_date=None, last_close=10,
            signal_low=None, previous_low=None, amount_yi=5, atr_pct=0.02,
            range_position_60d=0.5, pullback_pct=-0.05, close_vs_ma20_pct=0,
            close_position=0.5, volume_contraction=0.8, volume_ratio_20d=1,
            pullback_atr=None, down_days=None, is_st=False,
        )
    results = {
        "a": StrategyScreenResult(StrategyScreenConfig(profile="balanced-pullback"), PROFILES["balanced-pullback"], [candidate("600001.SH", "a", 90)]),
        "b": StrategyScreenResult(StrategyScreenConfig(profile="high-elasticity"), PROFILES["high-elasticity"], [candidate("600002.SH", "b", 10)]),
    }
    rows = diversified_shortlist(results, 2)
    assert [row["symbol"] for row in rows] == ["600001.SH", "600002.SH"]


def passing_bars(
    symbol: str = "600000.SH",
    *,
    peak_at: int = 55,
    pullback: float = 0.04,
    count: int = 90,
    base: float = 20.0,
    amount: float = 8e8,
) -> list[dict]:
    """先上行建立 60 日高、再温和缩量回撤——`balanced-pullback` 要的正是这个形状。"""
    rows: list[dict] = []
    high_volume, low_volume = 2_000_000, 700_000
    for index in range(count):
        day = date(2026, 1, 1) + timedelta(days=index)
        if index <= peak_at:
            price = base * (1.0 + 0.006 * index)
            volume = high_volume
        else:
            progress = (index - peak_at) / max(1, count - 1 - peak_at)
            price = base * (1.0 + 0.006 * peak_at) * (1.0 - pullback * progress)
            volume = high_volume + (low_volume - high_volume) * progress
        high, low = price * 1.012, price * 0.988
        rows.append(
            {
                "symbol": symbol,
                "timestamp": datetime.combine(day, datetime.min.time()),
                "open": price * 0.999,
                "high": high,
                "low": low,
                "close": low + (high - low) * 0.45,
                "volume": volume,
                "amount": amount,
            }
        )
    return rows


def test_a_matching_shape_actually_produces_a_candidate() -> None:
    """**这条钉的是整个筛选器的成功路径。**

    在它之前，测试里没有任何一条断言过「真的选出了一只」——最接近的一条写的是
    `assert result.candidates or result.rejected_shape == 1`，两边都成立时它恒真。
    于是从「过完九条约束」到「算出 setup_rank、拼出 reasons、造出 StrategyCandidate」
    这一整段（模块里最长的一段）没有任何覆盖：它整个坏掉，全部测试照样绿，筛选器
    只是安静地一只都不出。
    """
    result = screen_overnight(
        {"600000.SH": passing_bars()}, config=StrategyScreenConfig(profile="balanced-pullback")
    )

    assert result.candidates, f"应当选出候选，实际拒绝原因：{result.to_dict().get('rejections')}"
    candidate = result.candidates[0]
    assert isinstance(candidate, StrategyCandidate)
    assert candidate.symbol == "600000.SH"
    assert candidate.profile == "balanced-pullback"

    # 排名是给列表排序用的透明分，不是预测——但它必须落在声明的量纲里。
    assert 0.0 <= candidate.setup_rank <= 100.0
    assert set(candidate.rank_components) == {
        "liquidity",
        "location",
        "pullback_quality",
        "close_quality",
    }
    assert all(0.0 <= value <= 1.0 for value in candidate.rank_components.values())

    # 每一条 reason 都必须是可复核的读数，不是形容词。
    assert len(candidate.reasons) == 4
    assert any("成交额" in reason for reason in candidate.reasons)
    assert candidate.pullback_pct < 0
    assert candidate.to_dict()["symbol"] == "600000.SH"


def test_an_accepted_candidate_survives_serialisation() -> None:
    """候选要整个序列化进产物，`to_dict()` 里不能留下不认识的对象。"""
    import json

    result = screen_overnight(
        {"600000.SH": passing_bars()}, config=StrategyScreenConfig(profile="balanced-pullback")
    )
    assert result.candidates
    json.dumps(result.candidates[0].to_dict(), ensure_ascii=False)


def test_a_high_close_earns_a_do_not_chase_warning() -> None:
    """收在日内高位时必须带一句「不宜按昨收直接追价」——那是最容易亏钱的形状。

    只收窄最后一根的**日内区间**，不抬它的收盘价：抬收盘价会把 60 日位置一起推
    出允许区间，那样测到的是「被拒绝」，不是这条告警。
    """
    rows = passing_bars()
    last = rows[-1]
    close = last["close"]
    span = close * 0.005
    last["low"] = close - span * 0.84
    last["high"] = close + span * 0.16

    result = screen_overnight(
        {"600000.SH": rows}, config=StrategyScreenConfig(profile="balanced-pullback")
    )
    assert result.candidates, result.to_dict().get("rejection_reasons")
    assert result.candidates[0].close_position >= 0.80
    assert any("不宜按昨收直接追价" in warning for warning in result.candidates[0].warnings)


def test_config_rejects_every_kind_of_invalid_bound() -> None:
    """参数是人从命令行给的，**每一条拒绝都要有测试**——否则一个坏区间会静默生效。"""
    import pytest

    bad_configs = [
        {"price_min": -1.0},
        {"price_max": 0.0},
        {"price_min": 50.0, "price_max": 10.0},
        {"min_amount_yi": -1.0},
        {"limit": -1},
        {"min_history_bars": 19},
        {"profile": "不存在的画像"},
        {"min_atr_pct": -0.1},
        {"min_atr_pct": 0.09, "max_atr_pct": 0.01},
        {"min_range_position": -0.1},
        {"max_range_position": 1.5},
        {"min_range_position": 0.9, "max_range_position": 0.1},
        {"min_volume_ratio": -1.0},
        {"min_volume_ratio": 5.0, "max_volume_ratio": 1.0},
        {"min_pullback_pct": -0.01, "max_pullback_pct": -0.20},
        {"min_close_position": -0.1},
        {"max_close_position": 1.5},
        {"min_close_position": 0.9, "max_close_position": 0.1},
    ]
    for overrides in bad_configs:
        with pytest.raises(ValueError):
            StrategyScreenConfig(**overrides).resolved_profile()


def test_breakout_and_pullback_rank_location_in_opposite_directions() -> None:
    """**突破与回踩对「60 日位置」的偏好是相反的，而这很容易写反。**

    突破要的是位置高（贴着 60 日高），回踩要的是位置低（跌透了）。写反了不会
    报错，只会让每份榜单把最不该排前面的那只排到第一。
    """
    from ashare_decision_support.strategy.overnight import _setup_rank

    def location_of(profile_name: str, range_position: float) -> float:
        _rank, components = _setup_rank(
            amount_yi=10.0,
            min_amount_yi=5.0,
            range_position=range_position,
            pullback=-0.08,
            min_pullback=-0.16,
            close_position_value=0.55,
            profile=PROFILES[profile_name],
        )
        return components["location"]

    assert location_of("breakout-continuation", 0.95) > location_of("breakout-continuation", 0.15)
    assert location_of("balanced-pullback", 0.15) > location_of("balanced-pullback", 0.95)


def test_setup_rank_stays_inside_its_declared_scale() -> None:
    """`setup_rank` 是 0-100 的排序分，越界会让它和别的分数没法并排读。"""
    from ashare_decision_support.strategy.overnight import _setup_rank

    for profile in PROFILES.values():
        for range_position in (0.0, 0.5, 1.0):
            for amount_yi in (0.01, 5.0, 5000.0):
                rank, components = _setup_rank(
                    amount_yi=amount_yi,
                    min_amount_yi=5.0,
                    range_position=range_position,
                    pullback=-0.30,
                    min_pullback=-0.16,
                    close_position_value=range_position,
                    profile=profile,
                )
                assert 0.0 <= rank <= 100.0, (profile.name, range_position, amount_yi)
                assert all(0.0 <= value <= 1.0 for value in components.values())
