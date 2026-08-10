"""板块监控的一轮：扫描 → 落盘 → 与当日序列比对 → 产出跃迁。

和 `monitor/session.py` 的分工完全对称：这里管**一轮做什么**，`monitor/loop.py`
管**什么时候做**。分开的理由也一样——轮询节奏、降频、投递是运行时问题，
截面计算与跃迁判定是逻辑问题，混在一起就没法在没有网络的情况下测后者。

## 为什么挂在个股监控的同一个循环里

全市场扫描是 56 页并发打新浪，一轮不便宜。而 `monitor/loop.py` 开头就写着
「Sustained polling against Sina has not been rate-limit tested」——个股监控本身
已经在打这个源。**再起第二个进程去打同一个源，两边谁都不知道对方的失败率**，
降频阶梯也就各降各的。所以板块 pass 走同一个循环，每 `SECTOR_EVERY` 轮做一次，
共用那套降频。

板块的变化本来也不需要两分钟一看：一个板块的排名在两分钟里的移动基本是噪声。
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
from ashare_decision_support.schedule.timezone import beijing_naive_now
from ashare_decision_support.sectors import intraday
from ashare_decision_support.sectors.intraday import SectorSnapshot

# 个股监控每跑这么多轮，做一次板块截面。个股 2 分钟一轮，所以默认约 10 分钟。
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
    """投递一轮板块结果，返回推了几条。

    **一份实现，两个调用点。** 循环模式（`monitor/loop.py`）与单轮模式
    （`jobs/monitor.py`）此前各写了一份标题 + 渲染 + 逐通道发送——和
    `notify/render.py:alert_title` 当初被收口时是同一个问题：两份实现各自演化，
    结果是同一套系统按调用路径给出不同措辞的通知。

    走 `publish` 而不是自己 `for channel in channels: channel.send(...)`：
    投递是副作用，一个渠道的 webhook 挂了不该让监控这一轮跟着抛。
    """
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
        self.pruned = intraday.prune_history(root=store_root)
        # 每轮重读，这样盘中 `watchlist add` 立刻影响「你盯着的票在这个板块里」
        # 那一行——与 `jobs/monitor.py` 里 watch_symbols 每轮重读同一个理由。
        self._watch_symbols = watch_symbols

    def poll(self, now: datetime | None = None) -> SectorPassResult:
        """跑一轮。**任何一步失败都降级成 notes，不抛给循环。**

        板块是旁证不是主线：它挂了不该让个股监控那一轮跟着停。同
        `notify/base.py:publish` 的处置。
        """
        moment = now or beijing_naive_now()
        try:
            snapshot = intraday.take_snapshot(
                self.taxonomies,
                adapter=self.adapter,
                now=moment,
                watchlist=self._watchlist(),
            )
        except Exception as exc:
            return SectorPassResult(notes=[f"板块截面取数失败（{type(exc).__name__}: {exc}）"])

        # 基线在**落盘之前**读：文件为空时这一轮自己就是当日第一条，
        # 落完再读会把「较开盘」变成「较自己」（恒等于 0，永远不触发）。
        baseline = intraday.load_first(self.path) or snapshot
        try:
            intraday.append(snapshot, self.path)
        except OSError as exc:
            # 落盘失败只损失序列，不该丢掉这一轮已经算出来的跃迁。
            snapshot.notes.append(f"板块序列落盘失败（{type(exc).__name__}），本轮读数只在内存里")

        alerts, notes = self.engine.evaluate(snapshot, baseline, self.state, moment)
        try:
            self.state.save()
        except OSError as exc:
            notes.append(f"板块告警状态落盘失败（{type(exc).__name__}），可能重复推送")
        return SectorPassResult(snapshot=snapshot, alerts=alerts, notes=list(snapshot.notes) + notes)

    def _watchlist(self) -> set[str]:
        if self._watch_symbols is None:
            return set()
        try:
            symbols = self._watch_symbols() if callable(self._watch_symbols) else self._watch_symbols
        except Exception:
            return set()
        return {str(item).partition(".")[0] for item in symbols or [] if item}
