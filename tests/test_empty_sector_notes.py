"""主攻板块没出候选时，说明是在哪一道被挡掉的。"""

from __future__ import annotations

from types import SimpleNamespace

from ashare_decision_support.jobs.morning_select import _empty_sector_notes
from ashare_decision_support.models.enums import Playbook
from ashare_decision_support.strategy.playbook import PlaybookMatch, SetupVerdict


def _match(ok: bool = False, blocked=(), reject: str | None = None) -> PlaybookMatch:
    return PlaybookMatch(
        trend=None,
        matched=[SetupVerdict(Playbook.PULLBACK, True, [], [])] if ok else [],
        rejected=[SetupVerdict(Playbook.PULLBACK, False, [reject], [])] if reject else [],
        blocked_by_regime=list(blocked),
    )


def test_regime_blocked_breakouts_are_named_for_the_empty_sector() -> None:
    sector_of = {"A": "元件", "B": "元件", "C": "元件", "D": "银行"}
    coarse = SimpleNamespace(
        matches={
            "A": _match(blocked=[Playbook.BREAKOUT]),
            "B": _match(blocked=[Playbook.BREAKOUT]),
            "C": _match(reject="尚未回调（none），不是回踩位置"),
            "D": _match(ok=True),
        }
    )
    notes = _empty_sector_notes(coarse, sector_of, [{"sector": "银行"}], list(sector_of))

    assert len(notes) == 1
    line = notes[0]
    assert line.startswith("主攻板块无候选 元件：入围 3 只，0 只满足入场判据")
    assert "2 只的「突破」打法被情绪周期挡掉" in line
    assert "最常见否决：不是回踩位置（1 只）" in line


def test_sector_with_a_pick_or_no_members_in_scope_is_silent() -> None:
    sector_of = {"A": "元件", "D": "银行"}
    coarse = SimpleNamespace(matches={"A": _match(ok=True), "D": _match(ok=True)})
    assert _empty_sector_notes(coarse, sector_of, [{"sector": "元件"}], ["A"]) == []


def test_reject_reason_key_drops_measurements_so_rules_aggregate() -> None:
    from ashare_decision_support.jobs.morning_select import _reject_reason_key

    assert _reject_reason_key("60 日分位 0.50（<0.60），尚未到突破位") == "尚未到突破位"
    assert _reject_reason_key("60 日分位 0.31（<0.60），尚未到突破位") == "尚未到突破位"
    assert _reject_reason_key("尚未回调（none），不是回踩位置") == "不是回踩位置"
    assert _reject_reason_key("无量比读数，突破无法确认") == "突破无法确认"
    assert _reject_reason_key("量能数据不足") == "量能数据不足"
