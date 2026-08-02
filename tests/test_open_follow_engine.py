from datetime import datetime

from ashare_decision_support.engines.open_follow import OpenFollowEngine
from ashare_decision_support.models.enums import RiskTag, Route, SignalMode
from ashare_decision_support.models.schemas import Candidate, OpenFeatures


def build_candidate(route: Route = Route.MAINLINE) -> Candidate:
    return Candidate(
        symbol="300001.SZ",
        route=route,
        thesis="test",
        stop_bps=350,
        rrr_min=2.0,
        min_position_pct=0.10,
        max_position_pct=0.15,
    )


def test_weak_open_stays_watch() -> None:
    engine = OpenFollowEngine()
    card = engine.analyze(
        build_candidate(),
        OpenFeatures(
            symbol="300001.SZ",
            minutes_since_open=4,
            price_vs_open_pct=-0.8,
            price_vs_vwap_pct=-0.2,
            volume_ratio=0.7,
            open_gap_pct=3.1,
            source_ts=datetime(2026, 7, 31, 9, 34, 0),
        ),
        strategy_version="v1.0",
    )
    assert card.signal_mode == SignalMode.WATCH
    assert RiskTag.OPENING_WEAKNESS in card.risk_tags


def test_strong_open_follow_signal() -> None:
    engine = OpenFollowEngine()
    card = engine.analyze(
        build_candidate(),
        OpenFeatures(
            symbol="300001.SZ",
            minutes_since_open=3,
            price_vs_open_pct=0.7,
            price_vs_vwap_pct=0.3,
            volume_ratio=1.4,
            open_gap_pct=3.1,
            source_ts=datetime(2026, 7, 31, 9, 33, 0),
        ),
        strategy_version="v1.0",
    )
    assert card.signal_mode == SignalMode.OPEN_FOLLOW
    assert card.expected_edge_bps > 0


def test_below_vwap_after_half_hour_blocks_follow() -> None:
    engine = OpenFollowEngine()
    card = engine.analyze(
        build_candidate(Route.PULSE),
        OpenFeatures(
            symbol="300001.SZ",
            minutes_since_open=35,
            price_vs_open_pct=-1.2,
            price_vs_vwap_pct=-0.6,
            volume_ratio=0.9,
            open_gap_pct=5.8,
            source_ts=datetime(2026, 7, 31, 10, 5, 0),
        ),
        strategy_version="v1.0",
    )
    assert card.signal_mode == SignalMode.BLOCK
    assert RiskTag.BELOW_VWAP in card.risk_tags
