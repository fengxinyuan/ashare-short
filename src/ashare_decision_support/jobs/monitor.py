"""盘中监控任务：把 `monitor/` 那套接到持仓、板块与推送通道上。

`monitor/` 负责判断（时效闸门、跃迁触发、降频），这里负责取名单、建会话、决定
跑一轮还是跑一天，以及**把当日公告喂进去**——`risk_notice` 那条 WARN 触发器在
代码里一直存在，但在此之前没有任何调用点给它数据，等于结构上永不触发。

**个股那一半只盯两种票**：命令行显式给的，和持仓表里的。自选股与候选池已经删掉
（见 `state/positions.py`）——一份没人维护却仍被当默认输入的清单，会让每一轮监控
都在几十只你今天根本没看的票上发告警。

**板块监控不依赖名单。** 空仓的日子照样有话说，而那恰恰是最需要「现在该看哪个
方向」的一天。所以名单为空时不再整体 return，只跳过个股那一半。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.freshness import in_session
from ashare_decision_support.adapters.snapshot import default_snapshot_adapter
from ashare_decision_support.adapters.tickflow_intraday import build_intraday_adapter
from ashare_decision_support.artifacts import MONITOR_STATUS_LATEST_NAME, MORNING_LATEST_NAME
from ashare_decision_support.monitor import DEFAULT_LEVELS, MonitorSession, run_monitor_loop
from ashare_decision_support.monitor.sector_session import (
    SECTOR_EVERY,
    SectorSession,
    publish_sector_alerts,
)
from ashare_decision_support.monitor.sector_triggers import SectorPlanContext, SectorTriggerEngine
from ashare_decision_support.monitor.session import MONITOR_PERIOD
from ashare_decision_support.news import AkshareNewsSource, NewsCache
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier
from ashare_decision_support.notify.base import Notifier, publish
from ashare_decision_support.notify.render import (
    alert_title,
    render_alerts,
    render_feed_suspension,
)
from ashare_decision_support.schedule.timezone import beijing_naive_now, beijing_today
from ashare_decision_support.state import Positions
from ashare_decision_support.state.runtime import exclusive_run


@exclusive_run("monitor")
def run_monitor(
    adapter: Any,
    symbols: list[str] | None = None,
    period: str = MONITOR_PERIOD,
    once: bool = False,
    max_passes: int | None = None,
    alert_levels: tuple[str, ...] = DEFAULT_LEVELS,
    notifiers: list[Notifier] | None = None,
    intraday_adapter: Any | None = None,
    trade_date: date | None = None,
    now: datetime | None = None,
    news_source: Any | None = None,
    use_news: bool = True,
    news_cache_dir: str | None = None,
    output_dir: str | Path = "briefs",
    snapshot_adapter: Any | None = None,
    use_order_book: bool = True,
    sectors: bool = True,
    sector_every: int = SECTOR_EVERY,
    sector_session: Any | None = None,
) -> dict[str, Any]:
    today = trade_date or beijing_today()
    # 墙上时间，不带 tzinfo：`moment` 在这里**只**当比较用的时钟——它要减的是
    # 分钟线的时间戳（`feed.assess_feed` 算滞后），要喂的是 `in_session()`。
    # 数据源给的 K 线时间一律是 naive 的北京时间，拿 aware 的 `beijing_now()`
    # 去减会直接 `TypeError`（见 `schedule/timezone.py` 的模块注释）。
    # 这一行原来是 `beijing_now()`，于是**只要名单非空**——传了 `--symbols`，
    # 或者持仓表里有票——`monitor` 必崩；名单为空时走的是 `_sector_only`，
    # 不碰 `assess_feed`，所以持仓为空的那段时间它一直看着是好的。
    # 对外落地的时间戳不从这里来：`as_of` 取的是 `health.latest`（K 线时间）。
    moment = now or beijing_naive_now()
    channels = notifiers if notifiers is not None else [ConsoleNotifier()]

    # 显式给了标的就只盯它；否则盯持仓。没有第三种来源了。
    positions = Positions()
    watch = list(symbols) if symbols else positions.symbols()

    notes: list[str] = []
    sector_plan = SectorPlanContext()
    if sectors and sector_session is None:
        sector_plan, plan_notes = _load_morning_sector_plan(output_dir, today)
        notes.extend(plan_notes)
    sector = _build_sector_session(
        enabled=sectors,
        provided=sector_session,
        today=today,
        symbols=symbols,
        notes=notes,
        sector_plan=sector_plan,
    )

    if not watch:
        notes.append(
            "没有可盯的个股（未传代码、持仓表为空），本次只跑板块监控；"
            "登记持仓用 `ashare-ds position open <代码> --cost <成本价>`"
        )
        if sector is None:
            return {
                "trade_date": str(today),
                "watch": 0,
                "passes": 0,
                "alerts": 0,
                "notes": [*notes, "板块监控也未启用，本次没有任何可做的事"],
            }
        return _sector_only(sector, channels, today, moment, once, max_passes, sector_every, notes)

    source = intraday_adapter or build_intraday_adapter(adapter)
    session = MonitorSession(
        adapter=adapter,
        intraday_adapter=source,
        session_date=today,
        levels=alert_levels,
        period=period,
        snapshot_adapter=(
            snapshot_adapter
            if snapshot_adapter is not None
            else (default_snapshot_adapter() if use_order_book else None)
        ),
    )
    notes.extend(session.prime(watch))
    # 持仓在 prime 之后加载：已持仓的票要盯的是卖点（止损/目标/结构破坏/时间止损），
    # 未持仓的盯买点。两套判断的紧急程度不同，没有这一步 monitor 分不清。
    notes.extend(session.load_positions(positions))

    # `--no-news` 此前只是让 CLI 传 `news_source=None`，而这里一见 None 就自己造
    # 默认源——开关等于没接上。现在关掉就是真的不取公告。
    news = None
    if use_news:
        news = news_source if news_source is not None else _default_news_source(news_cache_dir)
        blocked, risk_notes = _refresh_risk(session, news, today)
    else:
        blocked, risk_notes = 0, ["已关闭公告采集（--no-news），本次监控不含突发风险公告告警"]
    notes.extend(risk_notes)

    if once or max_passes == 1:
        result = session.poll(watch, moment, in_session=in_session(moment))
        notes.extend(result.notes)
        # 走 `publish` 而不是逐个 `channel.send`：投递是副作用，一个渠道的契约
        # 违约（webhook 404、磁盘满）不该让这一轮监控整个抛出去。循环那一侧
        # 一直是这么做的，单轮这一侧此前不是——同一件事按调用路径有两种韧性。
        if result.alerts:
            publish(
                channels, alert_title(result.alerts), render_alerts(result.alerts, result.health)
            )
        elif not result.health.usable:
            publish(channels, "分时监控状态", render_feed_suspension(result.health))
        sector_summary = _sector_pass(sector, channels, moment, notes)
        summary = {
            "trade_date": str(today),
            "watch": len(watch),
            "passes": 1,
            "alerts": len(result.alerts),
            "risk_blocked": blocked,
            "feed_verdict": result.health.verdict,
            "feed_reason": result.health.reason,
            "as_of": result.health.latest.isoformat(timespec="seconds")
            if result.health.latest
            else None,
            "alert_list": [_alert_dict(item) for item in result.alerts],
            **sector_summary,
            "notes": notes,
        }
        file_notifier = next(
            (channel for channel in channels if isinstance(channel, FileNotifier)), None
        )
        if file_notifier is not None:
            status_path = file_notifier.write_payload(summary, MONITOR_STATUS_LATEST_NAME)
            if status_path is not None:
                summary["status_path"] = str(status_path)
        return summary

    stats = run_monitor_loop(
        session=session,
        # 每轮重读，这样盘中 `position open` 登记的新仓位立刻被盯上，不用重启监控。
        watch_symbols=lambda: list(symbols) if symbols else Positions().symbols(),
        notifiers=channels,
        max_passes=max_passes,
        # 盘中会新挂临时公告，所以不能只在开盘时读一次；但公告源是整日全量拉取，
        # 每轮都拉是浪费，走慢周期。
        refresh_risk=(lambda: _refresh_risk(session, news, today)[1]) if news is not None else None,
        sector_session=sector,
        sector_every=sector_every,
    )
    return {
        "trade_date": str(today),
        "watch": len(watch),
        "passes": stats.passes,
        "alerts": stats.alerts,
        "risk_blocked": blocked,
        "bad_passes": stats.bad_passes,
        "suspensions": stats.suspensions,
        "interval_seconds": stats.interval_seconds,
        "sector_passes": stats.sector_passes,
        "sector_alerts": stats.sector_alerts,
        "notes": notes + stats.notes,
    }


# 板块那一半


def default_sector_session(
    today: date, symbols: list[str] | None, sector_plan: SectorPlanContext | None = None
) -> Any:
    """默认板块会话。**单独一个函数，是为了让测试能把它掐掉。**

    构造本身不打网络（分类与全市场适配器都是懒加载），但第一次 `poll()` 会打
    56 页新浪扫描。测试里 `run_monitor(once=True)` 因此会静默地发一批真实请求——
    不会失败，只会让整套测试依赖一个第三方站点。`tests/conftest.py` 把这个函数
    补成返回 None，与 `default_snapshot_adapter` / `default_earnings_adapter`
    那几条同一个处置。
    """
    return SectorSession(
        session_date=today,
        engine=SectorTriggerEngine(plan=sector_plan),
        # 板块告警里「你盯着的票在这个板块里」那一行要每轮重读，理由同
        # `run_monitor_loop` 的 watch_symbols。
        watch_symbols=lambda: list(symbols) if symbols else Positions().symbols(),
    )


def _build_sector_session(
    enabled: bool,
    provided: Any,
    today: date,
    symbols: list[str] | None,
    notes: list[str],
    sector_plan: SectorPlanContext | None = None,
) -> Any | None:
    """构造失败只关掉板块这一半，不影响个股监控。

    分类源要读两个 HTTP 文件，拉不到时 `list_sectors()` 会抛 `SectorTaxonomyError`
    ——但那发生在第一次 poll，不是构造时。这里挡的是更早的失败（导入、状态目录）。
    """
    if provided is not None:
        return provided
    if not enabled:
        return None
    try:
        return default_sector_session(today, symbols, sector_plan=sector_plan)
    except Exception as exc:
        notes.append(f"板块监控初始化失败（{type(exc).__name__}: {exc}），本次只跑个股监控")
        return None


def _load_morning_sector_plan(
    output_dir: str | Path,
    trade_date: date | None = None,
) -> tuple[SectorPlanContext, list[str]]:
    """从盘前产物里抽取主线/观察板块，供盘中异动做关系标注。

    它只读最终事实文件，不重新跑盘前分析；缺失时返回空计划，让盘中监控保持独立。
    """
    path = Path(output_dir) / MORNING_LATEST_NAME
    if not path.exists():
        return SectorPlanContext(), []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return SectorPlanContext(), [f"盘前板块计划读取失败（{type(exc).__name__}: {exc}）"]
    if not isinstance(payload, dict):
        return SectorPlanContext(), ["盘前板块计划格式异常，盘中板块告警不标注盘前关系"]
    payload_date = payload.get("trade_date")
    if trade_date is not None and payload_date and str(payload_date) != str(trade_date):
        return SectorPlanContext(), [
            f"盘前板块计划归属 {payload_date}，与本次监控交易日 {trade_date} 不一致，已跳过"
        ]
    if payload.get("execution_ready") is False:
        return SectorPlanContext(), ["盘前板块计划标记为不可执行，盘中板块告警不沿用该计划"]

    primary = _sector_names(payload.get("sectors"))
    watch = (
        _sector_names(payload.get("theme_radar"))
        | _sector_names((payload.get("rotation_price") or {}).get("rising"))
        | _sector_names((payload.get("rotation_price") or {}).get("persistent"))
        | _sector_names((payload.get("turnover_migration") or {}).get("rising"))
    ) - primary
    notes: list[str] = []
    if primary or watch:
        notes.append(f"已载入盘前板块计划：主线 {len(primary)} 个，观察 {len(watch)} 个")
    return SectorPlanContext(frozenset(primary), frozenset(watch)), notes


def _sector_names(value: Any) -> set[str]:
    """宽松提取板块名，兼容 morning_latest 里的多种局部结构。"""
    names: set[str] = set()
    if isinstance(value, dict):
        for key in ("name", "sector", "sector_name"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                names.add(item.strip())
        if isinstance(value.get("sectors"), list):
            names.update(_sector_names(value["sectors"]))
        return names
    if not isinstance(value, list):
        return names
    for item in value:
        if isinstance(item, str) and item.strip():
            names.add(item.strip())
        elif isinstance(item, dict):
            names.update(_sector_names(item))
    return names


def _sector_pass(sector: Any, channels: list[Notifier], moment: datetime, notes: list[str]) -> dict:
    """单轮模式下的板块 pass。循环模式在 `monitor/loop.py:_run_sector_pass`，
    两者共用 `publish_sector_alerts`——标题与正文只有一份实现。"""
    if sector is None:
        return {}
    try:
        outcome = sector.poll(moment)
    except Exception as exc:
        notes.append(f"板块截面失败（{type(exc).__name__}: {exc}）")
        return {}
    notes.extend(outcome.notes or [])
    return {
        "sector_passes": 1,
        "sector_alerts": publish_sector_alerts(channels, outcome),
        "sector_reads": len(outcome.snapshot.reads) if outcome.snapshot else 0,
        "sector_alert_list": [_sector_alert_dict(item) for item in outcome.alerts],
    }


def _sector_only(
    sector: Any,
    channels: list[Notifier],
    today: date,
    moment: datetime,
    once: bool,
    max_passes: int | None,
    sector_every: int,
    notes: list[str],
) -> dict[str, Any]:
    """名单为空时的路径：只跑板块。

    单轮直接跑一次；循环模式借用 `run_monitor_loop` 的节奏与降频，个股那一侧传
    一个空名单——`MonitorSession.poll` 对空名单的处置本来就是「什么都不做，
    只报一条 note」，不会产生任何个股告警，也不会打分钟线适配器。
    """
    if once or max_passes == 1:
        summary = _sector_pass(sector, channels, moment, notes)
        return {
            "trade_date": str(today),
            "watch": 0,
            "passes": 0,
            "alerts": 0,
            **summary,
            "notes": notes,
        }

    stats = run_monitor_loop(
        session=_NullSession(),
        watch_symbols=list,
        notifiers=channels,
        max_passes=max_passes,
        sector_session=sector,
        sector_every=sector_every,
    )
    return {
        "trade_date": str(today),
        "watch": 0,
        "passes": stats.passes,
        "alerts": 0,
        "sector_passes": stats.sector_passes,
        "sector_alerts": stats.sector_alerts,
        "interval_seconds": stats.interval_seconds,
        "notes": notes + stats.notes,
    }


class _NullSession:
    """空名单时的个股侧占位。

    比给 `run_monitor_loop` 加一个 `session: MonitorSession | None` 参数干净：
    那会让循环里每一处用到 `result` 的地方都要判一次 None，而循环的主线本来就是
    个股。这里返回的健康度是「没有标的可取」，与真实的取数失败可区分。
    """

    def poll(self, watch, now, in_session=True):
        from ashare_decision_support.monitor.feed import assess_feed
        from ashare_decision_support.monitor.session import PassResult

        return PassResult(
            health=assess_feed({}, 0, now, None, in_session),
            notes=["没有可盯的个股，本轮只跑板块"],
        )


def _default_news_source(cache_dir: str | None) -> Any | None:
    try:
        return AkshareNewsSource(cache=NewsCache(cache_dir) if cache_dir else None)
    except RuntimeError:
        return None


def _refresh_risk(
    session: MonitorSession, news_source: Any | None, today: date
) -> tuple[int, list[str]]:
    """把当日公告里的否决项喂给监控会话。

    取不到公告不是错误，是降级：说清楚这一轮没有公告否决输入，然后继续跑。沉默地
    跑一个永远不触发的告警，比明说「这条输入缺了」糟糕得多。
    """
    if news_source is None or not hasattr(news_source, "fetch_announcements"):
        return 0, ["公告源不可用，本次监控不含突发风险公告告警"]
    try:
        announcements, notes = news_source.fetch_announcements(today)
    except Exception as exc:
        return 0, [f"公告拉取失败（{type(exc).__name__}），本次监控不含突发风险公告告警"]

    blocked = session.refresh_risk(announcements)
    messages = list(notes or [])
    if blocked:
        messages.append(f"当日公告命中否决项 {blocked} 只，命中的标的会推一条风险告警")
    return blocked, messages


def _alert_dict(alert) -> dict:
    return {
        "symbol": alert.symbol,
        "name": alert.name,
        "level": alert.level,
        "trigger": alert.trigger,
        "headline": alert.headline,
        "detail": alert.detail,
        "as_of": alert.as_of.isoformat(timespec="seconds") if alert.as_of else None,
        "lag_minutes": alert.lag_minutes,
        "holding": alert.holding,
    }


def _sector_alert_dict(alert) -> dict:
    return {
        "sector": alert.sector,
        "sector_level": alert.sector_level,
        "level": alert.level,
        "trigger": alert.trigger,
        "headline": alert.headline,
        "detail": alert.detail,
        "rank": alert.rank,
        "excess_pct": alert.excess_pct,
        "watch_hits": [list(item) for item in alert.watch_hits],
        "as_of": alert.as_of.isoformat(timespec="seconds") if alert.as_of else None,
    }
