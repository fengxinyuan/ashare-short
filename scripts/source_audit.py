"""Measure every data source this system depends on: reachability, freshness, stability.

Answers one question: **is the free tier good enough, and where exactly does it
run out?** Three numbers per source, all measured on this host, never copied
from a vendor doc:

- **通** — does the call answer at all
- **时效** — how old is the newest row it returns (lag vs now, and vs the last
  session close)
- **稳** — success rate and p50/p95 latency over N repeats

Run it any day for reachability/stability. Run it *during a session* to also get
true intraday lag — outside trading hours the lag figure only measures distance
to the last close and is labelled as such.

    python3 scripts/source_audit.py --repeats 5 --json audit.json
"""

from __future__ import annotations

import argparse
import json
import signal
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

CALL_TIMEOUT_SECONDS = 45

SESSION_CLOSE = 15 * 60  # minutes from midnight
SESSION_OPEN = 9 * 60 + 30


class ProbeTimeout(RuntimeError):
    pass


def _timeout_handler(signum, frame):
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


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def run_once(probe: Probe) -> Result:
    start = time.perf_counter()
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(CALL_TIMEOUT_SECONDS)
    try:
        result = probe.call()
        result.seconds = time.perf_counter() - start
        return result
    except Exception as exc:
        return Result(ok=False, seconds=time.perf_counter() - start, error=f"{type(exc).__name__}: {exc}"[:200])
    finally:
        signal.alarm(0)


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        if ts < 1e8:
            return None
        return datetime.fromtimestamp(ts)
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y%m%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
        "%m-%d %H:%M", "%H:%M:%S", "%H:%M",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        # Bare times/dates carry no year: anchor them to today.
        if parsed.year == 1900:
            today = date.today()
            parsed = parsed.replace(year=today.year, month=parsed.month or today.month, day=parsed.day or today.day)
            if fmt in {"%H:%M:%S", "%H:%M"}:
                parsed = datetime.combine(today, parsed.time())
        return parsed
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def frame_rows(table: Any) -> list[dict]:
    if table is None:
        return []
    if isinstance(table, list):
        return [row for row in table if isinstance(row, dict)]
    to_dict = getattr(table, "to_dict", None)
    if callable(to_dict):
        try:
            return list(table.to_dict(orient="records"))
        except TypeError:
            pass
    return []


def columnar(payload: Any) -> tuple[int, datetime | None]:
    """(bar count, newest timestamp) for TickFlow's column-array kline payload."""
    if not isinstance(payload, dict):
        rows = frame_rows(payload)
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
    """Most recent 15:00 close, weekends stepped over. Holidays not modelled."""
    day = now.date()
    if now.weekday() < 5 and now.hour * 60 + now.minute >= SESSION_CLOSE:
        return datetime.combine(day, datetime.min.time()) + timedelta(minutes=SESSION_CLOSE)
    while True:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            return datetime.combine(day, datetime.min.time()) + timedelta(minutes=SESSION_CLOSE)


def in_session(now: datetime) -> bool:
    minutes = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return False
    return SESSION_OPEN <= minutes <= 11 * 60 + 30 or 13 * 60 <= minutes <= SESSION_CLOSE


# --------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------


def build_probes(ak: Any, tf_client: Any, symbols: list[str]) -> list[Probe]:
    probes: list[Probe] = []
    bare = [s.split(".")[0] for s in symbols]
    today = date.today()
    close = last_session_close(datetime.now()).date()

    # ---- TickFlow free tier ------------------------------------------------
    if tf_client is not None:
        def tf_universes() -> Result:
            payload = tf_client.universes.list()
            rows = payload if isinstance(payload, list) else frame_rows(payload)
            return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

        def tf_universe_get() -> Result:
            payload = tf_client.universes.list()
            rows = payload if isinstance(payload, list) else frame_rows(payload)
            first = rows[0] if rows else None
            uid = None
            if isinstance(first, dict):
                uid = first.get("id") or first.get("universe_id") or first.get("code")
            else:
                uid = getattr(first, "id", None)
            members = tf_client.universes.get(uid)
            count = len(members) if isinstance(members, list) else len(frame_rows(members))
            if count == 0 and isinstance(members, dict):
                inner = members.get("symbols") or members.get("constituents") or members.get("data") or []
                count = len(inner)
            return Result(ok=count > 0, seconds=0.0, rows=count, extra={"universe": str(uid)})

        def tf_klines_batch() -> Result:
            end_ms = int(datetime.combine(today + timedelta(days=1), datetime.min.time()).timestamp() * 1000)
            start_ms = int(datetime.combine(today - timedelta(days=40), datetime.min.time()).timestamp() * 1000)
            payload = tf_client.klines.batch(
                symbols, period="1d", start_time=start_ms, end_time=end_ms, count=64, adjust="forward"
            )
            total = 0
            latest = None
            if isinstance(payload, dict):
                for series in payload.values():
                    count, stamp = columnar(series)
                    total += count
                    if stamp and (latest is None or stamp > latest):
                        latest = stamp
            return Result(ok=total > 0, seconds=0.0, rows=total, latest=latest,
                          extra={"symbols": len(payload) if isinstance(payload, dict) else 0})

        def tf_instruments() -> Result:
            payload = tf_client.instruments.batch(symbols)
            rows = payload if isinstance(payload, list) else frame_rows(payload)
            has_limits = any(
                isinstance(row, dict) and (row.get("ext") or {}).get("limit_up") for row in rows
            )
            return Result(ok=bool(rows), seconds=0.0, rows=len(rows), extra={"limit_prices": has_limits})

        def tf_quotes() -> Result:
            """Expected to fail on the free tier — that failure IS the paid-tier case."""
            payload = tf_client.quotes.get(symbols[0])
            rows = payload if isinstance(payload, list) else frame_rows(payload) or ([payload] if payload else [])
            return Result(ok=bool(rows), seconds=0.0, rows=len(rows),
                          latest=newest([r for r in rows if isinstance(r, dict)], "timestamp", "time"))

        def tf_intraday() -> Result:
            payload = tf_client.klines.intraday(symbols[0], period="1m", count=240)
            count, stamp = columnar(payload)
            return Result(ok=count > 0, seconds=0.0, rows=count, latest=stamp)

        def tf_depth() -> Result:
            payload = tf_client.depth.get(symbols[0])
            rows = payload if isinstance(payload, list) else frame_rows(payload) or ([payload] if payload else [])
            return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

        probes += [
            Probe("tf_universes", "TickFlow 免费档", "universes.list（申万分类）", tf_universes, "板块划分", critical=True),
            Probe("tf_universe_get", "TickFlow 免费档", "universes.get（成分股）", tf_universe_get, "板块成分", critical=True),
            Probe("tf_klines_batch", "TickFlow 免费档", "klines.batch（日线批量）", tf_klines_batch, "板块动量/个股日线", critical=True),
            Probe("tf_instruments", "TickFlow 免费档", "instruments.batch（名称/涨跌停）", tf_instruments, "标的信息"),
            Probe("tf_quotes", "TickFlow 付费闸", "quotes.get（实时报价）", tf_quotes, "盘中板块实时"),
            Probe("tf_intraday", "TickFlow 付费闸", "klines 1m（分钟线）", tf_intraday, "盘中买卖点"),
            Probe("tf_depth", "TickFlow 付费闸", "depth.get（五档盘口）", tf_depth, "挂单档位"),
        ]

    if ak is None:
        return probes

    # ---- Sina intraday (the free path for needs 2/3) -----------------------
    for period in ("1", "5", "60"):
        def sina_minute(period: str = period) -> Result:
            table = ak.stock_zh_a_minute(symbol="sh600519", period=period, adjust="")
            rows = frame_rows(table)
            return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "day"))

        probes.append(
            Probe(f"sina_min_{period}", "新浪分钟线", f"stock_zh_a_minute period={period}",
                  sina_minute, "买卖点/止损/目标（不复权）", critical=(period == "1"))
        )

    # ---- Sina / Tencent bulk + tick: candidate replacements for dead push2 --
    def sina_spot_all() -> Result:
        table = ak.stock_zh_a_spot()
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

    def sina_daily() -> Result:
        table = ak.stock_zh_a_daily(symbol="sh600519", adjust="")
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "date"))

    def tx_tick() -> Result:
        table = ak.stock_zh_a_tick_tx_js(symbol="sh600519")
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "成交时间"))

    probes += [
        Probe("sina_spot_all", "候选替代源", "stock_zh_a_spot（新浪全量快照）", sina_spot_all, "替代已死的东财批量行情"),
        Probe("sina_daily", "候选替代源", "stock_zh_a_daily（新浪日线不复权）", sina_daily, "备份日线/复权校验"),
        Probe("tx_tick", "候选替代源", "stock_zh_a_tick_tx_js（腾讯逐笔）", tx_tick, "替代已死的东财逐笔"),
    ]

    # ---- AkShare news / flow ----------------------------------------------
    def cls_telegraph() -> Result:
        table = ak.stock_info_global_cls(symbol="全部")
        rows = frame_rows(table)
        stamps = []
        for row in rows:
            merged = f"{row.get('发布日期', '')} {row.get('发布时间', '')}".strip()
            parsed = parse_ts(merged)
            if parsed:
                stamps.append(parsed)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=max(stamps) if stamps else None)

    def em_global() -> Result:
        table = ak.stock_info_global_em()
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "发布时间"))

    def cctv() -> Result:
        table = ak.news_cctv(date=close.strftime("%Y%m%d"))
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "date"))

    def stock_news() -> Result:
        table = ak.stock_news_em(symbol=bare[0])
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "发布时间"))

    def notices() -> Result:
        table = ak.stock_notice_report(symbol="全部", date=close.strftime("%Y%m%d"))
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "公告日期"))

    def lhb() -> Result:
        table = ak.stock_lhb_detail_em(
            start_date=(close - timedelta(days=7)).strftime("%Y%m%d"), end_date=close.strftime("%Y%m%d")
        )
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "上榜日"))

    probes += [
        Probe("cls", "AkShare 舆情", "stock_info_global_cls（财联社电报）", cls_telegraph, "盘前舆情", critical=True),
        Probe("em_global", "AkShare 舆情", "stock_info_global_em（东财快讯）", em_global, "盘前舆情"),
        Probe("cctv", "AkShare 舆情", "news_cctv（新闻联播）", cctv, "政策面"),
        Probe("stock_news", "AkShare 舆情", "stock_news_em（个股新闻）", stock_news, "个股舆情"),
        Probe("notices", "AkShare 硬筛", "stock_notice_report（沪深公告）", notices, "致命公告否决", critical=True),
        Probe("lhb", "AkShare 资金", "stock_lhb_detail_em（龙虎榜）", lhb, "资金确认"),
    ]

    # ---- 东财 push2 family: re-verify the recorded outage --------------------
    def em_spot() -> Result:
        table = ak.stock_zh_a_spot_em()
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

    def em_min() -> Result:
        table = ak.stock_zh_a_hist_min_em(symbol=bare[0], period="1", adjust="")
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "时间"))

    def em_intraday() -> Result:
        table = ak.stock_intraday_em(symbol=bare[0])
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "时间"))

    def em_bid_ask() -> Result:
        table = ak.stock_bid_ask_em(symbol=bare[0])
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

    def em_board() -> Result:
        table = ak.stock_board_industry_name_em()
        rows = frame_rows(table)
        return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

    probes += [
        Probe("em_spot", "东财 push2（记录为不可达）", "stock_zh_a_spot_em", em_spot, "批量行情"),
        Probe("em_min", "东财 push2（记录为不可达）", "stock_zh_a_hist_min_em", em_min, "分钟线"),
        Probe("em_intraday", "东财 push2（记录为不可达）", "stock_intraday_em", em_intraday, "逐笔成交"),
        Probe("em_bid_ask", "东财 push2（记录为不可达）", "stock_bid_ask_em", em_bid_ask, "五档盘口"),
        Probe("em_board", "东财 push2（记录为不可达）", "stock_board_industry_name_em", em_board, "板块列表"),
    ]

    # ---- AkShare endpoints that DO answer despite the push2 outage ----------
    # The outage is per-endpoint, not per-host: 涨停池 and 逐笔-adjacent calls on
    # the same 东财 domains answer fine. Ultra-short work lives on these.
    def ak_call(fn, *keys: str):
        def run() -> Result:
            table = fn()
            rows = frame_rows(table)
            return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, *keys) if keys else None)
        return run

    probes += [
        Probe("zt_pool", "东财-仍可用", "stock_zt_pool_em（涨停板池）",
              ak_call(lambda: ak.stock_zt_pool_em(date=close.strftime("%Y%m%d"))), "连板梯队/情绪温度", critical=True),
        Probe("zt_zbgc", "东财-仍可用", "stock_zt_pool_zbgc_em（炸板池）",
              ak_call(lambda: ak.stock_zt_pool_zbgc_em(date=close.strftime("%Y%m%d"))), "炸板率/情绪退潮"),
        Probe("zt_strong", "东财-仍可用", "stock_zt_pool_strong_em（强势股池）",
              ak_call(lambda: ak.stock_zt_pool_strong_em(date=close.strftime("%Y%m%d"))), "次级梯队"),
        Probe("em_hist_daily", "东财-仍可用", "stock_zh_a_hist（日线）",
              ak_call(lambda: ak.stock_zh_a_hist(symbol=bare[0], period="daily",
                                                 start_date=(close - timedelta(days=60)).strftime("%Y%m%d"),
                                                 end_date=close.strftime("%Y%m%d"), adjust=""), "日期"), "不复权日线交叉校验"),
        Probe("em_flow_indiv", "东财-仍可用", "stock_individual_fund_flow（个股资金流）",
              ak_call(lambda: ak.stock_individual_fund_flow(stock=bare[0], market="sh"), "日期"), "主力/超大单净流入"),
        Probe("em_flow_market", "东财-仍可用", "stock_market_fund_flow（两市资金流）",
              ak_call(lambda: ak.stock_market_fund_flow(), "日期"), "大盘资金面"),
        Probe("hsgt", "东财-仍可用", "stock_hsgt_fund_flow_summary_em（北向）",
              ak_call(lambda: ak.stock_hsgt_fund_flow_summary_em(), "交易日"), "外资流向"),
        Probe("ths_industry_flow", "同花顺-全可用", "stock_fund_flow_industry（行业资金流·即时）",
              ak_call(lambda: ak.stock_fund_flow_industry(symbol="即时")), "板块资金确认", critical=True),
        Probe("ths_concept_flow", "同花顺-全可用", "stock_fund_flow_concept（概念资金流·即时）",
              ak_call(lambda: ak.stock_fund_flow_concept(symbol="即时")), "题材资金确认"),
        Probe("ths_industry_sum", "同花顺-全可用", "stock_board_industry_summary_ths（行业板块汇总）",
              ak_call(lambda: ak.stock_board_industry_summary_ths()), "板块涨跌幅/涨跌家数", critical=True),
        Probe("sina_index_spot", "新浪-仍可用", "stock_zh_index_spot_sina（指数实时）",
              ak_call(lambda: ak.stock_zh_index_spot_sina()), "大盘基准"),
    ]

    return probes


def build_library_probes(symbols: list[str]) -> list[Probe]:
    """Third-party libraries that reach hosts AkShare does not.

    These are the candidates that decide the paid-tier question: if a free
    library already serves the whole-market snapshot with a 5-level book, the
    paid TickFlow depth feed buys much less than the docs assume.
    """
    probes: list[Probe] = []
    bare = [s.split(".")[0] for s in symbols]

    try:
        import easyquotation as eq
    except Exception:
        eq = None
    if eq is not None:
        sina = eq.use("sina")
        tencent = eq.use("tencent")

        def eq_sina_depth() -> Result:
            payload = sina.stocks(bare)
            first = next(iter(payload.values()), {})
            stamp = parse_ts(f"{first.get('date', '')} {first.get('time', '')}".strip())
            has_book = all(f"bid{i}" in first and f"ask{i}" in first for i in range(1, 6))
            return Result(ok=bool(payload), seconds=0.0, rows=len(payload), latest=stamp,
                          extra={"five_level_book": has_book})

        def eq_sina_market() -> Result:
            payload = sina.market_snapshot(prefix=True)
            first = next(iter(payload.values()), {})
            stamp = parse_ts(f"{first.get('date', '')} {first.get('time', '')}".strip())
            return Result(ok=bool(payload), seconds=0.0, rows=len(payload), latest=stamp,
                          extra={"five_level_book": "bid5" in first})

        def eq_tencent() -> Result:
            payload = tencent.stocks(bare)
            first = next(iter(payload.values()), {})
            return Result(ok=bool(payload), seconds=0.0, rows=len(payload),
                          latest=parse_ts(first.get("datetime")),
                          extra={"limit_prices": "涨停价" in first, "volume_ratio": "量比" in first})

        probes += [
            Probe("eq_sina_depth", "easyquotation（新浪 hq）", "sina.stocks（个股五档）", eq_sina_depth,
                  "挂单档位/买卖点", critical=True),
            Probe("eq_sina_market", "easyquotation（新浪 hq）", "sina.market_snapshot（全市场五档）", eq_sina_market,
                  "板块实时聚合/全市场扫描", critical=True),
            Probe("eq_tencent", "easyquotation（腾讯 qt）", "tencent.stocks（含涨跌停/量比/委差）", eq_tencent,
                  "涨停判定/量比"),
        ]

    try:
        import efinance as ef
    except Exception:
        ef = None
    if ef is not None:
        def ef_spot() -> Result:
            table = ef.stock.get_realtime_quotes()
            rows = frame_rows(table)
            return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

        def ef_boards() -> Result:
            table = ef.stock.get_realtime_quotes("行业板块")
            rows = frame_rows(table)
            return Result(ok=bool(rows), seconds=0.0, rows=len(rows))

        probes += [
            Probe("ef_spot", "efinance（东财）", "get_realtime_quotes（全市场快照）", ef_spot, "全市场实时"),
            Probe("ef_boards", "efinance（东财）", "get_realtime_quotes('行业板块')", ef_boards, "东财板块实时行情"),
        ]

    try:
        import adata
    except Exception:
        adata = None
    if adata is not None:
        def adata_five() -> Result:
            table = adata.stock.market.get_market_five(stock_code=bare[0])
            rows = frame_rows(table)
            filled = bool(rows) and any(str(rows[0].get(key, "")).strip() not in {"", "nan"} for key in ("b1", "s1"))
            return Result(ok=filled, seconds=0.0, rows=len(rows), extra={"values_present": filled})

        def adata_min() -> Result:
            table = adata.stock.market.get_market_min(stock_code=bare[0])
            rows = frame_rows(table)
            return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=newest(rows, "trade_time"))

        probes += [
            Probe("adata_five", "adata（多源聚合）", "get_market_five（五档）", adata_five, "五档备份"),
            Probe("adata_min", "adata（多源聚合）", "get_market_min（分时）", adata_min, "分时备份"),
        ]

    try:
        import baostock as bs

        bs.login()
    except Exception:
        bs = None
    if bs is not None:
        def bao_5min() -> Result:
            end = last_session_close(datetime.now()).date()
            rs = bs.query_history_k_data_plus(
                "sh.600519", "date,time,code,open,high,low,close,volume",
                start_date=(end - timedelta(days=7)).isoformat(), end_date=end.isoformat(),
                frequency="5", adjustflag="3",
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            stamp = parse_ts(rows[-1][0]) if rows else None
            return Result(ok=bool(rows), seconds=0.0, rows=len(rows), latest=stamp)

        probes.append(
            Probe("bao_5min", "baostock（独立 TCP）", "query_history_k_data_plus 5min", bao_5min,
                  "历史分钟线/回测底座")
        )

    return probes



# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def audit(probes: list[Probe], repeats: int) -> dict:
    now = datetime.now()
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


def watch(symbols: list[str], seconds: int, interval: float) -> dict:
    """Poll the live feeds on a loop.

    Two questions at once. **In session** it answers "how stale is the feed and
    how often does it actually advance" — the number that decides whether the
    free path can carry an intraday entry. **Outside session** the staleness
    figure is meaningless but the failure rate is not: a feed that starts
    refusing after 200 rapid requests is a feed you cannot poll during a
    session, and that is a rate-limit measurement you can take any day.
    """
    feeds: dict[str, Any] = {}
    bare = [s.split(".")[0] for s in symbols]

    try:
        import easyquotation as eq

        sina = eq.use("sina")
        tencent = eq.use("tencent")

        def sina_one() -> tuple[int, datetime | None]:
            payload = sina.stocks(bare)
            row = next(iter(payload.values()), {})
            return len(payload), parse_ts(f"{row.get('date', '')} {row.get('time', '')}".strip())

        def sina_all() -> tuple[int, datetime | None]:
            payload = sina.market_snapshot(prefix=True)
            row = next(iter(payload.values()), {})
            return len(payload), parse_ts(f"{row.get('date', '')} {row.get('time', '')}".strip())

        def tencent_one() -> tuple[int, datetime | None]:
            payload = tencent.stocks(bare)
            row = next(iter(payload.values()), {})
            return len(payload), parse_ts(row.get("datetime"))

        feeds["sina_quote"] = sina_one
        feeds["sina_market_all"] = sina_all
        feeds["tencent_quote"] = tencent_one
    except Exception as exc:
        print(f"easyquotation unavailable: {exc}")

    try:
        import akshare as ak

        def sina_minute() -> tuple[int, datetime | None]:
            table = ak.stock_zh_a_minute(symbol="sh" + bare[0], period="1", adjust="")
            rows = frame_rows(table)
            return len(rows), newest(rows, "day")

        feeds["sina_minute_1m"] = sina_minute
    except Exception as exc:
        print(f"akshare unavailable: {exc}")

    stats: dict[str, dict] = {
        name: {"calls": 0, "ok": 0, "latencies": [], "staleness": [], "advances": 0, "last_stamp": None, "errors": []}
        for name in feeds
    }

    deadline = time.monotonic() + seconds
    print(f"# 轮询 {seconds}s，间隔 {interval}s，源={list(feeds)}\n")
    while time.monotonic() < deadline:
        for name, fetch in feeds.items():
            entry = stats[name]
            entry["calls"] += 1
            start = time.perf_counter()
            try:
                signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(CALL_TIMEOUT_SECONDS)
                _count, stamp = fetch()
                signal.alarm(0)
                entry["ok"] += 1
                entry["latencies"].append(time.perf_counter() - start)
                if stamp is not None:
                    entry["staleness"].append((datetime.now() - stamp).total_seconds())
                    if entry["last_stamp"] is not None and stamp > entry["last_stamp"]:
                        entry["advances"] += 1
                    entry["last_stamp"] = stamp
            except Exception as exc:
                signal.alarm(0)
                entry["errors"].append(f"{type(exc).__name__}: {exc}"[:120])
        time.sleep(interval)

    now = datetime.now()
    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "in_session": in_session(now),
        "window_seconds": seconds,
        "interval_seconds": interval,
        "feeds": {},
    }
    for name, entry in stats.items():
        latencies = sorted(entry["latencies"])
        stale = sorted(entry["staleness"])
        summary = {
            "calls": entry["calls"],
            "ok": entry["ok"],
            "success_rate": round(entry["ok"] / entry["calls"], 3) if entry["calls"] else 0.0,
            "p50_seconds": round(latencies[len(latencies) // 2], 3) if latencies else None,
            "p95_seconds": round(latencies[int(len(latencies) * 0.95)], 3) if latencies else None,
            "median_staleness_seconds": round(stale[len(stale) // 2], 1) if stale else None,
            "min_staleness_seconds": round(stale[0], 1) if stale else None,
            "timestamp_advanced_times": entry["advances"],
            "errors": sorted(set(entry["errors"]))[:3],
        }
        report["feeds"][name] = summary
        print(
            f"{name:<18} 成功 {summary['success_rate']:>5.0%} ({summary['ok']}/{summary['calls']})  "
            f"p50={summary['p50_seconds']}s p95={summary['p95_seconds']}s  "
            f"数据滞后中位={summary['median_staleness_seconds']}s  时间戳前进={summary['timestamp_advanced_times']}次"
            + (f"  err={summary['errors'][0]}" if summary["errors"] else "")
        )

    if in_session(now):
        print("\n判定依据：交易时段内，'数据滞后中位' 就是真实盘中延迟，'时间戳前进次数' 接近轮询次数才算真更新。")
    else:
        print("\n非交易时段：'数据滞后' 只反映距上次收盘的自然时间，无意义；"
              "本次有效结论只有成功率与延迟——即这些源能否被高频轮询而不被拒。")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3, help="calls per source (stability sample)")
    parser.add_argument("--symbols", nargs="*", default=["600519.SH", "000001.SZ", "300750.SZ"])
    parser.add_argument("--json", default="", help="write the full report here")
    parser.add_argument("--only", nargs="*", default=[], help="probe keys to run (default: all)")
    parser.add_argument("--watch", type=int, default=0, help="poll live feeds for N seconds instead of auditing")
    parser.add_argument("--interval", type=float, default=3.0, help="seconds between polls in --watch mode")
    args = parser.parse_args()

    if args.watch:
        report = watch(args.symbols, args.watch, args.interval)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
            print(f"\nwrote {args.json}")
        return

    try:
        import akshare as ak
    except Exception as exc:
        print(f"akshare unavailable: {exc}")
        ak = None

    tf_client = None
    try:
        import tickflow

        tf_client = tickflow.TickFlow.free()
        connect = getattr(tf_client, "connect", None)
        if callable(connect):
            connect()
    except Exception as exc:
        print(f"tickflow unavailable: {exc}")

    probes = build_probes(ak, tf_client, args.symbols) + build_library_probes(args.symbols)
    if args.only:
        probes = [p for p in probes if p.key in set(args.only)]

    now = datetime.now()
    print(f"# 数据源实测 {now:%Y-%m-%d %H:%M:%S}  交易时段={in_session(now)}  "
          f"上一收盘={last_session_close(now):%Y-%m-%d %H:%M}  repeats={args.repeats}\n")

    report = audit(probes, args.repeats)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
