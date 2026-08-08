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
