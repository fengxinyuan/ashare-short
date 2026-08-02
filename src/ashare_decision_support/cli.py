"""Command-line entry point.

系统有两种用法，命令集也按这两种组织：

**自动运行**（主体）——`daemon` 按时间表跑全天：舆情采集 → 盘前主链路 → 执行卡 →
盘中监控 → 盘后复盘 → 兑现核对，结果推到控制台/文件/飞书。见 `docs/AUTOMATION.md`。

**手工插问**——`morning` / `advise` / `monitor` / `chat` / `review` 单独跑同一批任务，
用于调试、补跑，或者你想立刻看一眼。

自选股（`watchlist`）是两者共享的输入：所有接受 `--symbols` 的命令省略它时都读自选股。
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from ashare_decision_support.adapters.akshare_adapter import AkshareAdapter
from ashare_decision_support.adapters.freshness import check_intraday_freshness
from ashare_decision_support.adapters.mapping import get_profile, load_profile_from_path
from ashare_decision_support.adapters.qmt_xtdata import QmtAdapterError, QmtXtDataAdapter
from ashare_decision_support.adapters.sina_intraday import SUPPORTED_PERIODS, SinaIntradayAdapter
from ashare_decision_support.adapters.tickflow_intraday import build_intraday_adapter
from ashare_decision_support.adapters.tickflow_adapter import TickFlowAdapter
from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.chat import run_chat
from ashare_decision_support.jobs import (
    run_advise,
    run_daily_cycle,
    run_monitor,
    run_morning,
    run_post_close,
    run_pre_open,
    run_review,
)
from ashare_decision_support.journal import Journal, summarize
from ashare_decision_support.monitor import DEFAULT_LEVELS, LEVEL_ACTION, LEVEL_INFO, LEVEL_WARN
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier
from ashare_decision_support.notify.feishu import FeishuNotifier
from ashare_decision_support.research import ResearchPipeline, load_research_input
from ashare_decision_support.schedule import TradingCalendar
from ashare_decision_support.signals import WyckoffAnalyzer
from ashare_decision_support.state import CandidatePool, Watchlist, state_home
from ashare_decision_support.symbols import normalize_symbol
from ashare_decision_support.writer import DEFAULT_PROVIDER, PROVIDERS, LlmBriefWriter, TemplateWriter

ALERT_LEVELS = [LEVEL_ACTION, LEVEL_WARN, LEVEL_INFO]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A-share ultra-short decision support CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_tickflow_args(sub) -> None:
        sub.add_argument("--api-key", help="TickFlow API key; omit for free mode")
        sub.add_argument("--base-url", help="Optional TickFlow base URL override")

    def add_writer_args(sub) -> None:
        sub.add_argument(
            "--writer",
            default="template",
            choices=["template", "llm"],
            help="template 确定性；llm 只改写同一份事实，数字对不上即回退",
        )
        sub.add_argument("--llm-provider", default=DEFAULT_PROVIDER, choices=sorted(PROVIDERS))
        sub.add_argument("--llm-model", help="Override the provider's default model")

    def add_notify_args(sub) -> None:
        sub.add_argument("--feishu", action="store_true", help="同时推送到飞书（需 FEISHU_WEBHOOK_URL）")
        sub.add_argument("--quiet", action="store_true", help="不打印简报正文到控制台")

    # ================= 自动运行 =================

    daemon = subparsers.add_parser("daemon", help="[主体] 守护进程：按时间表全天自动运行")
    daemon.add_argument("--output-dir", default="briefs")
    daemon.add_argument("--only", nargs="+", choices=
                        ["news", "morning", "preopen", "monitor", "postclose", "review"],
                        help="只跑指定任务，省略则跑全表")
    daemon.add_argument("--date", help="指定交易日（默认今天）")
    daemon.add_argument("--dry-run", action="store_true", help="只打印时间表，不执行")
    daemon.add_argument("--max-constituents", type=int, help="盘前扫描抽样，用于快速试跑")
    daemon.add_argument("--top-sectors", type=int, default=5)
    daemon.add_argument("--per-sector", type=int, default=3)
    daemon.add_argument("--alert-levels", nargs="+", default=list(DEFAULT_LEVELS), choices=ALERT_LEVELS)
    daemon.add_argument("--no-debate", action="store_true", help="关闭多空辩论层")
    daemon.add_argument("--no-llm-news", action="store_true", help="舆情归因只用关键词词典，不调大模型")
    add_writer_args(daemon)
    add_notify_args(daemon)
    add_tickflow_args(daemon)

    # ================= 主链路各段（可单独跑） =================

    morning = subparsers.add_parser(
        "morning", help="[需求1] 盘前主链路：舆情 → 大盘趋势 → 板块排序 → 板块内选股 → 候选池"
    )
    morning.add_argument("--output-dir", default="briefs")
    morning.add_argument("--level", default="SW1", choices=["SW1", "SW2", "SW3"])
    morning.add_argument("--top-sectors", type=int, default=5)
    morning.add_argument("--per-sector", type=int, default=3)
    morning.add_argument("--position-factor", type=float, help="手工仓位系数 (0-1)，省略用系统建议值")
    morning.add_argument("--no-news", action="store_true", help="跳过舆情与龙虎榜")
    morning.add_argument("--no-llm-news", action="store_true", help="舆情归因只用关键词词典，不调大模型")
    morning.add_argument("--no-limit-pool", action="store_true", help="跳过涨停池情绪读数")
    morning.add_argument(
        "--no-sector-index", action="store_true", help="跳过同花顺行业指数（板块日内位置读数）"
    )
    morning.add_argument(
        "--no-market-scan", action="store_true", help="跳过全市场扫描（可交易性过滤与真实宽度）"
    )
    morning.add_argument("--no-debate", action="store_true", help="关闭多空辩论层")
    morning.add_argument("--max-constituents", type=int, help="按板块等比例抽样，用于快速试跑")
    add_writer_args(morning)
    add_notify_args(morning)
    add_tickflow_args(morning)

    monitor = subparsers.add_parser(
        "monitor", help="[需求3] 盘中监控：自选股 + 候选池，按跃迁告警"
    )
    monitor.add_argument("--symbols", nargs="*", help="省略则读自选股 + 候选池")
    monitor.add_argument("--once", action="store_true", help="只跑一轮，不循环")
    monitor.add_argument("--max-passes", type=int, help="最多跑几轮（调试用）")
    monitor.add_argument("--period", default="1", choices=list(SUPPORTED_PERIODS))
    monitor.add_argument("--alert-levels", nargs="+", default=list(DEFAULT_LEVELS), choices=ALERT_LEVELS)
    monitor.add_argument("--output-dir", default="briefs")
    monitor.add_argument("--no-news", action="store_true", help="跳过公告采集，关闭突发风险公告告警")
    add_notify_args(monitor)
    add_tickflow_args(monitor)

    advise = subparsers.add_parser("advise", help="[需求2/3] 个股买卖点与后续情景推演")
    advise.add_argument("--symbols", nargs="*", help="省略则读自选股")
    advise.add_argument("--output-dir", help="省略则只打印，不落盘")
    advise.add_argument("--period", default="5", choices=list(SUPPORTED_PERIODS))
    advise.add_argument("--no-news", action="store_true", help="跳过个股舆情采集")
    advise.add_argument("--debate", action="store_true", help="开启多空辩论层（需 LLM key）")
    add_writer_args(advise)
    add_notify_args(advise)
    add_tickflow_args(advise)

    chat = subparsers.add_parser("chat", help="[需求4] 持续对话：对已算出的事实多轮追问")
    chat.add_argument("--symbols", nargs="*", help="省略则读自选股")
    chat.add_argument("--period", default="5", choices=list(SUPPORTED_PERIODS))
    chat.add_argument("--llm-provider", default=DEFAULT_PROVIDER, choices=sorted(PROVIDERS))
    chat.add_argument("--llm-model", help="Override the provider's default model")
    add_tickflow_args(chat)

    # ================= 自选股 =================

    watch = subparsers.add_parser("watchlist", help="自选股：所有命令的默认标的来源")
    watch_sub = watch.add_subparsers(dest="action", required=True)
    watch_add = watch_sub.add_parser("add", help="加入自选")
    watch_add.add_argument("symbols", nargs="+")
    watch_add.add_argument("--note", help="备注")
    watch_rm = watch_sub.add_parser("remove", help="移出自选")
    watch_rm.add_argument("symbols", nargs="+")
    watch_sub.add_parser("list", help="列出自选与今日候选池")
    watch_sub.add_parser("clear", help="清空自选")

    # 持仓状态：决定这只票该被盯买点还是盯卖点。系统只出建议，不下单——
    # 这些字段全部手工维护。
    watch_hold = watch_sub.add_parser("hold", help="标记为已持仓（盯卖点）")
    watch_hold.add_argument("symbol")
    watch_hold.add_argument("--cost", type=float, required=True, help="成本价")
    watch_hold.add_argument("--shares", type=int, help="股数")
    watch_hold.add_argument("--opened-on", help="建仓日 YYYY-MM-DD，默认今天")
    watch_hold.add_argument("--stop", type=float, help="你自己定的止损，优先于系统值")
    watch_hold.add_argument("--target", type=float, help="你自己定的目标")
    watch_close = watch_sub.add_parser("close", help="清掉持仓状态，仍留在自选")
    watch_close.add_argument("symbols", nargs="+")

    # ================= 台账与复盘 =================

    review = subparsers.add_parser("review", help="兑现核对：T+1..T+5 对照实际走势，并出统计")
    review.add_argument("--horizon", type=int, default=5, help="核对窗口（交易日）")
    review.add_argument("--output-dir", default="briefs")
    add_notify_args(review)
    add_tickflow_args(review)

    journal_cmd = subparsers.add_parser("journal", help="查台账：某只票的历史建议与兑现")
    journal_cmd.add_argument("--symbol", help="省略则出整体统计")
    journal_cmd.add_argument("--limit", type=int, default=20)

    # ================= 日常闭环（既有） =================

    post_close = subparsers.add_parser("post-close-brief", help="盘后：自选股打分与次日矩阵")
    post_close.add_argument("--symbols", nargs="*", help="省略则读自选股")
    post_close.add_argument("--output-dir", default="briefs")
    post_close.add_argument("--days", type=int, default=180)
    post_close.add_argument("--top", type=int)
    post_close.add_argument("--strategy-version", default="rule-v1")
    post_close.add_argument("--position-factor", type=float, default=0.5)
    post_close.add_argument("--research-input", help="可选 ResearchInput JSON")
    post_close.add_argument("--no-debate", action="store_true", help="关闭多空辩论层")
    add_tickflow_args(post_close)

    pre_open = subparsers.add_parser("pre-open-brief", help="次日盘前：把昨日矩阵复述成执行卡")
    pre_open.add_argument("--matrix", required=True)
    pre_open.add_argument("--output-dir")
    pre_open.add_argument("--refresh", action="store_true", help="重新取价而非复用盘后快照")
    add_tickflow_args(pre_open)

    build_matrix = subparsers.add_parser("build-matrix", help="从结构化研究输入构建 DecisionMatrix")
    build_matrix.add_argument("--input", required=True)
    build_matrix.add_argument("--output")

    wyckoff_scan = subparsers.add_parser("wyckoff-scan", help="历史结构（威科夫）打分")
    wyckoff_scan.add_argument("--symbols", nargs="+", required=True)
    wyckoff_scan.add_argument("--source", default="tickflow", choices=["tickflow", "akshare", "qmt"])
    wyckoff_scan.add_argument("--start")
    wyckoff_scan.add_argument("--end")
    wyckoff_scan.add_argument("--output")
    wyckoff_scan.add_argument("--calendar-market", default="SH")
    wyckoff_scan.add_argument("--profile-file")
    add_tickflow_args(wyckoff_scan)

    # ================= 诊断 =================

    tick_health = subparsers.add_parser("tick-health", help="诊断：TickFlow 连通性与能力边界")
    add_tickflow_args(tick_health)

    intraday_check = subparsers.add_parser(
        "intraday-check",
        help="诊断：实测分钟线盘中时效（必须在交易时段跑）",
    )
    intraday_check.add_argument("--symbols", nargs="+", default=["600519.SH", "000001.SZ"])
    intraday_check.add_argument("--period", default="1", choices=list(SUPPORTED_PERIODS))
    intraday_check.add_argument("--gap-seconds", type=int, default=90)
    # 要比的是两条路的**相对**时效：付费档与新浪相当，就说明买到的是分钟历史与
    # 配额而不是实时性；付费档明显更新，实时这条线就该切过去。
    intraday_check.add_argument(
        "--source",
        default="auto",
        choices=["auto", "tickflow", "sina"],
        help="auto=按档位自动选；tickflow/sina=强制走某一路，用于两路对比",
    )

    subparsers.add_parser("qmt-health", help="诊断：本地 QMT / xtdata 是否就绪")
    subparsers.add_parser("state", help="诊断：状态目录、自选股、候选池、任务完成情况")
    return parser


# ---- helpers -------------------------------------------------------------


def _dump_json(payload: object, output_path: str | None = None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.write("\n")
    print(rendered)


def _trim(summary: dict, drop: set[str] = frozenset({"content", "facts"})) -> dict:
    return {key: value for key, value in summary.items() if key not in drop}


def _tickflow(args) -> TickFlowAdapter:
    return TickFlowAdapter(
        api_key=getattr(args, "api_key", None), base_url=getattr(args, "base_url", None)
    )


def _writer(args):
    if getattr(args, "writer", "template") == "llm":
        return LlmBriefWriter(provider=args.llm_provider, model=args.llm_model)
    return TemplateWriter()


def _debate(args) -> DebatePanel | None:
    if getattr(args, "no_debate", False):
        return None
    panel = DebatePanel(
        provider=getattr(args, "llm_provider", DEFAULT_PROVIDER),
        model=getattr(args, "llm_model", None),
    )
    # 没配 key 就不挂这一层，省得每次跑都刷一条同样的提示。
    return panel if panel.client.configured else None


def _news_source(cache_dir: str | Path | None):
    """公告与舆情源。akshare 没装就返回 None，调用方按缺输入降级。"""
    try:
        from ashare_decision_support.news import AkshareNewsSource, NewsCache

        return AkshareNewsSource(cache=NewsCache(cache_dir) if cache_dir else None)
    except Exception:
        return None


def _notifiers(args, output_dir: str | Path | None, slug: str) -> list:
    """控制台与文件始终保留——飞书挂了，本地仍有完整记录。"""
    channels: list = []
    if not getattr(args, "quiet", False):
        channels.append(ConsoleNotifier())
    if output_dir is not None:
        channels.append(FileNotifier(output_dir, slug=slug))
    if getattr(args, "feishu", False):
        feishu = FeishuNotifier()
        if not feishu.configured:
            print("⚠️  未配置 FEISHU_WEBHOOK_URL，本次不推飞书")
        else:
            channels.append(feishu)
    return channels


def _resolve_symbols(args, include_candidates: bool = False) -> list[str]:
    """`--symbols` 缺省即读自选股，这样自动与手工两种用法看到的是同一批标的。"""
    explicit = getattr(args, "symbols", None)
    if explicit:
        return [normalize_symbol(item) for item in explicit]

    symbols = Watchlist().symbols()
    if include_candidates:
        symbols = list(dict.fromkeys(symbols + CandidatePool().symbols()))
    return symbols


def _source_adapter(args):
    if args.source == "tickflow":
        return _tickflow(args)
    if args.source == "akshare":
        return AkshareAdapter()
    profile = load_profile_from_path(args.profile_file) if args.profile_file else get_profile("generic-qmt")
    return QmtXtDataAdapter(profile=profile, calendar_market=args.calendar_market)


def _optional_date(value: str | None, fallback: date) -> date:
    return date.fromisoformat(value) if value else fallback


def _qmt_health() -> dict:
    """QMT 未开通是预期状态，诊断命令要如实报告而不是抛栈。"""
    try:
        payload = QmtXtDataAdapter(profile=get_profile("generic-qmt")).health_check()
    except QmtAdapterError as exc:
        return {"adapter": "QmtXtDataAdapter", "ready": False, "connected": False, "reason": str(exc)}
    payload["ready"] = True
    return payload


def _morning_facts(adapter) -> dict:
    """板块与大盘读数，作为 chat 的上下文。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        run_morning(
            adapter=adapter,
            output_dir=Path(tmp),
            top_sectors=8,
            candidates_per_sector=2,
            notifiers=[],
            journal=Journal(root=Path(tmp) / "journal"),
        )
        return json.loads((Path(tmp) / "morning_latest.json").read_text(encoding="utf-8"))


# ---- dispatch ------------------------------------------------------------


def main() -> None:
    args = build_parser().parse_args()

    if args.command == "daemon":
        summary = run_daily_cycle(
            adapter_factory=lambda: _tickflow(args),
            output_dir=args.output_dir,
            trade_date=_optional_date(getattr(args, "date", None), date.today()),
            only=args.only,
            dry_run=args.dry_run,
            writer_factory=lambda: _writer(args),
            debate_factory=lambda: _debate(args),
            notifier_factory=lambda slug: _notifiers(args, args.output_dir, slug),
            top_sectors=args.top_sectors,
            candidates_per_sector=args.per_sector,
            max_constituents=args.max_constituents,
            alert_levels=tuple(args.alert_levels),
        )
        _dump_json(_trim(summary))
        return

    if args.command == "morning":
        summary = run_morning(
            adapter=_tickflow(args),
            output_dir=args.output_dir,
            sector_level=args.level,
            top_sectors=args.top_sectors,
            candidates_per_sector=args.per_sector,
            position_factor=args.position_factor,
            max_constituents=args.max_constituents,
            use_news=not args.no_news,
            use_llm_news=not args.no_llm_news,
            use_limit_pool=not args.no_limit_pool,
            use_sector_index=not args.no_sector_index,
            use_market_scan=not args.no_market_scan,
            writer=_writer(args),
            debate=_debate(args),
            notifiers=_notifiers(args, args.output_dir, "morning"),
        )
        _dump_json(_trim(summary))
        return

    if args.command == "monitor":
        summary = run_monitor(
            adapter=_tickflow(args),
            symbols=_resolve_symbols(args, include_candidates=True) if args.symbols else None,
            period=args.period,
            once=args.once,
            max_passes=args.max_passes,
            alert_levels=tuple(args.alert_levels),
            news_source=None if getattr(args, "no_news", False) else _news_source(args.output_dir),
            notifiers=_notifiers(args, args.output_dir, "alerts"),
        )
        _dump_json(_trim(summary))
        return

    if args.command == "advise":
        summary = run_advise(
            adapter=_tickflow(args),
            symbols=_resolve_symbols(args),
            output_dir=args.output_dir,
            period=args.period,
            use_news=not args.no_news,
            debate=_debate(args) if args.debate else None,
            writer=_writer(args),
            notifiers=_notifiers(args, args.output_dir, "advice"),
        )
        _dump_json(_trim(summary))
        return

    if args.command == "chat":
        adapter = _tickflow(args)

        def compute_symbols(symbols: list[str]) -> dict:
            return run_advise(adapter=adapter, symbols=symbols, period=args.period, notifiers=[])["facts"]

        run_chat(
            compute_symbols=compute_symbols,
            compute_sectors=lambda: _morning_facts(adapter),
            symbols=_resolve_symbols(args),
        )
        return

    if args.command == "watchlist":
        _dump_json(_watchlist_command(args))
        return

    if args.command == "review":
        summary = run_review(
            adapter=_tickflow(args),
            horizon=args.horizon,
            notifiers=_notifiers(args, args.output_dir, "review"),
        )
        _dump_json(_trim(summary))
        return

    if args.command == "journal":
        journal = Journal()
        if args.symbol:
            _dump_json(journal.history_for(args.symbol, limit=args.limit))
        else:
            _dump_json(summarize(journal.outcomes()))
        return

    if args.command == "post-close-brief":
        summary = run_post_close(
            adapter=_tickflow(args),
            symbols=_resolve_symbols(args),
            output_dir=args.output_dir,
            days=args.days,
            strategy_version=args.strategy_version,
            position_factor=args.position_factor,
            research_input_path=args.research_input,
            top_n=args.top,
            debate=_debate(args),
        )
        _dump_json(_trim(summary))
        return

    if args.command == "pre-open-brief":
        summary = run_pre_open(
            matrix_path=args.matrix,
            output_dir=args.output_dir,
            adapter=_tickflow(args) if args.refresh else None,
        )
        _dump_json(_trim(summary))
        return

    if args.command == "build-matrix":
        matrix = ResearchPipeline().build_matrix(load_research_input(args.input))
        _dump_json(matrix.model_dump(mode="json"), args.output)
        return

    if args.command == "wyckoff-scan":
        adapter = _source_adapter(args)
        end = _optional_date(args.end, date.today())
        start = _optional_date(args.start, end.replace(year=end.year - 1))
        bars = adapter.get_historical_bars(args.symbols, start, end)
        signals = WyckoffAnalyzer().analyze_many(bars)
        _dump_json([signal.model_dump(mode="json") for signal in signals], args.output)
        return

    if args.command == "tick-health":
        _dump_json(_tickflow(args).health_check())
        return

    if args.command == "intraday-check":
        # 盘中时效要测的是**实际在跑的那条链路**，拿另一条的读数交差没有意义。
        # `--source` 可以强制某一路，用来做两路的时效对比。
        if args.source == "sina":
            source = SinaIntradayAdapter()
        else:
            source = build_intraday_adapter(_tickflow(args)) or SinaIntradayAdapter()
        _dump_json(
            check_intraday_freshness(
                source,
                symbols=args.symbols,
                period=args.period,
                gap_seconds=args.gap_seconds,
            )
        )
        return

    if args.command == "qmt-health":
        _dump_json(_qmt_health())
        return

    if args.command == "state":
        _dump_json(_state_report())
        return


def _watchlist_command(args) -> dict:
    book = Watchlist()
    if args.action == "add":
        added, updated = [], []
        for symbol in args.symbols:
            entry, created = book.add(symbol, note=args.note)
            (added if created else updated).append(entry.symbol)
        return {"added": added, "updated": updated, "total": len(book), "path": str(book.path)}

    if args.action == "remove":
        removed = [symbol for symbol in args.symbols if book.remove(symbol)]
        missing = [normalize_symbol(s) for s in args.symbols if normalize_symbol(s) not in removed]
        return {"removed": removed, "not_found": missing, "total": len(book)}

    if args.action == "clear":
        return {"cleared": book.clear(), "total": 0}

    if args.action == "hold":
        entry = book.set_position(
            args.symbol,
            cost_price=args.cost,
            shares=args.shares,
            opened_on=date.fromisoformat(args.opened_on) if args.opened_on else None,
            plan_stop=args.stop,
            plan_target=args.target,
        )
        return {"holding": entry.to_dict(), "total_holdings": len(book.holdings())}

    if args.action == "close":
        closed = [symbol for symbol in args.symbols if book.close_position(symbol)]
        return {"closed": closed, "total_holdings": len(book.holdings())}

    pool = CandidatePool()
    return {
        "watchlist": [entry.to_dict() for entry in book.entries],
        "holdings": [entry.symbol for entry in book.holdings()],
        "candidates": [entry.to_dict() for entry in pool.entries],
        "candidates_trade_date": pool.trade_date,
        "candidates_stale": pool.stale,
        "path": str(book.path),
    }


def _state_report() -> dict:
    from ashare_decision_support.state import AlertState, DaemonState

    today = date.today()
    book, pool = Watchlist(), CandidatePool(today=today)
    alerts = AlertState.load(today)
    daemon = DaemonState.load(today)
    calendar = TradingCalendar()
    journal = Journal()
    outcomes = journal.outcomes()
    return {
        "state_home": str(state_home()),
        "today": str(today),
        "trading_day": calendar.explain(today),
        "watchlist": book.symbols(),
        "candidates": pool.symbols(),
        "candidates_stale": pool.stale,
        "alerts_today": alerts.fired_summary()[:20],
        "daemon_tasks": daemon.tasks,
        "journal": {
            "recommendations": len(journal.recommendations()),
            "outcomes": len(outcomes),
            "pending": len(journal.pending(today)),
            "model_ready_gap": summarize(outcomes)["model_ready_gap"],
        },
        "feishu_configured": FeishuNotifier().configured,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    main()
