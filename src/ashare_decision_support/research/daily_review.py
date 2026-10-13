"""从本地计划、监控和新闻证据还原一天；不取行情、不推断成交、不自动调参。"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from ashare_decision_support.artifacts import MORNING_LATEST_NAME
from ashare_decision_support.journal import Journal
from ashare_decision_support.journal.records import VERIFIABLE_KINDS, record_key
from ashare_decision_support.monitor.feed import DEGRADED_LAG_MINUTES
from ashare_decision_support.monitor.triggers import POLICIES
from ashare_decision_support.news.store import DEFAULT_RELATIVE_PATH, NewsStore
from ashare_decision_support.numeric import finite
from ashare_decision_support.research.sector_confirmation import build_sector_confirmation
from ashare_decision_support.research.sector_forecast import build_sector_forecast
from ashare_decision_support.schedule.clock import (
    AFTERNOON_CLOSE,
    MORNING_OPEN,
    is_continuous_session,
)
from ashare_decision_support.schedule.timezone import beijing_naive_now, to_beijing_naive
from ashare_decision_support.sectors.intraday import SectorSnapshot, load_history, store_path
from ashare_decision_support.signals.series import parse_bar_date, parse_bar_datetime
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import read_json_object
from ashare_decision_support.state.workflow import read_events, read_plans
from ashare_decision_support.symbols import normalize_symbol

SCHEMA = "ashare-daily-review-v1"
NEWS_LIMIT = 2000
ENTRY_TRIGGERS = {"entry_zone", "entry_confirmed", "breakout_confirmed"}
EXIT_TRIGGERS = {"stop_break", "target_hit", "structure_break", "time_stop", "giveback"}


def build_daily_review(
    trade_date: date,
    output_dir: str | Path = "briefs",
    *,
    now: datetime | None = None,
    journal: Journal | None = None,
    news_store: NewsStore | None = None,
    store_root: Path | None = None,
) -> dict[str, Any]:
    """按北京时间截止时点回放；历史回填和无时点旧计划只能作事后对照。"""
    moment = to_beijing_naive(now or beijing_naive_now())
    if trade_date > moment.date():
        raise ValueError("每日复盘日期不能晚于当前北京时间")
    cutoff = min(moment, datetime.combine(trade_date, time.max))
    notes: list[str] = []
    plans = read_plans(output_dir, trade_date, until=cutoff, notes=notes)
    preopen = [
        row
        for row in plans
        if parse_bar_datetime(row["generated_at"]) <= datetime.combine(trade_date, MORNING_OPEN)
    ]
    baseline = preopen[0] if preopen else (plans[0] if plans else None)
    if baseline is None:
        baseline = _legacy_plan(output_dir, trade_date, cutoff, notes)
    if not preopen:
        notes.append("缺少可核验生成时点的原始盘前计划，后续对照不能证明盘前预测正确或错误")

    events = read_events(trade_date, until=cutoff, notes=notes)
    for event in events:
        event["plan_id"] = _plan_id_at(plans, parse_bar_datetime(event["at"]))
    path = store_path(str(trade_date), root=store_root)
    all_snapshots = load_history(path, notes)
    snapshots = [
        row for row in all_snapshots if row.as_of.date() == trade_date and row.as_of <= cutoff
    ]
    if len(snapshots) != len(all_snapshots):
        notes.append("板块序列中其他日期或截止时点之后的截面已排除")
    confirmation = _confirm(baseline, snapshots, cutoff, path, bool(preopen), notes)
    book = journal or Journal()
    decisions = _decisions(book, trade_date, cutoff, notes)
    candidates, timeline, coverage = _candidate_review(
        plans or ([baseline] if baseline else []), events, decisions, notes
    )
    news = _news_review(news_store, trade_date, cutoff, plans, snapshots, path, notes)
    timeline.extend(
        {**row, "kind": "news", "at": row["first_seen_at"], "headline": row["title"]}
        for row in news
    )
    timeline.extend(
        {
            "kind": "plan",
            "at": row["generated_at"],
            "plan_id": row["plan_id"],
            "headline": f"计划第 {row['plan_revision']} 版",
            "source_ref": row["source_ref"],
        }
        for row in plans
    )
    timeline.sort(key=lambda row: (parse_bar_datetime(row["at"]), row.get("kind", "")))
    if not events:
        notes.append("没有当天过程日志，无法判断是否持续监控、是否漏报或是否触发过条件")
    if not snapshots:
        notes.append("没有截止时点内的板块截面，板块方向仍待验证")
    elif cutoff.time() >= AFTERNOON_CLOSE and snapshots[-1].as_of.time() < AFTERNOON_CLOSE:
        notes.append(
            f"板块结论只截至 {snapshots[-1].as_of:%H:%M}，没有收盘截面；常规监控于14:55结束"
        )
    if cutoff.time() < AFTERNOON_CLOSE:
        notes.append("当日尚未收盘，本报告为截至当前时点的阶段复盘")
    notes.extend(
        [
            "告警记录表示系统生成过条件提示；未记录投递回执，也不能据此推断用户成交、仓位变化或收益。",
            "轮询记录只覆盖采样时点；未出现告警不等于全天未触发，也不能证明没有漏报。",
            "新闻关联与后续价格仅作时间对照，不证明消息导致涨跌；新闻分类是语料库当前标签，不能倒推当时已知标签。",
            "事件所关联的计划是当时已归档的最新背景，不表示监控重新加载或执行了该计划；监控依据仍是实时结构和登记持仓。",
        ]
    )
    next_checks = _next_checks(candidates, confirmation, bool(preopen))
    return {
        "schema": SCHEMA,
        "kind": "daily_review",
        "trade_date": str(trade_date),
        "as_of": cutoff.isoformat(timespec="microseconds"),
        "generated_at": moment.isoformat(timespec="microseconds"),
        "original_plan_available": bool(preopen),
        "original_plan_id": preopen[0]["plan_id"] if preopen else None,
        "strategy_version": (baseline or {}).get("strategy_version"),
        "plans": [_plan_summary(row) for row in plans or ([baseline] if baseline else [])],
        "decisions": decisions,
        "sector_confirmation": confirmation,
        "candidates": candidates,
        "news": news,
        "timeline": timeline,
        "process_records": [
            {key: row.get(key) for key in ("at", "kind", "plan_id", "source_ref", "notes")}
            for row in events
        ],
        "coverage": {
            **coverage,
            "sector_snapshots": len(snapshots),
            "sector_first_at": snapshots[0].as_of.isoformat() if snapshots else None,
            "sector_last_at": snapshots[-1].as_of.isoformat() if snapshots else None,
            "news_items": len(news),
        },
        "counts": {
            "candidates": len(candidates),
            "observed_candidates": sum(row["observations"] > 0 for row in candidates),
            "alerts": sum(row["kind"] == "alert" for row in timeline),
            "sector_statuses": dict(
                Counter(row["status"] for row in confirmation.get("results", []))
            ),
        },
        "next_checks": next_checks,
        "data_notes": list(dict.fromkeys(notes)),
    }


def _legacy_plan(
    output_dir: str | Path, day: date, cutoff: datetime, notes: list[str]
) -> dict | None:
    path = Path(output_dir) / MORNING_LATEST_NAME
    payload = read_json_object(path)
    if not payload:
        if path.exists():
            notes.append("最近晨报损坏或为空，无法用于本日复盘")
        return None
    if parse_bar_date(payload.get("trade_date")) != day:
        notes.append("最近晨报与复盘日期不同，未用其他日期的计划代替")
        return None
    stamp = parse_bar_datetime(payload.get("generated_at"))
    if stamp and stamp > cutoff:
        notes.append("最近晨报在截止时点之后生成，不能回填为当时计划")
        return None
    notes.append("仅有可覆盖的最近晨报，缺少不可变计划档案；保留事后对照，不关联到盘中判断")
    return {
        "plan_id": None,
        "plan_revision": None,
        "generated_at": None,
        "strategy_version": payload.get("strategy_version"),
        "facts": payload,
        "source_ref": str(path.resolve()),
        "legacy": True,
    }


def _plan_summary(row: dict) -> dict:
    facts = row["facts"]
    return {
        **{
            key: row.get(key)
            for key in (
                "plan_id",
                "plan_revision",
                "generated_at",
                "strategy_version",
                "source_ref",
            )
        },
        "strategy_parameters": _object(facts.get("strategy_parameters")),
        "candidates": [
            _symbol(item.get("symbol"))
            for item in _rows(facts.get("candidates"))
            if _symbol(item.get("symbol"))
        ],
        "sectors": [item.get("name") for item in _rows(facts.get("sectors"))],
        "timing_known": not row.get("legacy", False),
    }


def _plan_id_at(plans: list[dict], moment: datetime) -> str | None:
    available = [row for row in plans if parse_bar_datetime(row.get("generated_at")) <= moment]
    return available[-1]["plan_id"] if available else None


def _confirm(
    plan: dict | None,
    snapshots: list[SectorSnapshot],
    cutoff: datetime,
    path: Path,
    original: bool,
    notes: list[str],
) -> dict:
    if plan is None:
        return {"results": [], "data_status": "缺少当天计划，无法验证原判断"}
    try:
        forecast = build_sector_forecast(facts=plan["facts"])
        result = build_sector_confirmation(
            forecast=forecast,
            snapshots=snapshots,
            since=parse_bar_datetime(plan.get("generated_at")),
            until=cutoff,
        )
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        notes.append(f"计划中的板块字段无法解析（{type(exc).__name__}），保留其他复盘内容")
        return {"results": [], "data_status": "板块计划格式异常"}
    result["plan_id"] = plan.get("plan_id")
    result["original_prediction_test"] = original
    result["invalidation_conditions"] = forecast["execution_protocol"]["invalidate"]
    result["source_ref"] = f"{path.resolve()}#{result.get('as_of') or 'missing'}"
    for row in result["results"]:
        row["attribution"] = (
            "仅事后对照"
            if not original
            else {"已确认": "方向证据一致", "已否决": "方向假设失效"}.get(
                row["status"], "方向证据不足"
            )
        )
    return result


def _decisions(book: Journal, day: date, cutoff: datetime, notes: list[str]) -> list[dict]:
    result = []
    for row in book.rows(since=day, until=day):
        if row.get("kind") not in VERIFIABLE_KINDS:
            continue
        stamp = parse_bar_datetime(row.get("recorded_at"))
        if stamp and stamp > cutoff:
            notes.append("台账中有事后写入的判断，未用来解释当天决策")
            continue
        result.append(
            {
                **{
                    key: row.get(key)
                    for key in (
                        "symbol",
                        "name",
                        "sector",
                        "source",
                        "kind",
                        "decision_id",
                        "plan_id",
                        "revision",
                        "strategy_version",
                        "strategy_revision",
                        "recorded_at",
                        "supersedes",
                    )
                },
                "timing_known": stamp is not None,
                "source_ref": f"{book.root.resolve()}/{day:%Y-%m}.jsonl#{record_key(row)}",
            }
        )
    return result


def _candidate_universe(plans: list[dict], decisions: list[dict]) -> dict[str, dict]:
    candidates: dict[str, dict] = {}
    for plan in plans:
        for item in _rows(plan["facts"].get("candidates")):
            symbol = _symbol(item.get("symbol"))
            if symbol:
                candidates.setdefault(
                    symbol,
                    {
                        "symbol": symbol,
                        "name": item.get("name"),
                        "sector": item.get("sector"),
                        "plan_id": plan.get("plan_id"),
                        "introduced_at": plan.get("generated_at"),
                        "origin": "计划候选",
                        "plan_ref": plan["source_ref"],
                    },
                )
    for row in decisions:
        symbol = _symbol(row.get("symbol"))
        if symbol and row.get("kind") == "recommendation":
            candidates.setdefault(
                symbol,
                {"symbol": symbol, "origin": "仅台账记录", "introduced_at": row.get("recorded_at")},
            )
    return candidates


def _candidate_review(
    plans: list[dict], events: list[dict], decisions: list[dict], notes: list[str]
) -> tuple[list[dict], list[dict], dict]:
    candidates = _candidate_universe(plans, decisions)
    observed: dict[str, list[dict]] = {}
    watched: Counter = Counter()
    alerts: dict[str, list[dict]] = {}
    timeline = []
    stock_events = [row for row in events if row.get("kind") == "stock_poll"]
    for event in events:
        moment = parse_bar_datetime(event["at"])
        for alert in _rows(event.get("alerts")):
            row = {
                **alert,
                "kind": "alert",
                "at": event["at"],
                "plan_id": event["plan_id"],
                "source_ref": event["source_ref"],
                "delivery": "未记录回执",
            }
            timeline.append(row)
            symbol = _symbol(alert.get("symbol"))
            if symbol:
                alerts.setdefault(symbol, []).append(row)
        if event.get("kind") != "stock_poll":
            if event.get("kind") == "sector_poll" and event.get("tradable") is not True:
                timeline.append(
                    {
                        "kind": "sector_gap",
                        "at": event["at"],
                        "plan_id": event["plan_id"],
                        "headline": "；".join(_strings(event.get("notes")))
                        or "本轮板块截面不可用于确认",
                        "source_ref": event["source_ref"],
                    }
                )
            continue
        for symbol in _strings(event.get("watch_symbols")):
            symbol = _symbol(symbol)
            watched[symbol] += 1
            candidates.setdefault(symbol, {"symbol": symbol, "origin": "额外监控标的"})
        if _object(event.get("health")).get("usable") is not True:
            timeline.append(
                {
                    "kind": "feed_gap",
                    "at": event["at"],
                    "plan_id": event["plan_id"],
                    "headline": _object(event.get("health")).get("reason") or "本轮行情不可用",
                    "source_ref": event["source_ref"],
                }
            )
            continue
        for item in _rows(event.get("observations")):
            symbol = _symbol(item.get("symbol"))
            stamp = parse_bar_datetime(item.get("source_at"))
            price = finite(item.get("price"))
            if (
                not symbol
                or stamp is None
                or price is None
                or price <= 0
                or stamp.date() != moment.date()
                or not 0 <= (moment - stamp).total_seconds() <= DEGRADED_LAG_MINUTES * 60
            ):
                notes.append("个股过程日志有缺失、陈旧或未来时点的报价，未用于价位验证")
                continue
            introduced = parse_bar_datetime(candidates.get(symbol, {}).get("introduced_at"))
            if introduced and stamp < introduced:
                continue
            observed.setdefault(symbol, []).append(
                {
                    **item,
                    "at": event["at"],
                    "plan_id": event["plan_id"],
                    "source_ref": event["source_ref"],
                }
            )

    result = [
        _candidate_result(
            candidate, watched[symbol], observed.get(symbol, []), alerts.get(symbol, [])
        )
        for symbol, candidate in candidates.items()
    ]
    return (
        result,
        timeline,
        {
            "stock_polls": len(stock_events),
            "unusable_polls": sum(
                _object(row.get("health")).get("usable") is not True for row in stock_events
            ),
            "first_poll_at": stock_events[0]["at"] if stock_events else None,
            "last_poll_at": stock_events[-1]["at"] if stock_events else None,
            "meaning": "仅表示日志中观察到的采样范围，不证明时段内连续覆盖",
        },
    )


def _candidate_result(
    candidate: dict, watched: int, observations: list[dict], emitted: list[dict]
) -> dict:
    introduced = parse_bar_datetime(candidate.get("introduced_at"))
    emitted = [
        row for row in emitted if introduced is None or parse_bar_datetime(row["at"]) >= introduced
    ]
    triggers = sorted({str(row.get("trigger")) for row in emitted})
    hits: dict[str, list[dict]] = {"entry": [], "stop": [], "target": []}
    for row in observations:
        price = finite(row.get("price"))
        low, high, stop = (finite(row.get(key)) for key in ("entry_low", "entry_high", "stop"))
        reference = {
            key: row.get(key) for key in ("at", "source_at", "price", "plan_id", "source_ref")
        }
        if (
            row.get("holding") is not True
            and low is not None
            and high is not None
            and 0 < low <= price <= high
        ):
            hits["entry"].append(reference)
        if row.get("armed") is True:
            if stop is not None and 0 < price <= stop:
                hits["stop"].append(reference)
            targets = row.get("targets") if isinstance(row.get("targets"), list) else []
            if any(
                0 < target <= price for value in targets if (target := finite(value)) is not None
            ):
                hits["target"].append(reference)
    status = (
        "监控覆盖不足"
        if not observations
        else (
            "风控或退出条件已提示"
            if EXIT_TRIGGERS.intersection(triggers) or "risk_notice" in triggers
            else "入场条件已提示"
            if ENTRY_TRIGGERS.intersection(triggers)
            else "已观察，未记录行动条件告警"
        )
    )
    return {
        **candidate,
        "watched_polls": watched,
        "observations": len(observations),
        "first_observed_at": observations[0]["source_at"] if observations else None,
        "last_observed_at": observations[-1]["source_at"] if observations else None,
        "first_price": observations[0]["price"] if observations else None,
        "last_price": observations[-1]["price"] if observations else None,
        "observed_hits": hits,
        "alerts": emitted,
        "triggers": triggers,
        "attribution": status,
        "actual_execution": "未记录",
    }


def _news_review(
    corpus: NewsStore | None,
    day: date,
    cutoff: datetime,
    plans: list[dict],
    snapshots: list[SectorSnapshot],
    path: Path,
    notes: list[str],
) -> list[dict]:
    if corpus is None:
        corpus_path = state_path(*DEFAULT_RELATIVE_PATH)
        if not corpus_path.exists():
            notes.append("没有本地新闻语料库，无法还原当天首次采集的消息")
            return []
        corpus = NewsStore(corpus_path)
    if not corpus.ok:
        notes.append(f"新闻语料库不可用：{corpus.error}")
        return []
    hours = (cutoff - datetime.combine(day, time.min)).total_seconds() / 3600
    docs = corpus.recent(within_hours=hours, now=cutoff, limit=NEWS_LIMIT + 1)
    if corpus.read_error:
        notes.append(f"新闻语料库读取失败：{corpus.read_error}")
    if len(docs) > NEWS_LIMIT:
        notes.append(f"当天新闻超过 {NEWS_LIMIT} 条，仅列最近 {NEWS_LIMIT} 条，早段新闻覆盖不全")
    result = []
    for doc in docs[:NEWS_LIMIT]:
        stamp = to_beijing_naive(doc.first_seen_at)
        published = parse_bar_datetime(doc.published_at)
        if stamp.date() != day or stamp > cutoff or (published is not None and published > cutoff):
            notes.append("新闻存在截止时点之后的采集或发布时间，已排除")
            continue
        matched = [
            (snapshot, [row for row in snapshot.reads if row.name in doc.sectors])
            for snapshot in snapshots
            if snapshot.tradable
            and snapshot.as_of >= stamp
            and is_continuous_session(snapshot.as_of)
        ]
        matched = [(snapshot, rows) for snapshot, rows in matched if rows]
        evidence = []
        if matched:
            selected = [matched[0]] if len(matched) == 1 else [matched[0], matched[-1]]
            evidence = [
                {
                    "as_of": snapshot.as_of.isoformat(),
                    "sectors": [
                        {
                            "name": row.name,
                            "excess_pct": row.excess_pct,
                            "advance_ratio": row.advance_ratio,
                        }
                        for row in rows
                    ],
                    "source_ref": f"{path.resolve()}#{snapshot.as_of.isoformat()}",
                }
                for snapshot, rows in selected
            ]
        result.append(
            {
                "key": doc.key,
                "title": doc.title,
                "source": doc.source,
                "url": doc.url,
                "published_at": doc.published_at,
                "first_seen_at": stamp.isoformat(),
                "plan_id": _plan_id_at(plans, stamp),
                "sectors": list(doc.sectors),
                "classification_timing": "标签可事后补录，仅用于检索对照",
                "price_evidence": evidence,
                "follow_up": "已观察到后续相关板块截面，不证明因果"
                if evidence
                else "缺少后续匹配截面，待验证",
                "source_ref": f"{corpus.path.resolve()}#{doc.key}",
            }
        )
    return sorted(result, key=lambda row: row["first_seen_at"])


def _next_checks(candidates: list[dict], confirmation: dict, original: bool) -> list[dict]:
    result = []
    if not original:
        result.append(
            {
                "subject": "盘前计划",
                "reason": "原判断时点证据不足",
                "action": "次日开盘前生成并保存晨报计划，盘中变更保留新版本",
            }
        )
    for row in confirmation.get("results", []):
        status = row["status"]
        action = (
            "重新检查上涨广度、超额收益与成交额占比，重新确认前暂停沿用该主线"
            if status == "已否决"
            else "次日09:35后复核广度、超额稳定性与成交额占比，触及原失效条件时取消延续判断"
            if status == "已确认"
            else "补齐连续有效板块截面，确认独立行业与价格跟随后再评估原情景"
        )
        result.append(
            {
                "subject": row.get("name"),
                "reason": status,
                "action": action,
                "source_ref": confirmation.get("source_ref"),
            }
        )
    for row in candidates:
        if not row["observations"]:
            action = "若继续关注，显式加入监控并检查行情时效；当前不能判断条件是否触发"
        elif EXIT_TRIGGERS.intersection(row["triggers"]) or "risk_notice" in row["triggers"]:
            action = "人工核对风险或退出提示与实际持仓，复核失效条件后重建计划"
        elif ENTRY_TRIGGERS.intersection(row["triggers"]):
            action = "人工核实是否成交及成交时间；未成交时重新计算当日价位与风险收益"
        else:
            action = "次日重新检查入场条件、价位与行情覆盖，不能把未告警解释为条件未触发"
        result.append(
            {
                "subject": row["symbol"],
                "reason": row["attribution"],
                "action": action,
                "plan_id": row.get("plan_id"),
            }
        )
    return result


def render_daily_review(payload: dict) -> str:
    """报告主体；全部证据和事件保留在同日 JSON 与来源档案中。"""
    coverage = payload["coverage"]
    lines = [
        f"**复盘日** {payload['trade_date']}　**截止** {payload['as_of']}",
        "",
        (
            f"原始盘前计划：{'可核验' if payload['original_plan_available'] else '证据不足'}；"
            f"策略版本：{payload.get('strategy_version') or '未记录'}；计划版本 {len(payload['plans'])} 份。"
        ),
        (
            f"监控 {coverage['stock_polls']} 轮（不可用 {coverage['unusable_polls']}）；"
            f"板块截面 {coverage['sector_snapshots']} 份；首次采集新闻 {coverage['news_items']} 条。"
        ),
        "",
        "## 计划与版本",
    ]
    for row in payload["plans"]:
        lines.append(
            f"- {row.get('generated_at') or '时点未知'}：{row.get('plan_id') or '旧晨报'}，策略 {row.get('strategy_version') or '未记录'}；来源 {row['source_ref']}"
        )
    lines.extend(["", "## 板块判断对照"])
    confirmation = payload["sector_confirmation"]
    lines.append(
        f"- {confirmation.get('data_status')}；证据时点 {confirmation.get('as_of') or '缺失'}"
    )
    for row in confirmation.get("results", []):
        lines.append(f"- {row.get('name')}：{row['status']}，{row['attribution']}")
    lines.extend(["", "## 候选与行动条件"])
    for row in payload["candidates"]:
        triggers = "、".join(
            POLICIES[key].label if key in POLICIES else key for key in row["triggers"]
        )
        lines.append(
            f"- {row['symbol']}（{row['origin']}）：{row['attribution']}；有效报价 {row['observations']} 次；告警 {triggers or '未记录'}。"
        )
    lines.extend(["", "## 事件时间线"])
    timeline = payload["timeline"]
    visible = [
        row
        for row in timeline
        if row["kind"] != "news" or parse_bar_datetime(row["at"]).time() >= MORNING_OPEN
    ]
    for row in visible[:60]:
        lines.append(
            f"- {row['at']} [{row['kind']}] {row.get('headline') or '事件'}；计划 {row.get('plan_id') or '未关联'}；来源 {row.get('source_ref')}"
        )
    if len(visible) > 60 or len(visible) != len(timeline):
        lines.append(
            f"- 正文展示 {min(len(visible), 60)}/{len(timeline)} 条（优先盘中事件）；完整时间线见同日 JSON。"
        )
    lines.extend(["", render_next_checks(payload), "", "## 数据说明"])
    lines.extend(f"- {note}" for note in payload["data_notes"])
    return "\n".join(lines)


def render_next_checks(payload: dict) -> str:
    lines = [f"## 次日复查（来源 {payload.get('trade_date')}）", ""]
    lines.extend(
        f"- {row.get('subject')}：{row.get('reason')}。{row.get('action')}"
        for row in _rows(payload.get("next_checks"))
    )
    return "\n".join(lines)


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return (
        [item for item in value if isinstance(item, str) and item]
        if isinstance(value, list)
        else []
    )


def _symbol(value: Any) -> str:
    return normalize_symbol(value) if isinstance(value, str) else ""
