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


# ---- 拒绝路径 ---------------------------------------------------------------
#
# 这一层是**唯一**挡住「外部分析者自己编一个价」的地方，而它的拒绝分支此前几乎
# 没有测试（实测 182 条语句里 50 条未覆盖，绝大多数是 `reasons.append(); return`）。
# 校验代码最危险的失效方式不是报错，是**悄悄放行**——所以每条拒绝都要有测试钉住。


def test_packet_must_be_a_well_formed_adjudication_packet() -> None:
    response = select_response()
    assert not validate_external_decisions("不是对象", response).accepted
    assert not validate_external_decisions({}, response).accepted
    assert not validate_external_decisions({"schema": "别的协议"}, response).accepted


def test_response_must_be_an_object_with_a_non_empty_decisions_array() -> None:
    packet = build_adjudication_packet(facts_bundle())
    now = datetime(2026, 8, 7, 10, 5)
    for bad in ("不是对象", {}, {"decisions": []}, {"decisions": "不是数组"}):
        assert not validate_external_decisions(packet, bad, now=now).accepted


def test_a_symbol_outside_the_packet_is_refused() -> None:
    """**这条是这一层的核心。** 不在候选包里的标的，连带它的价一律不认。"""
    packet = build_adjudication_packet(facts_bundle())
    check = validate_external_decisions(
        packet, select_response(symbol="000001.SZ"), now=datetime(2026, 8, 7, 10, 5)
    )
    assert not check.accepted
    assert any("不在裁决包候选范围内" in reason for reason in check.reasons)


def test_duplicate_and_malformed_decisions_are_refused() -> None:
    packet = build_adjudication_packet(facts_bundle())
    now = datetime(2026, 8, 7, 10, 5)

    twice = {"decisions": [select_response()["decisions"][0]] * 2}
    check = validate_external_decisions(packet, twice, now=now)
    assert not check.accepted
    assert any("重复裁决" in reason for reason in check.reasons)

    check = validate_external_decisions(packet, {"decisions": ["不是对象"]}, now=now)
    assert not check.accepted


def test_action_must_be_select_or_abstain() -> None:
    packet = build_adjudication_packet(facts_bundle())
    check = validate_external_decisions(
        packet, select_response(action="buy"), now=datetime(2026, 8, 7, 10, 5)
    )
    assert not check.accepted
    assert any("只能是 select 或 abstain" in reason for reason in check.reasons)


def test_reason_is_length_capped_and_may_not_carry_untraceable_numbers() -> None:
    """理由里的每个数字都必须能在事实包里找到——否则它就是编出来的。"""
    packet = build_adjudication_packet(facts_bundle())
    now = datetime(2026, 8, 7, 10, 5)

    check = validate_external_decisions(packet, select_response(reason="长" * 121), now=now)
    assert not check.accepted
    assert any("超过 120 字" in reason for reason in check.reasons)

    check = validate_external_decisions(
        packet, select_response(reason="目标看到 12.345"), now=now
    )
    assert not check.accepted
    assert any("无法追溯的数字" in reason for reason in check.reasons)


def test_small_ordinals_in_the_reason_are_allowed() -> None:
    """「第 3 档」这类序数不算编造的价格，否则理由里没法说人话。"""
    packet = build_adjudication_packet(facts_bundle())
    check = validate_external_decisions(
        packet, select_response(reason="取第 2 档入场"), now=datetime(2026, 8, 7, 10, 5)
    )
    assert check.accepted, check.reasons


def test_abstain_must_leave_every_price_null() -> None:
    packet = build_adjudication_packet(facts_bundle())
    now = datetime(2026, 8, 7, 10, 5)

    check = validate_external_decisions(
        packet,
        {"decisions": [{"symbol": "600000.SH", "action": "abstain", "reason": "看不清"}]},
        now=now,
    )
    assert check.accepted, check.reasons
    assert check.selections[0]["action"] == "abstain"

    check = validate_external_decisions(
        packet, select_response(action="abstain"), now=now
    )
    assert not check.accepted
    assert any("必须全部为 null" in reason for reason in check.reasons)


def test_a_stop_at_or_above_the_entry_is_refused() -> None:
    """止损不低于入场价的「计划」不是计划。"""
    packet = build_adjudication_packet(facts_bundle())
    check = validate_external_decisions(
        packet,
        select_response(entry="entry:日内低点:9.900", stop="stop:结构破位:9.500", target=None),
        now=datetime(2026, 8, 7, 10, 5),
    )
    assert check.accepted, check.reasons

    # 把 entry 换成比 stop 更低的那一档，stop 就不再低于 entry。
    packet_low = build_adjudication_packet(facts_bundle())
    packet_low["symbols"][0]["candidates"]["entries"].append(
        {"key": "entry:极低:9.000", "side": "entry", "price": 9.0}
    )
    check = validate_external_decisions(
        packet_low,
        select_response(entry="entry:极低:9.000", target=None, reason="取更低一档"),
        now=datetime(2026, 8, 7, 10, 5),
    )
    assert not check.accepted
    assert any("stop 不低于 entry" in reason for reason in check.reasons)


def test_an_unknown_candidate_key_is_refused_even_when_it_looks_like_a_price() -> None:
    """**不能凭价格匹配，只能凭候选键。** 否则「编一个恰好相等的数」就能过。"""
    packet = build_adjudication_packet(facts_bundle())
    now = datetime(2026, 8, 7, 10, 5)
    for bad in ("entry:不存在的键:9.900", "", 9.9, None):
        check = validate_external_decisions(packet, select_response(entry=bad), now=now)
        assert not check.accepted, bad


def test_a_stale_or_future_packet_is_refused() -> None:
    packet = build_adjudication_packet(facts_bundle())
    stale = validate_external_decisions(
        packet, select_response(), now=datetime(2026, 8, 7, 12, 0), max_age_minutes=15
    )
    assert not stale.accepted
    assert any("滞后" in reason for reason in stale.reasons)

    ahead = validate_external_decisions(
        packet, select_response(), now=datetime(2026, 8, 7, 9, 0)
    )
    assert not ahead.accepted
    assert any("晚于校验时间" in reason for reason in ahead.reasons)


def test_a_packet_without_a_parsable_as_of_is_refused() -> None:
    packet = build_adjudication_packet(facts_bundle())
    packet["as_of"] = "昨天下午"
    check = validate_external_decisions(packet, select_response(), now=datetime(2026, 8, 7, 10, 5))
    assert not check.accepted
    assert any("as_of" in reason for reason in check.reasons)


def test_duplicate_symbols_in_the_packet_are_refused() -> None:
    """同一个标的在包里出现两次，`known` 会少一条，下游的「缺少裁决」判断就失真。"""
    packet = build_adjudication_packet(facts_bundle())
    packet["symbols"] = packet["symbols"] * 2
    check = validate_external_decisions(packet, select_response(), now=datetime(2026, 8, 7, 10, 5))
    assert not check.accepted
    assert any("唯一且非空" in reason for reason in check.reasons)


def test_an_invalid_target_key_is_refused_even_though_target_is_optional() -> None:
    """target 可以是 null，但给了就必须是真候选——「可选」不等于「随便填」。"""
    packet = build_adjudication_packet(facts_bundle())
    check = validate_external_decisions(
        packet, select_response(target="target:编的:99.900"), now=datetime(2026, 8, 7, 10, 5)
    )
    assert not check.accepted
    assert any("target" in reason for reason in check.reasons)


def test_a_candidate_carrying_a_non_finite_price_is_refused() -> None:
    """NaN 价过得了 `float()`，过不了 `isfinite`——它一旦漏下去，之后每个比较都是 False。"""
    packet = build_adjudication_packet(facts_bundle())
    packet["symbols"][0]["candidates"]["entries"].append(
        {"key": "entry:坏价:nan", "side": "entry", "price": float("nan")}
    )
    check = validate_external_decisions(
        packet, select_response(entry="entry:坏价:nan"), now=datetime(2026, 8, 7, 10, 5)
    )
    assert not check.accepted


def test_packet_reports_the_rule_default_from_an_existing_adjudication() -> None:
    """已经有内部裁决时，规则默认值取它的 `rule_*`，而不是候选表的第一条。"""
    bundle = facts_bundle()
    bundle["facts"]["symbols"][0]["adjudication"] = {
        "rule_entry": {"key": "entry:日内低点:9.900", "side": "entry", "price": 9.9},
        "rule_stop": {"key": "stop:结构破位:9.500", "side": "stop", "price": 9.5},
        "rule_target": None,
    }
    packet = build_adjudication_packet(bundle)
    assert packet["symbols"][0]["rule_default"]["entry"]["key"] == "entry:日内低点:9.900"
    assert packet["symbols"][0]["rule_default"]["target"] is None


def test_packet_finds_symbols_nested_one_level_down() -> None:
    """事实包的形状不止一种，`symbols` 可能挂在某个子块里。"""
    bundle = facts_bundle()
    bundle["facts"] = {"advise": {"symbols": bundle["facts"]["symbols"]}}
    packet = build_adjudication_packet(bundle)
    assert [row["symbol"] for row in packet["symbols"]] == ["600000.SH"]
