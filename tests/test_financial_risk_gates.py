"""金融语义回归：整段买入区间、最终价位与缺失情绪读数。"""

from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace

import pytest

from ashare_decision_support.advice.engine import AdviceEngine, StockAdvice, risk_reward
from ashare_decision_support.ask.verdict import LABEL_ACTIONABLE, assess
from ashare_decision_support.jobs.advise import _risk_qualified_plan
from ashare_decision_support.models.enums import EmotionCycle
from ashare_decision_support.monitor.session import MonitorSession
from ashare_decision_support.monitor.triggers import TriggerEngine
from ashare_decision_support.state.alerts import AlertState
from ashare_decision_support.strategy.regime import classify_regime
from tests.test_advice_layers import StubClient, advice_with, daily_for, intraday_for


def _plan(**changes) -> StockAdvice:
    values = {
        "symbol": "600519.SH",
        "last_price": 10.4,
        "entry_low": 10.0,
        "entry_high": 10.4,
        "stop": 9.8,
        "targets": [10.8],
        "rrr": 4.0,
        "playbook": "回踩",
        "setup": {"matched": [{"playbook": "回踩", "ok": True}]},
        "is_current_session": True,
    }
    return StockAdvice(**(values | changes))


def test_entry_band_is_evaluated_at_its_upper_price() -> None:
    plan = _plan()
    assert risk_reward(plan) == 0.67
    assert not _risk_qualified_plan(plan)
    decision = assess(asdict(plan))
    assert decision.label != LABEL_ACTIONABLE
    assert decision.rrr == 0.67


def test_nearest_target_controls_risk_even_when_targets_are_unordered() -> None:
    plan = _plan(targets=[12.0, 10.8, 11.0])
    assert risk_reward(plan) == 0.67
    assert assess(asdict(plan)).label != LABEL_ACTIONABLE


@pytest.mark.parametrize(
    "changes",
    [
        {"entry_high": float("nan")},
        {"entry_low": float("inf")},
        {"entry_high": 9.9},
        {"stop": -1.0},
        {"stop": 10.1},
        {"targets": []},
        {"targets": [10.4, 12.0]},
        {"targets": [float("nan")]},
        {"targets": [float("nan"), 12.0]},
        {"targets": [True, 12.0]},
        {"targets": None},
        {"targets": "12.0"},
    ],
)
def test_invalid_price_geometry_never_produces_a_risk_ratio(changes) -> None:
    assert risk_reward(_plan(**changes)) is None
    assert assess(asdict(_plan(**changes))).label != LABEL_ACTIONABLE
    assert assess(asdict(_plan(**changes)), holding={"cost_price": None}).label != LABEL_ACTIONABLE


def test_rounding_to_one_point_five_does_not_qualify_a_plan() -> None:
    plan = _plan(
        last_price=10.0, entry_low=10.0, entry_high=10.0, stop=9.0, targets=[11.499], rrr=1.5
    )
    assert risk_reward(plan) == 1.5  # 展示保留两位，闸门必须使用未舍入值。
    assert not _risk_qualified_plan(plan)
    assert assess(asdict(plan)).label != LABEL_ACTIONABLE


def test_exact_minimum_is_not_rejected_due_to_floating_point_subtraction() -> None:
    plan = _plan(entry_low=10.4, entry_high=10.4, stop=10.0, targets=[11.0], rrr=1.5)
    assert _risk_qualified_plan(plan)
    assert assess(asdict(plan)).label == LABEL_ACTIONABLE


def test_incomplete_plan_cannot_emit_an_action_buy_alert() -> None:
    now = datetime(2026, 7, 31, 10, 30)
    plan = _plan(stop=None, targets=[], rrr=None)
    intraday = SimpleNamespace(
        last_price=10.2,
        vwap=10.1,
        session_high=10.8,
        session_low=10.0,
        volume_ratio=1.2,
        last_timestamp=now,
        session_date=now.date(),
    )
    state = AlertState.load(now.date())
    state.set_state(plan.symbol, {"vs_entry": "above"})
    alerts = TriggerEngine().evaluate(plan, intraday, state, now)
    assert any(alert.trigger == "entry_blocked" for alert in alerts)
    assert not any(alert.level == "ACTION" for alert in alerts)


def test_pinned_prices_replace_outdated_risk_warnings() -> None:
    now = datetime(2026, 7, 31, 10, 30)
    session = MonitorSession(
        adapter=None,
        intraday_adapter=None,
        session_date=now.date(),
    )
    plan = _plan(warnings=["盈亏比仅 0.50，不满足超短线最低要求", "业绩否决：测试风险"])
    session.state.pin(plan.symbol, 9.5, [12.0], now)
    assert session._apply_pinned_levels(plan, SimpleNamespace(bar_count=60), now)
    assert plan.rrr == 1.78
    assert not any("盈亏比仅" in warning for warning in plan.warnings)
    assert "业绩否决：测试风险" in plan.warnings


def test_adjudicated_prices_recompute_warnings_from_final_levels() -> None:
    import json

    base = AdviceEngine().build(
        "600519.SH", daily_for(10.48, supports=[9.5], resistances=[11.0]), intraday_for()
    )
    # 同样的买入/止损，模型改选更远的既有压力位；旧目标的低盈亏比警告应重算。
    candidates = base.candidates
    target = max(candidates.targets, key=lambda item: item.price)
    reply = json.dumps(
        {
            "action": "select",
            "entry": candidates.default("entry").key(),
            "stop": candidates.default("stop").key(),
            "target": target.key(),
            "reason": "选择已有压力位",
        }
    )
    plan = advice_with(StubClient(reply))
    assert not plan.adjudication.used_fallback
    assert plan.rrr == risk_reward(plan)
    assert any("盈亏比仅" in warning for warning in plan.warnings) == (plan.rrr < 1.5)


@pytest.mark.parametrize("missing", ["max_streak", "limit_up_count", "limit_down_count"])
def test_missing_emotion_counts_are_not_zero_observations(missing: str) -> None:
    values = {"blowup_rate": 0.2, "max_streak": 5, "limit_up_count": 50, "limit_down_count": 0}
    values[missing] = None
    verdict = classify_regime(**values)
    assert verdict.cycle is EmotionCycle.UNKNOWN
    assert not verdict.trusted
    assert verdict.position_cap == 0.30


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), -0.1, 1.1, True])
def test_invalid_emotion_rates_cannot_produce_a_trusted_cycle(rate) -> None:
    verdict = classify_regime(rate, 5, 50, 0)
    assert verdict.cycle is EmotionCycle.UNKNOWN
    assert not verdict.trusted


@pytest.mark.parametrize("rate", [None, float("nan")])
def test_observed_panic_still_halts_new_entries_when_rate_is_missing(rate) -> None:
    verdict = classify_regime(rate, 3, 50, 25)
    assert verdict.cycle is EmotionCycle.FREEZING
    assert verdict.halted
    assert verdict.position_cap == 0.0
    assert any("跌停 25 家" in reason for reason in verdict.reasons)


@pytest.mark.parametrize("count", [-1, 3.5, True, float("inf")])
def test_invalid_counts_do_not_imply_a_measured_regime(count) -> None:
    verdict = classify_regime(0.2, 5, count, 0)
    assert verdict.cycle is EmotionCycle.UNKNOWN
    assert not verdict.trusted


def test_factual_zero_limit_ups_still_halts_entries() -> None:
    verdict = classify_regime(0.0, 0, 0, 0)
    assert verdict.cycle is EmotionCycle.FREEZING
    assert verdict.halted
