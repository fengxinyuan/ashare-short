"""采样部分 AkShare / TickFlow 接口的可达性、时效和重复调用延迟。

TickFlow 使用免费客户端，付费接口探针用于检查权限边界；主流程实际分钟线
使用 ashare-ds intraday-check 检查。非交易时段的时间差不代表盘中延迟。
这里只按工作日判断开市，不加载本地节假日表。有限次采样不代表持续可用。

--only 选择探针；--symbols 的首只标的用于单票接口，批量接口使用完整列表。

    python3 scripts/source_audit.py --repeats 5 --json audit.json
"""

from __future__ import annotations

import argparse
import signal
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.freshness import in_session
from ashare_decision_support.schedule.clock import AFTERNOON_CLOSE
from ashare_decision_support.schedule.timezone import BEIJING_TZ, beijing_naive_now, beijing_today
from ashare_decision_support.signals.series import parse_bar_datetime
from ashare_decision_support.state.store import write_json
from ashare_decision_support.symbols import bare_code, normalize_symbol, prefixed_code
from ashare_decision_support.tabular import records

CALL_TIMEOUT_SECONDS = 45


class ProbeTimeout(RuntimeError):
    pass


def _timeout_handler(_signum, _frame):
    raise ProbeTimeout(f"exceeded {CALL_TIMEOUT_SECONDS}s")


@dataclass
class Result:
    ok: bool
    seconds: float
    rows: int = 0
    latest: datetime | None = None
    error: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Probe:
    key: str
    group: str
    label: str
    call: Callable[[], Result]
    used_for: str
    critical: bool = False


# helpers


def run_once(probe: Probe) -> Result:
    start = time.perf_counter()
    has_alarm = hasattr(signal, "SIGALRM") and hasattr(signal, "alarm")
    previous_handler = signal.signal(signal.SIGALRM, _timeout_handler) if has_alarm else None
    if has_alarm:
        signal.alarm(CALL_TIMEOUT_SECONDS)
    try:
        result = probe.call()
        result.seconds = time.perf_counter() - start
        return result
    except Exception as exc:
        return Result(
            ok=False,
            seconds=time.perf_counter() - start,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
    finally:
        if has_alarm:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)


def parse_ts(value: Any) -> datetime | None:
    """完整日期复用行情解析；缺日期的时钟另按最近工作日锚定。"""
    parsed = parse_bar_datetime(value)
    if parsed is not None or not isinstance(value, str):
        return parsed
    text = value.strip()
    now = beijing_naive_now()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            clock = datetime.strptime(text, fmt).time()
        except ValueError:
            continue
        return anchor_bare_time(clock, now)

    # 月日没有年份时，取最近一次已发生的日期；跨年不能标成未来数据。
    for year in (now.year, now.year - 1):
        try:
            parsed = datetime.strptime(f"{year}-{text}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if parsed <= now:
            return parsed
    return None


def columnar(payload: Any) -> tuple[int, datetime | None]:
    """(bar count, newest timestamp) for TickFlow's column-array kline payload."""
    if not isinstance(payload, dict):
        rows = records(payload)
        return len(rows), newest(rows, "timestamp", "time", "date")
    stamps = payload.get("timestamp") or payload.get("time") or []
    if not isinstance(stamps, list):
        return 0, None
    parsed = [ts for ts in (parse_ts(value) for value in stamps) if ts is not None]
    return len(stamps), max(parsed) if parsed else None


def newest(rows: list[dict], *keys: str) -> datetime | None:
    stamps: list[datetime] = []
    for row in rows:
        for key in keys:
            if key in row:
                parsed = parse_ts(row.get(key))
                if parsed is not None:
                    stamps.append(parsed)
                    break
    return max(stamps) if stamps else None


def last_session_close(now: datetime) -> datetime:
    """最近一次工作日收盘，不加载本地节假日表。"""
    day = now.date()
    close = datetime.combine(day, AFTERNOON_CLOSE)
    if now.weekday() < 5 and now >= close:
        return close
    while True:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            return datetime.combine(day, AFTERNOON_CLOSE)


def anchor_bare_time(clock: dtime, now: datetime) -> datetime:
    """缺日期时只锚到已发生的工作日，避免周末和盘前产生未来时间戳。"""
    candidate = datetime.combine(now.date(), clock)
    if now.weekday() < 5 and candidate <= now:
        return candidate
    return datetime.combine(last_session_close(now).date(), clock)


# probes


def build_probes(ak: Any, tf_client: Any, symbols: list[str]) -> list[Probe]:
    probes: list[Probe] = []
    symbols = list(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols))
    code = bare_code(symbols[0])
    prefixed = prefixed_code(symbols[0])
    today = beijing_today()
    close = last_session_close(beijing_naive_now()).date()

    def ths_taxonomy() -> Result:
        from ashare_decision_support.adapters.ths_taxonomy import ThsTaxonomySource
        from ashare_decision_support.sectors import static_taxonomies

        source = ThsTaxonomySource()
        snapshot = source.load(force_refresh=True)
        industry, concept = static_taxonomies(source)
        industries, concepts = industry.list_sectors(), concept.list_sectors()
        return Result(
            ok=bool(industries and concepts) and not snapshot.from_cache,
            seconds=0.0,
            rows=len(snapshot.profiles),
            extra={
                "industries": len(industries),
                "concepts": len(concepts),
                "from_cache": snapshot.from_cache,
                "notes": snapshot.notes,
            },
        )

    probes.append(
        Probe(
            "ths_taxonomy",
            "同花顺分类快照",
            "industries.json / concepts.json（第三方托管）",
            ths_taxonomy,
            "行业、概念及成分股关系",
            critical=True,
        )
    )

    # TickFlow free tier
    if tf_client is not None:

        def tf_klines_batch() -> Result:
            end_ms = (
                int(
                    datetime.combine(
                        today + timedelta(days=1), dtime.min, tzinfo=BEIJING_TZ
                    ).timestamp()
                    * 1000
                )
                - 1
            )
            start_ms = int(
                datetime.combine(
                    today - timedelta(days=40), dtime.min, tzinfo=BEIJING_TZ
                ).timestamp()
                * 1000
            )
            payload = tf_client.klines.batch(
                symbols,
                period="1d",
                start_time=start_ms,
                end_time=end_ms,
                count=64,
                adjust="forward",
            )
            total = 0
            latest = None
            if isinstance(payload, dict):
                for series in payload.values():
                    count, stamp = columnar(series)
                    total += count
                    if stamp and (latest is None or stamp > latest):
                        latest = stamp
            return Result(
                ok=total > 0,
                seconds=0.0,
                rows=total,
                latest=latest,
                extra={"symbols": len(payload) if isinstance(payload, dict) else 0},
            )

        def tf_instruments() -> Result:
            payload = tf_client.instruments.batch(symbols)
            rows = payload if isinstance(payload, list) else records(payload)
            has_limits = any(
                isinstance(row, dict) and (row.get("ext") or {}).get("limit_up") for row in rows
            )
            return Result(
                ok=bool(rows), seconds=0.0, rows=len(rows), extra={"limit_prices": has_limits}
            )

        def tf_quotes() -> Result:
            """使用免费客户端检查实时报价权限。"""
            # TickFlow's public client is keyword-only here. Keep this probe
            # valid so a service rejection is not confused with our own call bug.
            payload = tf_client.quotes.get(symbols=symbols[0])
            rows = (
                payload
                if isinstance(payload, list)
                else records(payload) or ([payload] if payload else [])
            )
            return Result(
                ok=bool(rows),
                seconds=0.0,
                rows=len(rows),
                latest=newest([r for r in rows if isinstance(r, dict)], "timestamp", "time"),
            )

        def tf_intraday() -> Result:
            payload = tf_client.klines.intraday(symbols[0], period="1m", count=240)
            count, stamp = columnar(payload)
            return Result(ok=count > 0, seconds=0.0, rows=count, latest=stamp)

        def tf_depth() -> Result:
            payload = tf_client.depth.get(symbols[0])
            rows = (
                payload
                if isinstance(payload, list)
                else records(payload) or ([payload] if payload else [])
            )
            return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

        probes += [
            Probe(
                "tf_klines_batch",
                "TickFlow 免费档",
                "klines.batch（日线批量）",
                tf_klines_batch,
                "板块动量/个股日线",
                critical=True,
            ),
            Probe(
                "tf_instruments",
                "TickFlow 免费档",
                "instruments.batch（名称/涨跌停）",
                tf_instruments,
                "标的信息",
            ),
            Probe(
                "tf_quotes", "TickFlow 付费闸", "quotes.get（实时报价）", tf_quotes, "盘中板块实时"
            ),
            Probe(
                "tf_intraday", "TickFlow 付费闸", "klines 1m（分钟线）", tf_intraday, "盘中买卖点"
            ),
            Probe("tf_depth", "TickFlow 付费闸", "depth.get（五档盘口）", tf_depth, "挂单档位"),
        ]

    if ak is None:
        return probes

    # 新浪分钟线
    for period in ("1", "5", "60"):

        def sina_minute(period: str = period) -> Result:
            table = ak.stock_zh_a_minute(symbol=prefixed, period=period, adjust="")
            rows = records(table)
            return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "day"))

        probes.append(
            Probe(
                f"sina_min_{period}",
                "新浪分钟线",
                f"stock_zh_a_minute period={period}",
                sina_minute,
                "买卖点/止损/目标（不复权）",
                critical=(period == "1"),
            )
        )

    # 新浪快照、日线与腾讯逐笔
    def sina_spot_all() -> Result:
        table = ak.stock_zh_a_spot()
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

    def sina_daily() -> Result:
        table = ak.stock_zh_a_daily(symbol=prefixed, adjust="")
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "date"))

    def tx_tick() -> Result:
        table = ak.stock_zh_a_tick_tx_js(symbol=prefixed)
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "成交时间"))

    probes += [
        Probe(
            "sina_spot_all",
            "候选替代源",
            "stock_zh_a_spot（新浪全量快照）",
            sina_spot_all,
            "批量行情对照",
        ),
        Probe(
            "sina_daily",
            "候选替代源",
            "stock_zh_a_daily（新浪日线不复权）",
            sina_daily,
            "备份日线/复权校验",
        ),
        Probe(
            "tx_tick", "候选替代源", "stock_zh_a_tick_tx_js（腾讯逐笔）", tx_tick, "逐笔成交对照"
        ),
    ]

    # AkShare news / flow
    def cls_telegraph() -> Result:
        table = ak.stock_info_global_cls(symbol="全部")
        rows = records(table)
        stamps = []
        for row in rows:
            merged = f"{row.get('发布日期', '')} {row.get('发布时间', '')}".strip()
            parsed = parse_ts(merged)
            if parsed:
                stamps.append(parsed)
        return Result(
            ok=bool(rows), seconds=0.0, rows=len(rows), latest=max(stamps) if stamps else None
        )

    def em_global() -> Result:
        table = ak.stock_info_global_em()
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "发布时间"))

    def cctv() -> Result:
        table = ak.news_cctv(date=close.strftime("%Y%m%d"))
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "date"))

    def stock_news() -> Result:
        table = ak.stock_news_em(symbol=code)
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "发布时间"))

    def notices() -> Result:
        table = ak.stock_notice_report(symbol="全部", date=close.strftime("%Y%m%d"))
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "公告日期"))

    def lhb() -> Result:
        table = ak.stock_lhb_detail_em(
            start_date=(close - timedelta(days=7)).strftime("%Y%m%d"),
            end_date=close.strftime("%Y%m%d"),
        )
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "上榜日"))

    probes += [
        Probe(
            "cls",
            "AkShare 舆情",
            "stock_info_global_cls（财联社电报）",
            cls_telegraph,
            "盘前舆情",
            critical=True,
        ),
        Probe(
            "em_global", "AkShare 舆情", "stock_info_global_em（东财快讯）", em_global, "盘前舆情"
        ),
        Probe("cctv", "AkShare 舆情", "news_cctv（新闻联播）", cctv, "政策面"),
        Probe("stock_news", "AkShare 舆情", "stock_news_em（个股新闻）", stock_news, "个股舆情"),
        Probe(
            "notices",
            "AkShare 硬筛",
            "stock_notice_report（沪深公告）",
            notices,
            "致命公告否决",
            critical=True,
        ),
        Probe("lhb", "AkShare 资金", "stock_lhb_detail_em（龙虎榜）", lhb, "资金确认"),
    ]

    # 东财 push2
    def em_spot() -> Result:
        table = ak.stock_zh_a_spot_em()
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

    def em_min() -> Result:
        table = ak.stock_zh_a_hist_min_em(symbol=code, period="1", adjust="")
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "时间"))

    def em_intraday() -> Result:
        table = ak.stock_intraday_em(symbol=code)
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "时间"))

    def em_bid_ask() -> Result:
        table = ak.stock_bid_ask_em(symbol=code)
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

    def em_board() -> Result:
        table = ak.stock_board_industry_name_em()
        rows = records(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

    probes += [
        Probe("em_spot", "东财 push2", "stock_zh_a_spot_em", em_spot, "批量行情"),
        Probe("em_min", "东财 push2", "stock_zh_a_hist_min_em", em_min, "分钟线"),
        Probe("em_intraday", "东财 push2", "stock_intraday_em", em_intraday, "逐笔成交"),
        Probe("em_bid_ask", "东财 push2", "stock_bid_ask_em", em_bid_ask, "五档盘口"),
        Probe("em_board", "东财 push2", "stock_board_industry_name_em", em_board, "板块列表"),
    ]

    # 板块、资金与指数
    def ak_call(fn, *keys: str):
        def run() -> Result:
            table = fn()
            rows = records(table)
            return Result(
                ok=bool(rows),
                seconds=0.0,
                rows=len(rows),
                latest=newest(rows, *keys) if keys else None,
            )

        return run

    probes += [
        Probe(
            "zt_pool",
            "东财",
            "stock_zt_pool_em（涨停板池）",
            ak_call(lambda: ak.stock_zt_pool_em(date=close.strftime("%Y%m%d"))),
            "连板梯队/情绪温度",
            critical=True,
        ),
        Probe(
            "zt_zbgc",
            "东财",
            "stock_zt_pool_zbgc_em（炸板池）",
            ak_call(lambda: ak.stock_zt_pool_zbgc_em(date=close.strftime("%Y%m%d"))),
            "炸板率/情绪退潮",
        ),
        Probe(
            "zt_strong",
            "东财",
            "stock_zt_pool_strong_em（强势股池）",
            ak_call(lambda: ak.stock_zt_pool_strong_em(date=close.strftime("%Y%m%d"))),
            "次级梯队",
        ),
        Probe(
            "em_hist_daily",
            "东财",
            "stock_zh_a_hist（日线）",
            ak_call(
                lambda: ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=(close - timedelta(days=60)).strftime("%Y%m%d"),
                    end_date=close.strftime("%Y%m%d"),
                    adjust="",
                ),
                "日期",
            ),
            "不复权日线交叉校验",
        ),
        Probe(
            "em_flow_indiv",
            "东财",
            "stock_individual_fund_flow（个股资金流）",
            ak_call(lambda: ak.stock_individual_fund_flow(stock=code, market=prefixed[:2]), "日期"),
            "主力/超大单净流入",
        ),
        Probe(
            "em_flow_market",
            "东财",
            "stock_market_fund_flow（两市资金流）",
            ak_call(lambda: ak.stock_market_fund_flow(), "日期"),
            "大盘资金面",
        ),
        Probe(
            "hsgt",
            "东财",
            "stock_hsgt_fund_flow_summary_em（北向）",
            ak_call(lambda: ak.stock_hsgt_fund_flow_summary_em(), "交易日"),
            "外资流向",
        ),
        Probe(
            "ths_industry_flow",
            "同花顺",
            "stock_fund_flow_industry（行业资金流·即时）",
            ak_call(lambda: ak.stock_fund_flow_industry(symbol="即时")),
            "板块资金确认",
            critical=True,
        ),
        Probe(
            "ths_concept_flow",
            "同花顺",
            "stock_fund_flow_concept（概念资金流·即时）",
            ak_call(lambda: ak.stock_fund_flow_concept(symbol="即时")),
            "题材资金确认",
        ),
        Probe(
            "ths_industry_sum",
            "同花顺",
            "stock_board_industry_summary_ths（行业板块汇总）",
            ak_call(lambda: ak.stock_board_industry_summary_ths()),
            "板块涨跌幅/涨跌家数",
            critical=True,
        ),
        Probe(
            "sina_index_spot",
            "新浪",
            "stock_zh_index_spot_sina（指数实时）",
            ak_call(lambda: ak.stock_zh_index_spot_sina()),
            "大盘基准",
        ),
    ]

    return probes


# driver


def audit(probes: list[Probe], repeats: int) -> dict:
    now = beijing_naive_now()
    close = last_session_close(now)
    session = in_session(now)
    report: dict[str, Any] = {
        "generated_at": now.isoformat(timespec="seconds"),
        "in_session": session,
        "last_session_close": close.isoformat(timespec="seconds"),
        "repeats": repeats,
        "call_timeout_seconds": CALL_TIMEOUT_SECONDS,
        "sources": [],
    }

    for probe in probes:
        runs = [run_once(probe) for _ in range(repeats)]
        good = [r for r in runs if r.ok]
        latencies = sorted(r.seconds for r in runs if r.ok)
        latest = max((r.latest for r in good if r.latest), default=None)

        entry: dict[str, Any] = {
            "key": probe.key,
            "group": probe.group,
            "label": probe.label,
            "used_for": probe.used_for,
            "critical": probe.critical,
            "attempts": len(runs),
            "successes": len(good),
            "success_rate": round(len(good) / len(runs), 3) if runs else 0.0,
            "rows": good[0].rows if good else 0,
            "rows_varies": len({r.rows for r in good}) > 1 if good else False,
            "p50_seconds": round(statistics.median(latencies), 3) if latencies else None,
            "max_seconds": round(max(latencies), 3) if latencies else None,
            "latest_data": latest.isoformat(timespec="seconds") if latest else None,
            "errors": sorted({r.error for r in runs if not r.ok and r.error})[:3],
            "extra": good[0].extra if good else {},
        }
        if latest:
            entry["lag_minutes_vs_now"] = round((now - latest).total_seconds() / 60.0, 1)
            entry["lag_minutes_vs_close"] = round((close - latest).total_seconds() / 60.0, 1)
            entry["covers_last_session"] = latest.date() >= close.date()
        report["sources"].append(entry)
        print(_line(entry), flush=True)

    return report


def _line(entry: dict) -> str:
    mark = "OK  " if entry["success_rate"] == 1.0 else ("PART" if entry["successes"] else "FAIL")
    latency = f"{entry['p50_seconds']:>6.2f}s" if entry["p50_seconds"] is not None else "     -"
    rows = f"{entry['rows']:>6}"
    latest = entry.get("latest_data") or "-"
    err = f"  {entry['errors'][0][:70]}" if entry["errors"] else ""
    return (
        f"{mark} {entry['success_rate']:>5.0%} {latency} rows={rows}  "
        f"latest={latest:<19} {entry['label']}{err}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repeats", type=int, default=3, help="calls per source (stability sample)"
    )
    parser.add_argument("--symbols", nargs="+", default=["600519.SH", "000001.SZ", "300750.SZ"])
    parser.add_argument("--json", default="", help="write the full report here")
    parser.add_argument("--only", nargs="+", default=[], help="probe keys to run (default: all)")
    args = parser.parse_args()

    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    selected = set(args.only)
    ak = None
    if not selected or any(not key.startswith("tf_") for key in selected):
        try:
            import akshare as ak
        except Exception as exc:
            print(f"akshare unavailable: {exc}")

    tf_client = None
    if not selected or any(key.startswith("tf_") for key in selected):
        try:
            import tickflow

            tf_client = tickflow.TickFlow.free()
            connect = getattr(tf_client, "connect", None)
            if callable(connect):
                connect()
        except Exception as exc:
            tf_client = None
            print(f"tickflow unavailable: {exc}")

    probes = build_probes(ak, tf_client, args.symbols)
    unavailable = selected - {probe.key for probe in probes}
    if unavailable:
        parser.error("unknown or unavailable probe keys: " + ", ".join(sorted(unavailable)))
    if selected:
        probes = [probe for probe in probes if probe.key in selected]
    if not probes:
        parser.error(
            "no available probes; install akshare or tickflow and check initialization errors"
        )

    now = beijing_naive_now()
    print(
        f"# 数据源实测 {now:%Y-%m-%d %H:%M:%S}  交易时段={in_session(now)}  "
        f"上一收盘={last_session_close(now):%Y-%m-%d %H:%M}  repeats={args.repeats}\n"
    )

    report = audit(probes, args.repeats)

    if args.json:
        write_json(Path(args.json), report)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
