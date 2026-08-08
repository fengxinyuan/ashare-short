"""The external Codex protocol must retain L3 freedom without accepting prices."""

from __future__ import annotations

from datetime import datetime

from ashare_decision_support.advice.external import (
    PACKET_SCHEMA,
    build_adjudication_packet,
    validate_external_decisions,
)
from ashare_decision_support.cli import build_parser


def facts_bundle(*, current: bool = True) -> dict:
    return {
        "schema": "ashare-facts-v1",
        "as_of": "2026-08-07T10:00:00",
        "facts": {
            "symbols": [
                {
                    "symbol": "600000.SH",
                    "last_price": 10.1,
                    "session_date": "2026-08-07",
                    "is_current_session": current,
                    "intraday": {"is_current_session": current, "last_timestamp": "2026-08-07T10:00:00"},
                    "warnings": [],
                    "candidates": {
                        "entries": [
                            {"key": "entry:分时均价回踩:10.000", "side": "entry", "price": 10.0},
                            {"key": "entry:日内低点:9.900", "side": "entry", "price": 9.9},
                        ],
                        "stops": [
                            {"key": "stop:结构破位:9.500", "side": "stop", "price": 9.5}
                        ],
                        "targets": [
                            {"key": "target:日线压力:10.800", "side": "target", "price": 10.8}
                        ],
                    },
                    "adjudication": None,
                }
            ]
        },
    }


def select_response(**overrides) -> dict:
    decision = {
        "symbol": "600000.SH",
        "action": "select",
        "entry": "entry:日内低点:9.900",
        "stop": "stop:结构破位:9.500",
        "target": "target:日线压力:10.800",
        "reason": "选择 9.900 日内低点，止损 9.500",
    }
    decision.update(overrides)
    return {"decisions": [decision]}


def test_external_packet_exposes_dynamic_candidates_and_rule_default() -> None:
    packet = build_adjudication_packet(facts_bundle())

    assert packet["schema"] == PACKET_SCHEMA
    assert packet["symbols"][0]["rule_default"]["entry"]["price"] == 10.0
    assert packet["symbols"][0]["candidates"]["entries"][1]["key"].endswith("9.900")


def test_external_selection_can_override_default_but_never_invent_a_price() -> None:
    packet = build_adjudication_packet(facts_bundle())
    result = validate_external_decisions(
        packet, select_response(), now=datetime(2026, 8, 7, 10, 5)
    )

    assert result.accepted is True
    assert result.selections[0]["entry"]["price"] == 9.9
    assert result.selections[0]["stop"]["price"] == 9.5

    invented = validate_external_decisions(
        packet,
        select_response(entry="entry:模型自造:9.800"),
        now=datetime(2026, 8, 7, 10, 5),
    )
    assert invented.accepted is False
    assert any("有效的 entry" in reason for reason in invented.reasons)


def test_external_protocol_rejects_stale_sessions_and_invalid_abstention() -> None:
    stale = validate_external_decisions(
        build_adjudication_packet(facts_bundle(current=False)),
        select_response(),
        now=datetime(2026, 8, 7, 10, 5),
    )
    assert stale.accepted is False
    assert any("当前交易日" in reason for reason in stale.reasons)

    packet = build_adjudication_packet(facts_bundle())
    abstain = validate_external_decisions(
        packet,
        select_response(action="abstain", stop=None, target=None),
        now=datetime(2026, 8, 7, 10, 5),
    )
    assert abstain.accepted is False
    assert any("必须全部为 null" in reason for reason in abstain.reasons)


def test_external_protocol_requires_every_packet_symbol() -> None:
    packet = build_adjudication_packet(facts_bundle())
    result = validate_external_decisions(
        packet,
        {"decisions": []},
        now=datetime(2026, 8, 7, 10, 5),
    )
    assert result.accepted is False


def test_external_protocol_normalizes_timezone_offsets() -> None:
    packet = build_adjudication_packet(facts_bundle())
    packet["as_of"] = "2026-08-07T02:00:00Z"
    result = validate_external_decisions(
        packet,
        select_response(),
        now=datetime(2026, 8, 7, 10, 5),
    )
    assert result.accepted is True


def test_external_protocol_rejects_non_numeric_candidate_price() -> None:
    packet = build_adjudication_packet(facts_bundle())
    packet["symbols"][0]["candidates"]["entries"][1]["price"] = "bad"
    result = validate_external_decisions(
        packet,
        select_response(),
        now=datetime(2026, 8, 7, 10, 5),
    )
    assert result.accepted is False


def test_cli_parser_accepts_external_adjudication_commands() -> None:
    packet = build_parser().parse_args(["codex-adjudication-packet", "--facts", "facts.json"])
    validate = build_parser().parse_args(
        ["codex-adjudication-validate", "--packet", "packet.json", "--decision", "decision.json"]
    )

    assert packet.command == "codex-adjudication-packet"
    assert validate.max_age_minutes == 15
