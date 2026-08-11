"""共享的仓位决策策略。

同一条规则此前分别写在 `jobs/morning.py` 与 `jobs/post_close.py`：

- 系统建议值与情绪周期上限取 `min`
- 手工值仍然优先
- 手工值高于周期上限必须显式报警，不能静默放行

收成一处之后，两条链路不会再各自漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PositionSizingPlan:
    effective_factor: float
    capped_suggested_factor: float
    suggested_factor: float | None = None
    manual_factor: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def resolve_position_sizing(
    *,
    cycle_label: str,
    cycle_cap: float,
    suggested_factor: float | None = None,
    manual_factor: float | None = None,
) -> PositionSizingPlan:
    """统一仓位决策：先给系统建议值封顶，再决定是否用手工值覆盖。"""
    baseline = suggested_factor if suggested_factor is not None else cycle_cap
    capped = min(baseline, cycle_cap)
    notes: list[str] = []

    if suggested_factor is not None and capped < suggested_factor:
        notes.append(
            f"情绪周期 {cycle_label} 的仓位上限 {cycle_cap:.2f} "
            f"压低了系统建议值 {suggested_factor:.2f} → {capped:.2f}"
        )

    effective = manual_factor if manual_factor is not None else capped
    if manual_factor is not None:
        reference = (
            f"系统建议 {capped:.2f}" if suggested_factor is not None else f"周期上限 {cycle_cap:.2f}"
        )
        notes.append(f"仓位系数使用手工值 {manual_factor:.2f}（{reference}）")
        if manual_factor > cycle_cap:
            notes.append(
                f"⚠️ 手工仓位 {manual_factor:.2f} 高于 {cycle_label} 档上限 "
                f"{cycle_cap:.2f}——按你的设定执行，但这是逆周期加仓"
            )

    return PositionSizingPlan(
        effective_factor=effective,
        capped_suggested_factor=capped,
        suggested_factor=suggested_factor,
        manual_factor=manual_factor,
        notes=tuple(notes),
    )
