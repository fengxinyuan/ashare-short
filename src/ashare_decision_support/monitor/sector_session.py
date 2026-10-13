"""板块监控的一轮：扫描、落盘、比对当日基线并产出状态跃迁。

与个股监控共用 `monitor/loop.py` 的轮询、降频和通知通道。
全市场板块扫描按 `SECTOR_EVERY` 降低采样频率，会话负责单轮计算与状态保存。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.monitor.sector_triggers import (
    DEFAULT_LEVELS,
    SectorAlert,
    SectorTriggerEngine,
    load_sector_state,
)
from ashare_decision_support.notify.base import Notifier, publish
from ashare_decision_support.notify.render import render_sector_alerts, sector_alert_title
from ashare_decision_support.schedule.clock import is_continuous_session
from ashare_decision_support.schedule.timezone import beijing_naive_now
from ashare_decision_support.sectors import intraday
from ashare_decision_support.sectors.intraday import SectorSnapshot
from ashare_decision_support.state.workflow import record_event

# 每五轮个股监控采样一次板块；默认个股每两分钟一轮，板块约十分钟一次。
# CLI、任务入口和监控循环共用此默认值。
SECTOR_EVERY = 5


@dataclass
class SectorPassResult:
    snapshot: SectorSnapshot | None = None
    alerts: list[SectorAlert] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.snapshot is not None and bool(self.snapshot.reads)


def publish_sector_alerts(notifiers: list[Notifier], outcome: SectorPassResult) -> int:
    """投递一轮板块告警并返回告警条数；`publish` 隔离单个通知渠道的失败。"""
    if not outcome.alerts:
        return 0
    publish(
        notifiers,
        sector_alert_title(outcome.alerts),
        render_sector_alerts(outcome.alerts, outcome.snapshot),
    )
    return len(outcome.alerts)


class SectorSession:
    """一天一个实例。持有分类（自带缓存）、当日序列文件与告警状态。"""

    def __init__(
        self,
        session_date: date,
        taxonomies: dict[str, Any] | None = None,
        adapter: Any | None = None,
        levels: tuple[str, ...] = DEFAULT_LEVELS,
        engine: SectorTriggerEngine | None = None,
        state: Any | None = None,
        store_root: Path | None = None,
        watch_symbols: Any | None = None,
    ) -> None:
        self.session_date = session_date
        self.taxonomies = taxonomies if taxonomies is not None else intraday.default_taxonomies()
        self.adapter = adapter
        self.engine = engine or SectorTriggerEngine(levels=levels)
        self.state = state if state is not None else load_sector_state(session_date)
        self.path = intraday.store_path(str(session_date), root=store_root)
        # 一天一次的清理，挂在会话构造上而不是每轮 poll 上：这是 IO，而且
        # 板块序列是状态目录里最大的一类文件（一天约 3MB），没人删的话一年 750MB。
        intraday.prune_history(
            root=store_root,
            protected_dates={str(session_date)},
        )
        # 每轮重读，这样盘中 `position open/close` 立刻影响「你盯着的票在这个板块里」
        # 那一行——与 `jobs/monitor.py` 里 watch_symbols 每轮重读同一个理由。
        self._watch_symbols = watch_symbols

    def poll(self, now: datetime | None = None) -> SectorPassResult:
        """跑一轮。**任何一步失败都降级成 notes，不抛给循环。**

        板块是旁证不是主线：它挂了不该让个股监控那一轮跟着停。同
        `notify/base.py:publish` 的处置。
        """
        moment = now or beijing_naive_now()
        if not is_continuous_session(moment):
            return self._record_pass(
                SectorPassResult(notes=["当前不在连续竞价时段，跳过全市场板块扫描"]),
                moment,
            )
        try:
            snapshot = intraday.take_snapshot(
                self.taxonomies,
                adapter=self.adapter,
                now=moment,
                watched_symbols=self._watched_symbols(),
            )
        except Exception as exc:
            return self._record_pass(
                SectorPassResult(notes=[f"板块截面取数失败（{type(exc).__name__}: {exc}）"]),
                moment,
            )

        # 基线在**落盘之前**读：文件为空时这一轮自己就是当日第一条，
        # 落完再读会把「较开盘」变成「较自己」（恒等于 0，永远不触发）。
        baseline = intraday.load_first(self.path) or snapshot
        try:
            intraday.append(snapshot, self.path)
        except (OSError, TypeError, ValueError) as exc:
            # 落盘失败只损失序列，不该丢掉这一轮已经算出来的跃迁。
            snapshot.notes.append(f"板块序列落盘失败（{type(exc).__name__}），本轮读数只在内存里")

        alerts, notes = self.engine.evaluate(snapshot, baseline, self.state, moment)
        try:
            self.state.save()
        except (OSError, TypeError, ValueError) as exc:
            notes.append(f"板块告警状态落盘失败（{type(exc).__name__}），可能重复推送")
        return self._record_pass(
            SectorPassResult(snapshot=snapshot, alerts=alerts, notes=list(snapshot.notes) + notes),
            moment,
        )

    def _record_pass(self, result: SectorPassResult, now: datetime) -> SectorPassResult:
        try:
            record_event(
                self.session_date,
                "sector_poll",
                {
                    "snapshot_at": result.snapshot.as_of if result.snapshot else None,
                    "snapshot_ref": str(self.path.resolve()),
                    "tradable": result.snapshot.tradable if result.snapshot else False,
                    "alerts": [asdict(alert) for alert in result.alerts],
                    "notes": list(result.notes),
                },
                now,
            )
        except (OSError, TypeError, ValueError) as exc:
            result.notes.append(
                f"板块过程记录保存失败（{type(exc).__name__}），本轮告警保留，复盘证据缺失"
            )
        return result

    def _watched_symbols(self) -> set[str]:
        if self._watch_symbols is None:
            return set()
        try:
            symbols = (
                self._watch_symbols() if callable(self._watch_symbols) else self._watch_symbols
            )
        except Exception:
            return set()
        return {str(item).partition(".")[0] for item in symbols or [] if item}
