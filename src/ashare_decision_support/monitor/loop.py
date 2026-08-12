"""The intraday monitoring loop.

Owns three things the session does not: *when* to poll, what to do when the
feed degrades, and where alerts go.

Back-off is the part that matters operationally. Sustained polling against
Sina has not been rate-limit tested (measured: 40 requests in a burst, clean;
a real session is ~145 passes × N symbols). So instead of assuming it holds,
the loop watches its own failure rate and steps the interval down rather than
hammering a source that is already refusing — and it says so each time, so a
silent degradation is impossible.

板块截面挂在同一个循环上，每 `SECTOR_EVERY` 轮做一次。**同一个进程、同一套
降频**：全市场扫描打的是同一个新浪域名，另起一个进程去打它，等于两边各自
统计失败率、各降各的频，而谁都不知道对方在打。见 `monitor/sector_session.py`。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as clock_time
from typing import Any

from ashare_decision_support.adapters.freshness import in_session
from ashare_decision_support.monitor.sector_session import publish_sector_alerts
from ashare_decision_support.monitor.session import MonitorSession
from ashare_decision_support.notify.base import Notifier, publish
from ashare_decision_support.notify.render import (
    alert_title,
    render_alerts,
    render_feed_suspension,
)

# Poll intervals in seconds, from normal to most conservative. The loop walks
# this ladder down on sustained failure and back up on sustained success.
INTERVAL_LADDER: tuple[int, ...] = (120, 300, 600)

# Fraction of symbols that must fail in a pass for it to count as a bad pass.
BAD_PASS_FAILURE_RATIO = 0.5

# Consecutive bad passes before stepping down, and clean passes before stepping up.
STEP_DOWN_AFTER = 2
STEP_UP_AFTER = 5

# 收盘前 5 分钟停。开始时刻由 `schedule/timetable.py` 决定——那里是时间表的
# 唯一权威，这里再写一个 09:35 只会多一处可以和它对不上的地方。
MONITOR_END = clock_time(14, 55)

# Passes between announcement refreshes. The feed is a whole-day bulk pull, so
# doing it every pass is wasteful; but 临时公告 do post during the session, so
# reading it once at 09:35 and never again misses exactly the ones that matter.
# 15 passes at the fastest interval is half an hour.
RISK_REFRESH_EVERY = 15

# 每多少轮做一次板块截面。板块 pass 是一次全市场扫描（56 页并发），比个股那轮贵，
# 而且板块排名在两分钟里的移动基本是噪声。默认见 `monitor/sector_session.py`。
SECTOR_EVERY = 5


@dataclass
class LoopStats:
    passes: int = 0
    alerts: int = 0
    bad_passes: int = 0
    suspensions: int = 0
    interval_seconds: int = INTERVAL_LADDER[0]
    sector_passes: int = 0
    sector_alerts: int = 0
    notes: list[str] = field(default_factory=list)


def run_monitor_loop(
    session: MonitorSession,
    watch_symbols: Callable[[], list[str]],
    notifiers: list[Notifier],
    now_fn: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
    max_passes: int | None = None,
    until: clock_time = MONITOR_END,
    stop_when: Callable[[], bool] | None = None,
    refresh_risk: Callable[[], list[str]] | None = None,
    refresh_risk_every: int = RISK_REFRESH_EVERY,
    sector_session: Any | None = None,
    sector_every: int = SECTOR_EVERY,
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
            result = session.poll(watch_symbols(), moment, in_session=active)
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
                publish(notifiers, "分时监控恢复", render_feed_suspension(result.health))
                suspended = False
            if result.alerts:
                stats.alerts += len(result.alerts)
                publish(notifiers, alert_title(result.alerts), render_alerts(result.alerts, result.health))
        elif result.verdict_changed and not suspended and active:
            # One message when the gate closes, not one per pass.
            publish(notifiers, "分时监控挂起", render_feed_suspension(result.health))
            suspended = True
            stats.suspensions += 1

        # 板块 pass。**只在盘中做**：休市时全市场行情不动，落进序列的是一串重复
        # 截面，会把「较上轮」那一列变成噪声。
        #
        # 它不受个股那条时效闸门（`result.health.usable`）约束，因为两者读的是
        # 完全不同的源：个股走分钟线适配器，板块走新浪全市场快照。分钟线停更
        # 不代表快照也停了，用前者去否决后者会在最该看板块的那天把它关掉。
        if (
            sector_session is not None
            and sector_every > 0
            and active
            and (stats.passes % sector_every == 1 or sector_every == 1)
        ):
            _run_sector_pass(sector_session, notifiers, moment, stats)

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


def _run_sector_pass(
    sector_session: Any, notifiers: list[Notifier], moment: datetime, stats: LoopStats
) -> None:
    """一次板块截面 + 投递。**任何失败都只记一条 note。**

    板块是「先看哪里」，个股是「买卖点」。让前者有资格中断后者，等于用旁证
    决定主线跑不跑——同 `notify/base.py:publish` 对投递通道的处置。
    """
    try:
        outcome = sector_session.poll(moment)
    except Exception as exc:
        stats.notes.append(f"板块截面第 {stats.passes} 轮异常：{type(exc).__name__}: {exc}")
        return
    stats.sector_passes += 1
    stats.notes.extend(outcome.notes or [])
    stats.sector_alerts += publish_sector_alerts(notifiers, outcome)


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
