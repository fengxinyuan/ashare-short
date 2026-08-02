"""The intraday monitoring loop.

Owns three things the session does not: *when* to poll, what to do when the
feed degrades, and where alerts go.

Back-off is the part that matters operationally. Sustained polling against
Sina has not been rate-limit tested (measured: 40 requests in a burst, clean;
a real session is ~145 passes × N symbols). So instead of assuming it holds,
the loop watches its own failure rate and steps the interval down rather than
hammering a source that is already refusing — and it says so each time, so a
silent degradation is impossible.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock_time
from typing import Any, Callable

from ashare_decision_support.adapters.freshness import in_session
from ashare_decision_support.monitor.session import MonitorSession
from ashare_decision_support.monitor.triggers import Alert
from ashare_decision_support.notify.base import Notifier
from ashare_decision_support.notify.render import render_alerts, render_feed_suspension

# Poll intervals in seconds, from normal to most conservative. The loop walks
# this ladder down on sustained failure and back up on sustained success.
INTERVAL_LADDER: tuple[int, ...] = (120, 300, 600)

# Fraction of symbols that must fail in a pass for it to count as a bad pass.
BAD_PASS_FAILURE_RATIO = 0.5

# Consecutive bad passes before stepping down, and clean passes before stepping up.
STEP_DOWN_AFTER = 2
STEP_UP_AFTER = 5

MONITOR_START = clock_time(9, 35)
MONITOR_END = clock_time(14, 55)

# Passes between announcement refreshes. The feed is a whole-day bulk pull, so
# doing it every pass is wasteful; but 临时公告 do post during the session, so
# reading it once at 09:35 and never again misses exactly the ones that matter.
# 15 passes at the fastest interval is half an hour.
RISK_REFRESH_EVERY = 15


@dataclass
class LoopStats:
    passes: int = 0
    alerts: int = 0
    bad_passes: int = 0
    suspensions: int = 0
    interval_seconds: int = INTERVAL_LADDER[0]
    notes: list[str] = field(default_factory=list)


def run_monitor_loop(
    session: MonitorSession,
    watch_symbols: Callable[[], list[str]],
    candidate_symbols: Callable[[], list[str]],
    notifiers: list[Notifier],
    now_fn: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
    max_passes: int | None = None,
    until: clock_time = MONITOR_END,
    stop_when: Callable[[], bool] | None = None,
    refresh_risk: Callable[[], list[str]] | None = None,
    refresh_risk_every: int = RISK_REFRESH_EVERY,
) -> LoopStats:
    """Poll until `until`, or for `max_passes` passes (tests use the latter)."""
    stats = LoopStats()
    level = 0
    bad_streak = 0
    good_streak = 0
    suspended = False

    while True:
        if max_passes is not None and stats.passes >= max_passes:
            break
        if stop_when is not None and stop_when():
            stats.notes.append("收到停止信号")
            break

        moment = now_fn()
        if max_passes is None and moment.time() > until:
            break

        active = in_session(moment)
        try:
            result = session.poll(watch_symbols(), candidate_symbols(), moment, in_session=active)
        except Exception as exc:
            # A pass that raises is a bad pass, not a dead loop. The most
            # likely cause is the network, and the next pass may well succeed.
            stats.passes += 1
            stats.bad_passes += 1
            bad_streak += 1
            good_streak = 0
            stats.notes.append(f"第 {stats.passes} 轮异常：{type(exc).__name__}: {exc}")
            level, bad_streak = _maybe_step_down(level, bad_streak, stats)
            sleep(INTERVAL_LADDER[level])
            continue

        stats.passes += 1
        stats.interval_seconds = INTERVAL_LADDER[level]

        if (
            refresh_risk is not None
            and refresh_risk_every > 0
            and stats.passes % refresh_risk_every == 0
        ):
            try:
                stats.notes.extend(refresh_risk() or [])
            except Exception as exc:
                # 公告刷新失败只损失这一次风险输入，不能拖垮监控循环本身。
                stats.notes.append(f"公告刷新失败：{type(exc).__name__}: {exc}")

        if result.health.usable:
            if suspended:
                _publish(notifiers, "分时监控恢复", render_feed_suspension(result.health))
                suspended = False
            if result.alerts:
                stats.alerts += len(result.alerts)
                _publish(notifiers, _alert_title(result.alerts), render_alerts(result.alerts, result.health))
        elif result.verdict_changed and not suspended and active:
            # One message when the gate closes, not one per pass.
            _publish(notifiers, "分时监控挂起", render_feed_suspension(result.health))
            suspended = True
            stats.suspensions += 1

        if result.health.failure_ratio >= BAD_PASS_FAILURE_RATIO:
            stats.bad_passes += 1
            bad_streak += 1
            good_streak = 0
        else:
            good_streak += 1
            bad_streak = 0

        level, bad_streak = _maybe_step_down(level, bad_streak, stats)
        level, good_streak = _maybe_step_up(level, good_streak, stats)

        if max_passes is not None and stats.passes >= max_passes:
            break
        sleep(INTERVAL_LADDER[level])

    stats.interval_seconds = INTERVAL_LADDER[level]
    return stats


def _maybe_step_down(level: int, bad_streak: int, stats: LoopStats) -> tuple[int, int]:
    if bad_streak < STEP_DOWN_AFTER:
        return level, bad_streak
    if level + 1 >= len(INTERVAL_LADDER):
        stats.notes.append(
            f"连续 {bad_streak} 轮取数失败，已降至最低频 {INTERVAL_LADDER[level]} 秒，仍未恢复"
        )
        return level, 0
    stats.notes.append(
        f"连续 {bad_streak} 轮取数失败，轮询间隔 {INTERVAL_LADDER[level]} → {INTERVAL_LADDER[level + 1]} 秒"
    )
    return level + 1, 0


def _maybe_step_up(level: int, good_streak: int, stats: LoopStats) -> tuple[int, int]:
    if level == 0 or good_streak < STEP_UP_AFTER:
        return level, good_streak
    stats.notes.append(
        f"连续 {good_streak} 轮正常，轮询间隔 {INTERVAL_LADDER[level]} → {INTERVAL_LADDER[level - 1]} 秒"
    )
    return level - 1, 0


def _alert_title(alerts: list[Alert]) -> str:
    action = sum(1 for alert in alerts if alert.level == "ACTION")
    if action:
        return f"盘中告警 {action} 条 ACTION / 共 {len(alerts)} 条"
    return f"盘中告警 {len(alerts)} 条"


def _publish(notifiers: list[Notifier], title: str, content: str) -> None:
    for notifier in notifiers:
        try:
            notifier.send(title, content)
        except Exception:
            # A notifier contract violation must not stop monitoring.
            continue


def monitor_window(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, MONITOR_START),
        datetime.combine(day, MONITOR_END),
    )
