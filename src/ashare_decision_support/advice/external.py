"""Stable, offline-safe contract for an external price adjudicator.

An external analyst (including Codex) is allowed to choose among live
deterministic candidates, but it is never allowed to manufacture a price.  The
packet stays separate from execution: choosing a plan is analytical work;
the system does not create or submit orders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from ashare_decision_support.advice.adjudicate import ORDINAL_CEILING
from ashare_decision_support.writer.llm import NUMBER_PATTERN

PACKET_SCHEMA = "ashare-codex-adjudication-v1"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ExternalDecisionCheck:
    accepted: bool
    selections: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_adjudication_packet(facts_bundle: dict[str, Any]) -> dict[str, Any]:
    """Extract the minimum current facts needed to choose existing levels."""
    root = _facts_root(facts_bundle)
    rows = _symbol_rows(root)
    symbols: list[dict[str, Any]] = []
    for row in rows:
        candidates = row.get("candidates")
        if not isinstance(candidates, dict) or not any(candidates.get(side) for side in _SIDES):
            continue
        symbols.append(
            {
                "symbol": row.get("symbol"),
                "last_price": row.get("last_price"),
                "session_date": row.get("session_date"),
                "is_current_session": row.get("is_current_session"),
                "intraday": row.get("intraday") or {},
                "setup": row.get("setup"),
                "playbook": row.get("playbook"),
                "warnings": row.get("warnings") or [],
                "reasons": row.get("reasons") or [],
                "order_book": row.get("order_book") or None,
                "volume_price": row.get("volume_price") or None,
                "distribution": row.get("distribution") or None,
                "candidates": candidates,
                "rule_default": _rule_default(row.get("adjudication"), candidates),
            }
        )
    return {
        "schema": PACKET_SCHEMA,
        "contract": {
            "purpose": "external selection among deterministic A-share price candidates",
            "authority": "select candidate keys or abstain; do not create prices, position sizes, or orders",
            "freshness": "validate before use; stale or non-current-session facts must be rejected",
            "missing_values": "null or empty collection means unknown, never zero or negative evidence",
        },
        "as_of": facts_bundle.get("as_of"),
        "source_schema": facts_bundle.get("schema"),
        "response_schema": {
            "decisions": [
                {
                    "symbol": "600519.SH",
                    "action": "select | abstain",
                    "entry": "entry candidate key or null",
                    "stop": "stop candidate key or null",
                    "target": "target candidate key or null",
                    "reason": "max 120 chars; numbers must occur verbatim in the packet",
                }
            ]
        },
        "symbols": symbols,
    }


def validate_external_decisions(
    packet: dict[str, Any],
    response: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age_minutes: int = 15,
) -> ExternalDecisionCheck:
    """Validate external selections without trusting a model-supplied price."""
    reasons: list[str] = []
    if not isinstance(packet, dict):
        return ExternalDecisionCheck(False, reasons=["packet 必须是对象"])
    if packet.get("schema") != PACKET_SCHEMA:
        reasons.append("不是有效的 Codex 裁决包")
        return ExternalDecisionCheck(False, reasons=reasons)
    if not _is_fresh(packet, now or datetime.now(), max_age_minutes, reasons):
        return ExternalDecisionCheck(False, reasons=reasons)

    if not isinstance(response, dict):
        return ExternalDecisionCheck(False, reasons=["response 必须是对象"])
    packet_symbols = packet.get("symbols")
    if not isinstance(packet_symbols, list) or any(not isinstance(row, dict) for row in packet_symbols):
        return ExternalDecisionCheck(False, reasons=["裁决包 symbols 必须是对象数组"])
    decisions = response.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        return ExternalDecisionCheck(False, reasons=["response.decisions 必须是非空数组"])
    known = {str(row.get("symbol") or "").strip(): row for row in packet_symbols}
    if len(known) != len(packet_symbols) or "" in known:
        return ExternalDecisionCheck(False, reasons=["裁决包 symbols 必须唯一且非空"])
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in decisions:
        if not isinstance(item, dict):
            reasons.append("每条裁决必须是对象")
            continue
        symbol = str(item.get("symbol") or "").strip()
        row = known.get(symbol)
        if not symbol or row is None:
            reasons.append(f"{symbol or '空标的'} 不在裁决包候选范围内")
            continue
        if symbol in seen:
            reasons.append(f"{symbol} 出现重复裁决")
            continue
        seen.add(symbol)
        _validate_one(item, row, selected, reasons)
    missing = sorted(set(known) - seen)
    if missing:
        reasons.append(f"缺少标的裁决：{'、'.join(missing[:5])}")
    return ExternalDecisionCheck(not reasons, selections=selected if not reasons else [], reasons=reasons)


_SIDES = ("entries", "stops", "targets")
_RESPONSE_KEYS = {"entry": "entries", "stop": "stops", "target": "targets"}


def _validate_one(
    decision: dict[str, Any], row: dict[str, Any], selected: list[dict[str, Any]], reasons: list[str]
) -> None:
    symbol = str(row["symbol"])
    action = str(decision.get("action") or "").lower()
    reason = str(decision.get("reason") or "").strip()
    if action not in {"select", "abstain"}:
        reasons.append(f"{symbol} 的 action 只能是 select 或 abstain")
        return
    if len(reason) > 120:
        reasons.append(f"{symbol} 的 reason 超过 120 字")
        return
    untraceable = _untraceable_numbers(reason, row)
    if untraceable:
        reasons.append(f"{symbol} 的 reason 含无法追溯的数字 {untraceable[:3]}")
        return
    if action == "abstain":
        if any(decision.get(key) is not None for key in _RESPONSE_KEYS):
            reasons.append(f"{symbol} 弃权时 entry/stop/target 必须全部为 null")
            return
        selected.append({"symbol": symbol, "action": "abstain", "reason": reason or "外部分析者弃权"})
        return

    resolved = {key: _candidate(row, side, decision.get(key)) for key, side in _RESPONSE_KEYS.items()}
    if resolved["entry"] is None or resolved["stop"] is None:
        reasons.append(f"{symbol} 必须选择有效的 entry 与 stop 候选键")
        return
    for key in ("entry", "stop", "target"):
        if decision.get(key) is not None and resolved[key] is None:
            reasons.append(f"{symbol} 的 {key} 不是有效候选键或价格")
            return
    if resolved["stop"]["price"] >= resolved["entry"]["price"]:
        reasons.append(f"{symbol} 的 stop 不低于 entry")
        return
    selected.append(
        {
            "symbol": symbol,
            "action": "select",
            "entry": resolved["entry"],
            "stop": resolved["stop"],
            "target": resolved["target"],
            "reason": reason,
        }
    )


def _candidate(row: dict[str, Any], side: str, key: Any) -> dict[str, Any] | None:
    if not isinstance(key, str) or not key.strip():
        return None
    for candidate in (row.get("candidates") or {}).get(side) or []:
        if isinstance(candidate, dict) and candidate.get("key") == key.strip():
            try:
                price = float(candidate.get("price"))
            except (TypeError, ValueError):
                return None
            if not isfinite(price):
                return None
            result = dict(candidate)
            result["price"] = price
            return result
    return None


def _untraceable_numbers(reason: str, row: dict[str, Any]) -> list[str]:
    fact_text = json.dumps(row, ensure_ascii=False, default=str)
    allowed = {token.replace(",", "") for token in NUMBER_PATTERN.findall(fact_text)}
    bad: list[str] = []
    for match in NUMBER_PATTERN.finditer(reason):
        raw = match.group()
        token = raw.replace(",", "")
        if token in allowed:
            continue
        try:
            if "." not in token and float(token) <= ORDINAL_CEILING:
                continue
        except ValueError:
            pass
        bad.append(raw)
    return list(dict.fromkeys(bad))


def _is_fresh(packet: dict[str, Any], moment: datetime, max_age_minutes: int, reasons: list[str]) -> bool:
    as_of = _parse_datetime(packet.get("as_of"))
    if as_of is None:
        reasons.append("裁决包缺少可解析的 as_of")
        return False
    age = (moment - as_of).total_seconds() / 60.0
    if max_age_minutes >= 0 and age > max_age_minutes:
        reasons.append(f"裁决包已滞后 {age:.1f} 分钟，超过允许的 {max_age_minutes} 分钟")
        return False
    if age < -1.0:
        reasons.append("裁决包的 as_of 晚于校验时间，时间口径不一致")
        return False
    symbols = packet.get("symbols")
    if not isinstance(symbols, list) or any(not isinstance(row, dict) for row in symbols):
        reasons.append("裁决包 symbols 必须是对象数组")
        return False
    for row in symbols:
        intraday = row.get("intraday") or {}
        if row.get("is_current_session") is not True or intraday.get("is_current_session") is not True:
            reasons.append(f"{row.get('symbol')} 不是当前交易日分时")
            return False
    return True


def _rule_default(adjudication: Any, candidates: dict[str, Any]) -> dict[str, Any]:
    if isinstance(adjudication, dict):
        return {
            key: adjudication.get(f"rule_{key}")
            for key in ("entry", "stop", "target")
        }
    return {
        "entry": _first(candidates.get("entries")),
        "stop": _first(candidates.get("stops")),
        "target": _first(candidates.get("targets")),
    }


def _first(items: Any) -> Any:
    return items[0] if isinstance(items, list) and items else None


def _facts_root(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["facts"] if isinstance(payload.get("facts"), dict) else payload


def _symbol_rows(facts: dict[str, Any]) -> list[dict[str, Any]]:
    direct = facts.get("symbols")
    if isinstance(direct, list):
        return [row for row in direct if isinstance(row, dict)]
    for value in facts.values():
        if isinstance(value, dict) and isinstance(value.get("symbols"), list):
            return [row for row in value["symbols"] if isinstance(row, dict)]
    return []


def _parse_datetime(value: Any) -> datetime | None:
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            return parsed.astimezone(BEIJING_TZ).replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError):
        return None
