"""Command-line entry point.

系统有两种用法，命令集也按这两种组织：

**自动运行**（主体）——`daemon` 按时间表跑全天：舆情采集 → 盘前主链路 → 执行卡 →
盘中监控 → 盘后复盘 → 兑现核对，结果推到控制台/文件/飞书。见 `docs/ARCHITECTURE.md`。

**手工插问**——`morning` / `advise` / `monitor` / `chat` / `review` 单独跑同一批任务，
用于调试、补跑，或者你想立刻看一眼。

自选股（`watchlist`）是两者共享的输入：所有接受 `--symbols` 的命令省略它时都读自选股。
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path

from ashare_decision_support.adapters.akshare_adapter import AkshareAdapter
from ashare_decision_support.adapters.base import fetch_daily_bars
from ashare_decision_support.adapters.freshness import check_intraday_freshness
from ashare_decision_support.adapters.global_market import GlobalMarketAdapter
from ashare_decision_support.adapters.mapping import get_profile, load_profile_from_path
from ashare_decision_support.adapters.qmt_xtdata import QmtAdapterError, QmtXtDataAdapter
from ashare_decision_support.adapters.security_master import SecurityMasterAdapter
from ashare_decision_support.adapters.sina_intraday import SUPPORTED_PERIODS, SinaIntradayAdapter
from ashare_decision_support.adapters.tickflow_adapter import TickFlowAdapter
from ashare_decision_support.adapters.tickflow_intraday import build_intraday_adapter
from ashare_decision_support.advice import (
    LevelAdjudicator,
    build_adjudication_packet,
    validate_external_decisions,
)
from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.ask import run_ask
from ashare_decision_support.chat import run_chat
from ashare_decision_support.chat.session import ChatResponder
from ashare_decision_support.envfile import load_env_files, loaded_files
from ashare_decision_support.facts import build_facts_bundle
from ashare_decision_support.jobs import (
    MorningConfig,
    Sources,
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
from ashare_decision_support.schedule import TradingCalendar
from ashare_decision_support.schedule.timezone import beijing_now, beijing_today
from ashare_decision_support.sectors import ThsIndustryTaxonomy
from ashare_decision_support.signals import WyckoffAnalyzer
from ashare_decision_support.state import CandidatePool, Watchlist, state_home
from ashare_decision_support.strategy.overnight import (
    PROFILES,
    StrategyScreenConfig,
    diversified_shortlist,
    screen_overnight,
)
from ashare_decision_support.symbols import normalize_symbol
from ashare_decision_support.writer import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    LlmBriefWriter,
    TemplateWriter,
)
from ashare_decision_support.writer.llm import LlmClient

ALERT_LEVELS = [LEVEL_ACTION, LEVEL_WARN, LEVEL_INFO]


# 三组会被多条命令重复添加的参数。提到模块层而不是留在 `build_parser` 里做闭包，
# 是为了让每条命令的注册函数能独立存在——否则拆出去的函数还得把它们当参数传。
def _add_tickflow_args(sub) -> None:
    sub.add_argument("--api-key", help="TickFlow API key; omit for free mode")
    sub.add_argument("--base-url", help="Optional TickFlow base URL override")


def _add_writer_args(sub) -> None:
    sub.add_argument(
        "--writer",
        default="template",
        choices=["auto", "template", "llm"],
        help="默认 template 只走确定性；auto=有 key 用 LLM 改写，无 key 回退模板；llm 强制用 LLM",
    )
    sub.add_argument("--llm-provider", default=DEFAULT_PROVIDER, choices=sorted(PROVIDERS))
    sub.add_argument("--llm-model", help="Override the provider's default model")


def _add_notify_args(sub) -> None:
    sub.add_argument(
        "--feishu",
        action="store_true",
        help="同时推送到飞书（需 webhook，或应用凭证 + receive_id）",
    )
    sub.add_argument("--quiet", action="store_true", help="不打印简报正文到控制台")


def _add_llm_args(sub) -> None:
    sub.add_argument("--llm-provider", default=DEFAULT_PROVIDER, choices=sorted(PROVIDERS))
    sub.add_argument("--llm-model", help="Override the provider's default model")


# ---- 每条命令一个注册函数 ------------------------------------------------
# 一条命令的全部参数收在一个函数里，加命令 = 加一个函数 + 在 `build_parser` 里
# 加一行，而不是往一段两百行的顺序代码里再插十行。


def _parser_daemon(subparsers) -> None:
    daemon = subparsers.add_parser("daemon", help="[主体] 守护进程：按时间表全天自动运行")
    daemon.add_argument("--output-dir", default="briefs")
    daemon.add_argument(
        "--only",
        nargs="+",
        choices=["news", "morning", "preopen", "monitor", "postclose", "review"],
        help="只跑指定任务，省略则跑全表",
    )
    daemon.add_argument("--date", help="指定交易日（默认今天）")
    daemon.add_argument("--dry-run", action="store_true", help="只打印时间表，不执行")
    daemon.add_argument("--max-constituents", type=int, help="盘前扫描抽样，用于快速试跑")
    daemon.add_argument("--top-sectors", type=int, default=5)
    daemon.add_argument("--per-sector", type=int, default=3)
    daemon.add_argument(
        "--alert-levels", nargs="+", default=list(DEFAULT_LEVELS), choices=ALERT_LEVELS
    )
    daemon.add_argument("--no-debate", action="store_true", help="关闭多空辩论层")
    daemon.add_argument(
        "--no-llm-news", action="store_true", help="舆情归因只用关键词词典，不调大模型"
    )
    _add_writer_args(daemon)
    _add_notify_args(daemon)
    _add_tickflow_args(daemon)


def _parser_morning(subparsers) -> None:
    morning = subparsers.add_parser(
        "morning", help="[需求1] 盘前趋势分析：舆情 → 大盘趋势 → 板块排序 → 板块内选股 → 候选池"
    )
    morning.add_argument("--output-dir", default="briefs")
    morning.add_argument("--level", default="SW1", choices=["SW1", "SW2", "SW3"])
    morning.add_argument("--top-sectors", type=int, default=5)
    morning.add_argument("--per-sector", type=int, default=3)
    morning.add_argument(
        "--position-factor", type=float, help="手工仓位系数 (0-1)，省略用系统建议值"
    )
    morning.add_argument("--no-news", action="store_true", help="跳过舆情与龙虎榜")
    morning.add_argument(
        "--no-llm-news", action="store_true", help="舆情归因只用关键词词典，不调大模型"
    )
    morning.add_argument("--no-limit-pool", action="store_true", help="跳过涨停池情绪读数")
    morning.add_argument(
        "--no-sector-index", action="store_true", help="跳过同花顺行业指数（板块日内位置读数）"
    )
    morning.add_argument(
        "--no-market-scan", action="store_true", help="跳过全市场扫描（可交易性过滤与真实宽度）"
    )
    morning.add_argument(
        "--no-broad-index",
        action="store_true",
        help="跳过宽基指数（市值加权口径，用来对照等权宽度）",
    )
    morning.add_argument(
        "--no-trade-flow",
        action="store_true",
        help="跳过候选池的逐笔资金旁证（大单净额、尾盘净额），可省约 2 分钟",
    )
    morning.add_argument(
        "--no-earnings",
        action="store_true",
        help="跳过业绩预告（业绩杀否决与业绩催化标注）",
    )
    morning.add_argument("--no-debate", action="store_true", help="关闭多空辩论层")
    morning.add_argument("--max-constituents", type=int, help="按板块等比例抽样，用于快速试跑")
    morning.add_argument(
        "--explain",
        action="store_true",
        help="显示结构化筛选漏斗（覆盖、筛选、最终候选与对照样本）",
    )
    _add_writer_args(morning)
    _add_notify_args(morning)
    _add_tickflow_args(morning)


def _parser_monitor(subparsers) -> None:
    monitor = subparsers.add_parser(
        "monitor", help="[需求3] 自选股价格监控：自选股 + 候选池，按跃迁告警"
    )
    monitor.add_argument("--symbols", nargs="*", help="省略则读自选股 + 候选池")
    monitor.add_argument("--once", action="store_true", help="只跑一轮，不循环")
    monitor.add_argument("--max-passes", type=int, help="最多跑几轮（调试用）")
    monitor.add_argument("--period", default="1", choices=list(SUPPORTED_PERIODS))
    monitor.add_argument(
        "--alert-levels", nargs="+", default=list(DEFAULT_LEVELS), choices=ALERT_LEVELS
    )
    monitor.add_argument("--output-dir", default="briefs")
    monitor.add_argument(
        "--no-order-book", action="store_true", help="盘中不取五档（少一次批量请求）"
    )
    monitor.add_argument(
        "--no-news", action="store_true", help="跳过公告采集，关闭突发风险公告告警"
    )
    _add_notify_args(monitor)
    _add_tickflow_args(monitor)


def _parser_advise(subparsers) -> None:
    advise = subparsers.add_parser("advise", help="[需求2/3] 个股股价分析与买卖点推荐")
    advise.add_argument("--symbols", nargs="*", help="省略则读自选股")
    advise.add_argument("--output-dir", help="省略则只打印，不落盘")
    advise.add_argument("--period", default="5", choices=list(SUPPORTED_PERIODS))
    advise.add_argument("--no-news", action="store_true", help="跳过个股舆情采集")
    advise.add_argument("--debate", action="store_true", help="开启多空辩论层（需 LLM key）")
    advise.add_argument(
        "--adjudicate",
        action="store_true",
        help="开启买卖点裁决层：模型在规则给的候选价里取舍，可弃权（需 LLM key）",
    )
    advise.add_argument("--no-order-book", action="store_true", help="不取五档，候选集少挂单墙两类")
    advise.add_argument("--no-earnings", action="store_true", help="不提示业绩风险与业绩催化")
    _add_writer_args(advise)
    _add_notify_args(advise)
    _add_tickflow_args(advise)


def _parser_ask(subparsers) -> None:
    ask = subparsers.add_parser(
        "ask",
        help="[需求4] 单入口问答：趋势、股价、监控结果都能问",
    )
    ask.add_argument("question", nargs="*", help="省略则只打印装配好的上下文，不调模型")
    ask.add_argument("--symbols", nargs="*", help="额外加入的标的；自选与候选池自动带上")
    ask.add_argument("--output-dir", default="briefs", help="读盘前/盘后落盘结果的目录")
    ask.add_argument("--period", default="5", choices=list(SUPPORTED_PERIODS))
    ask.add_argument("--no-live", action="store_true", help="盘中也不重算价位，只读已落盘的")
    ask.add_argument(
        "--deep",
        action="store_true",
        help="分步研究：解析问题 → 研究计划 → 逐步论证 → 决策卡 + 排序 + 价位轴",
    )
    ask.add_argument("--llm", action="store_true", help="显式开启模型回答；默认只输出事实包")
    _add_llm_args(ask)
    _add_tickflow_args(ask)


def _parser_facts(subparsers) -> None:
    facts = subparsers.add_parser(
        "facts",
        help="[Codex/Claude] 导出统一事实包：默认 JSON，不调模型",
    )
    facts.add_argument("--symbols", nargs="*", help="额外加入的标的；自选与候选池自动带上")
    facts.add_argument("--output-dir", default="briefs", help="读盘前/盘后落盘结果的目录")
    facts.add_argument("--period", default="5", choices=list(SUPPORTED_PERIODS))
    facts.add_argument("--no-live", action="store_true", help="盘中也不现算价位，只读已落盘的")
    facts.add_argument(
        "--format",
        default="json",
        choices=["json", "text"],
        help="json=结构化事实包；text=直接输出人类可读摘要",
    )
    facts.add_argument("--output", help="把 JSON bundle 写到文件；text 模式忽略")
    _add_tickflow_args(facts)


def _parser_chat(subparsers) -> None:
    chat = subparsers.add_parser(
        "chat", help="[需求4] 持续对话：对趋势、股价、监控与买卖点多轮追问"
    )
    chat.add_argument("--symbols", nargs="*", help="省略则读自选股")
    chat.add_argument("--period", default="5", choices=list(SUPPORTED_PERIODS))
    chat.add_argument("--llm", action="store_true", help="显式开启模型对话；默认只输出事实包")
    _add_llm_args(chat)
    _add_tickflow_args(chat)


def _parser_preopen_research(subparsers) -> None:
    research = subparsers.add_parser(
        "preopen-research",
        help="[Codex] 导出盘前研报包：已落盘 facts + 海外市场快照 + 固定研判要求",
    )
    research.add_argument("--output-dir", default="briefs", help="读取 morning_latest.json 的目录")
    research.add_argument("--output", help="写入 JSON 研报包；省略则打印到控制台")
    research.add_argument("--format", choices=["json", "markdown"], default="json")
    research.add_argument(
        "--no-global", action="store_true", help="跳过海外市场快照，只导出已落盘 facts"
    )


def _parser_watchlist(subparsers) -> None:
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


def _parser_review(subparsers) -> None:
    review = subparsers.add_parser("review", help="兑现核对：T+1..T+5 对照实际走势，并出统计")
    review.add_argument("--horizon", type=int, default=5, help="核对窗口（交易日）")
    review.add_argument("--output-dir", default="briefs")
    _add_notify_args(review)
    _add_tickflow_args(review)


def _parser_journal(subparsers) -> None:
    journal_cmd = subparsers.add_parser("journal", help="查台账：某只票的历史建议与兑现")
    journal_cmd.add_argument("--symbol", help="省略则出整体统计")
    journal_cmd.add_argument("--limit", type=int, default=20)


def _parser_codex_adjudication(subparsers) -> None:
    packet = subparsers.add_parser(
        "codex-adjudication-packet",
        help="[Codex] 从统一事实包导出动态买卖点候选与裁决协议",
    )
    packet.add_argument("--facts", required=True, help="facts 命令导出的 JSON bundle")
    packet.add_argument("--output", help="写入裁决包 JSON")

    validate = subparsers.add_parser(
        "codex-adjudication-validate",
        help="[Codex] 校验外部模型对动态候选键的选择，不生成订单",
    )
    validate.add_argument("--packet", required=True, help="codex-adjudication-packet 导出的 JSON")
    validate.add_argument("--decision", required=True, help="Codex 返回的 decisions JSON")
    validate.add_argument("--max-age-minutes", type=int, default=15)
    validate.add_argument("--output", help="写入校验结果 JSON")


def _parser_post_close(subparsers) -> None:
    post_close = subparsers.add_parser("post-close-brief", help="盘后：自选股打分与次日执行卡")
    post_close.add_argument("--symbols", nargs="*", help="省略则读自选股")
    post_close.add_argument("--output-dir", default="briefs")
    post_close.add_argument("--days", type=int, default=180)
    post_close.add_argument("--top", type=int)
    post_close.add_argument("--strategy-version", default="rule-v1")
    post_close.add_argument(
        "--position-factor", type=float, help="手工仓位系数 (0-1)，省略用情绪周期上限"
    )
    post_close.add_argument("--no-limit-pool", action="store_true", help="跳过涨停池情绪读数")
    post_close.add_argument("--no-debate", action="store_true", help="关闭多空辩论层")
    _add_tickflow_args(post_close)


def _parser_pre_open(subparsers) -> None:
    pre_open = subparsers.add_parser("pre-open-brief", help="次日盘前：把昨日执行卡复述一遍")
    pre_open.add_argument(
        "--payload", help="盘后落盘的 post_close_latest.json，省略则读 briefs/ 下最新一份"
    )
    pre_open.add_argument("--output-dir")
    pre_open.add_argument("--refresh", action="store_true", help="重新取价而非复用盘后快照")
    _add_tickflow_args(pre_open)


def _parser_wyckoff_scan(subparsers) -> None:
    wyckoff_scan = subparsers.add_parser("wyckoff-scan", help="历史结构（威科夫）打分")
    wyckoff_scan.add_argument("--symbols", nargs="+", required=True)
    wyckoff_scan.add_argument(
        "--source", default="tickflow", choices=["tickflow", "akshare", "qmt"]
    )
    wyckoff_scan.add_argument("--start")
    wyckoff_scan.add_argument("--end")
    wyckoff_scan.add_argument("--output")
    wyckoff_scan.add_argument("--calendar-market", default="SH")
    wyckoff_scan.add_argument("--profile-file")
    _add_tickflow_args(wyckoff_scan)


def _parser_strategy_screen(subparsers) -> None:
    screen = subparsers.add_parser(
        "strategy-screen",
        help="全市场参数化策略选股：输出给 AI 的确定性候选事实",
    )
    screen.add_argument(
        "--profile",
        choices=[*sorted(PROFILES), "all"],
        default="high-elasticity",
        help="策略形态；all 一次运行全部策略并输出多样化短名单",
    )
    screen.add_argument("--price-min", type=float, default=3.0)
    screen.add_argument("--price-max", type=float, default=60.0)
    screen.add_argument("--min-amount-yi", type=float, default=5.0)
    screen.add_argument("--allow-st", action="store_true", help="兼容旧命令：默认已纳入 ST/退市风险标的")
    screen.add_argument("--exclude-st", action="store_true", help="显式排除名称含 ST/退的标的")
    screen.add_argument("--all-a", action="store_true", help="不限定沪深主板，纳入可识别的其他 A 股")
    screen.add_argument("--limit", type=int, default=30)
    screen.add_argument("--days", type=int, default=240, help="日线回溯天数")
    screen.add_argument("--min-history-bars", type=int, default=65, help="最少有效日线根数")
    screen.add_argument("--output-dir", default="briefs")
    screen.add_argument("--output", help="另写一份 JSON；省略则写 output-dir/strategy_screen_latest.json")
    screen.add_argument("--min-atr-pct", type=float)
    screen.add_argument("--max-atr-pct", type=float)
    screen.add_argument("--min-range-position", type=float)
    screen.add_argument("--max-range-position", type=float)
    screen.add_argument("--min-pullback-pct", type=float)
    screen.add_argument("--max-pullback-pct", type=float)
    screen.add_argument("--min-close-vs-ma20-pct", type=float)
    screen.add_argument("--max-volume-contraction", type=float)
    screen.add_argument("--min-volume-ratio", type=float)
    screen.add_argument("--max-volume-ratio", type=float)
    screen.add_argument("--min-close-position", type=float)
    screen.add_argument("--max-close-position", type=float)
    _add_tickflow_args(screen)


def _parser_diagnostics(subparsers) -> None:
    tick_health = subparsers.add_parser("tick-health", help="诊断：TickFlow 连通性与能力边界")
    _add_tickflow_args(tick_health)

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A-share ultra-short analysis CLI: trend, price, monitor, chat"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 自动运行
    _parser_daemon(subparsers)
    # 主链路各段（可单独跑）
    _parser_morning(subparsers)
    _parser_monitor(subparsers)
    _parser_advise(subparsers)
    _parser_ask(subparsers)
    _parser_facts(subparsers)
    _parser_preopen_research(subparsers)
    _parser_chat(subparsers)
    # 自选股
    _parser_watchlist(subparsers)
    # 台账与复盘
    _parser_review(subparsers)
    _parser_journal(subparsers)
    _parser_codex_adjudication(subparsers)
    # 日常闭环
    _parser_post_close(subparsers)
    _parser_pre_open(subparsers)
    _parser_wyckoff_scan(subparsers)
    _parser_strategy_screen(subparsers)
    # 诊断
    _parser_diagnostics(subparsers)
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
    mode = getattr(args, "writer", "auto")
    if mode == "template":
        return TemplateWriter()
    llm = LlmBriefWriter(provider=args.llm_provider, model=args.llm_model)
    if mode == "llm":
        return llm
    return llm if llm.api_key else TemplateWriter()


def _progress(args):
    if getattr(args, "quiet", False):
        return None
    return lambda message: print(message, flush=True)


def _debate(args) -> DebatePanel | None:
    if getattr(args, "no_debate", False):
        return None
    panel = DebatePanel(
        provider=getattr(args, "llm_provider", DEFAULT_PROVIDER),
        model=getattr(args, "llm_model", None),
    )
    # 没配 key 就不挂这一层，省得每次跑都刷一条同样的提示。
    return panel if panel.client.configured else None


def _adjudicator(args) -> LevelAdjudicator | None:
    """买卖点裁决层。没配 key 就不挂——它的兜底本来就是规则默认排序，
    挂一个必然回退的裁决器只会每只票刷一条同样的提示。"""
    judge = LevelAdjudicator(
        provider=getattr(args, "llm_provider", DEFAULT_PROVIDER),
        model=getattr(args, "llm_model", None),
    )
    return judge if judge.configured else None


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
            print("⚠️  未配置完整飞书参数，本次不推飞书")
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
    profile = (
        load_profile_from_path(args.profile_file)
        if args.profile_file
        else get_profile("generic-qmt")
    )
    return QmtXtDataAdapter(profile=profile, calendar_market=args.calendar_market)


def _optional_date(value: str | None, fallback: date) -> date:
    return date.fromisoformat(value) if value else fallback


def _qmt_health() -> dict:
    """QMT 未开通是预期状态，诊断命令要如实报告而不是抛栈。"""
    try:
        payload = QmtXtDataAdapter(profile=get_profile("generic-qmt")).health_check()
    except QmtAdapterError as exc:
        return {
            "adapter": "QmtXtDataAdapter",
            "ready": False,
            "connected": False,
            "reason": str(exc),
        }
    payload["ready"] = True
    return payload


def _morning_facts(adapter) -> dict:
    """板块与大盘读数，作为 chat 的上下文。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        run_morning(
            Sources(
                adapter=adapter,
                notifiers=[],
                journal=Journal(root=Path(tmp) / "journal"),
                industry_taxonomy=_industry_taxonomy("SW1"),
            ),
            Path(tmp),
            config=MorningConfig(top_sectors=8, candidates_per_sector=2),
        )
        return json.loads((Path(tmp) / "morning_latest.json").read_text(encoding="utf-8"))


def _industry_taxonomy(level: str) -> ThsIndustryTaxonomy:
    return ThsIndustryTaxonomy(level=level)


def _security_master_reference() -> SecurityMasterAdapter:
    return SecurityMasterAdapter()


# ---- dispatch ------------------------------------------------------------


# 命令名 → 处理函数。用表而不是 if/elif 链：链式写法下每加一个命令就多两个分支，
# 而分支之间没有任何共享逻辑——它们只是碰巧写在同一个函数里。拆开之后每条命令
# 可以单独调用和测试，`main` 只剩「取参数、查表、执行」。
def _cmd_daemon(args) -> None:
    summary = run_daily_cycle(
        adapter_factory=lambda: _tickflow(args),
        output_dir=args.output_dir,
        trade_date=_optional_date(getattr(args, "date", None), beijing_today()),
        only=args.only,
        dry_run=args.dry_run,
        writer_factory=lambda: _writer(args),
        debate_factory=lambda: _debate(args),
        notifier_factory=lambda slug: _notifiers(args, args.output_dir, slug),
        top_sectors=args.top_sectors,
        candidates_per_sector=args.per_sector,
        max_constituents=args.max_constituents,
        alert_levels=tuple(args.alert_levels),
        progress=_progress(args),
    )
    _dump_json(_trim(summary))


def _cmd_morning(args) -> None:
    summary = run_morning(
        Sources(
            adapter=_tickflow(args),
            writer=_writer(args),
            debate=_debate(args),
            notifiers=_notifiers(args, args.output_dir, "morning"),
            industry_taxonomy=_industry_taxonomy(args.level),
        ),
        args.output_dir,
        config=MorningConfig(
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
            use_earnings=not args.no_earnings,
            use_broad_index=not args.no_broad_index,
            use_trade_flow=not args.no_trade_flow,
            explain=args.explain,
        ),
        progress=_progress(args),
    )
    _dump_json(_trim(summary))
    if args.explain:
        funnel = summary.get("funnel") or {}
        labels = (
            ("universe", "全市场样本"),
            ("selected_sector_constituents", "主攻板块成分"),
            ("screened_constituents", "通过过滤"),
            ("final_candidates", "最终候选"),
        )
        print("筛选漏斗：" + " → ".join(f"{label} {funnel.get(key, 0)}" for key, label in labels))


def _cmd_monitor(args) -> None:
    summary = run_monitor(
        adapter=_tickflow(args),
        symbols=_resolve_symbols(args, include_candidates=True) if args.symbols else None,
        period=args.period,
        once=args.once,
        max_passes=args.max_passes,
        alert_levels=tuple(args.alert_levels),
        news_source=None if getattr(args, "no_news", False) else _news_source(args.output_dir),
        use_order_book=not args.no_order_book,
        notifiers=_notifiers(args, args.output_dir, "alerts"),
    )
    _dump_json(_trim(summary))


def _cmd_advise(args) -> None:
    summary = run_advise(
        adapter=_tickflow(args),
        symbols=_resolve_symbols(args),
        output_dir=args.output_dir,
        period=args.period,
        use_news=not args.no_news,
        use_order_book=not args.no_order_book,
        use_earnings=not args.no_earnings,
        debate=_debate(args) if args.debate else None,
        adjudicator=_adjudicator(args) if args.adjudicate else None,
        writer=_writer(args),
        notifiers=_notifiers(args, args.output_dir, "advice"),
        security_master_adapter=_security_master_reference(),
        progress=_progress(args),
    )
    _dump_json(_trim(summary))


def _cmd_ask(args) -> None:
    # 盘中才需要现算价位，而且只在真的问到标的时才建适配器——盘前问
    # 「今天板块怎么排」不该因为构造一个数据源失败而答不出来。
    compute = None
    if not args.no_live:

        def compute(symbols: list[str]) -> dict:
            return run_advise(
                adapter=_tickflow(args),
                symbols=symbols,
                period=args.period,
                notifiers=[],
                security_master_adapter=_security_master_reference(),
            )["facts"]

    summary = run_ask(
        question=" ".join(args.question).strip() or None,
        symbols=_resolve_symbols(args) if args.symbols else None,
        output_dir=args.output_dir,
        compute_symbols=compute,
        client=LlmClient(provider=args.llm_provider, model=args.llm_model) if args.llm else None,
        is_trading_day=TradingCalendar().is_trading_day(beijing_today()),
        deep=args.deep,
    )
    if summary["notes"] and summary.get("mode") != "deep":
        # 深挖模式的报告里已经有「数据边界」一节，再打一遍就是同一批话说两次。
        print("\n".join(f"（{note}）" for note in summary["notes"]))


def _cmd_facts(args) -> None:
    compute = None
    if not args.no_live:

        def compute(symbols: list[str]) -> dict:
            return run_advise(
                adapter=_tickflow(args),
                symbols=symbols,
                period=args.period,
                notifiers=[],
                security_master_adapter=_security_master_reference(),
            )["facts"]

    bundle = build_facts_bundle(
        moment=beijing_now(),
        symbols=_resolve_symbols(args) if args.symbols else None,
        output_dir=args.output_dir,
        compute_symbols=compute,
        is_trading_day=TradingCalendar().is_trading_day(beijing_today()),
        focus=_resolve_symbols(args) if args.symbols else None,
    )
    if args.format == "text":
        print(bundle.rendered)
        if bundle.notes:
            print("\n".join(f"（{note}）" for note in bundle.notes))
        return
    _dump_json(bundle.to_dict(), args.output)


_PREOPEN_RESEARCH_REQUIREMENTS = """基于 facts 和 overseas_snapshot 写盘前先手观察清单，不能补造数据。
依次判断：海外风险偏好与 A 股映射；政策/地缘/国家队；国内外财报与公告；昨日情绪与新题材。
输出不超过 700 字：总判断、1-3 条先手观察（催化→映射→竞价确认→失效）、只等确认、最多 3 条风险。
每项证据标注已发生、市场预期或推演。没有竞价/开盘数据时，不写正在走强，也不写直接买卖建议。"""


def _cmd_preopen_research(args) -> None:
    """生成每日交给 Codex 的固定输入，避免手工拼接与遗漏外盘时点。"""
    moment = beijing_now()
    bundle = build_facts_bundle(
        moment=moment,
        output_dir=args.output_dir,
        is_trading_day=TradingCalendar().is_trading_day(moment.date()),
    )
    overseas = None if args.no_global else GlobalMarketAdapter().fetch_snapshot().to_dict()
    payload = {
        "as_of": moment.isoformat(timespec="seconds"),
        "task": "A股盘前先手研报",
        "requirements": _PREOPEN_RESEARCH_REQUIREMENTS,
        "facts": bundle.to_dict(),
        "overseas_snapshot": overseas,
    }
    if args.format == "markdown":
        rendered = _render_preopen_research(payload)
        if args.output:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return
    _dump_json(payload, args.output)


def _render_preopen_research(payload: dict) -> str:
    """只根据包内事实排版，不把条件预案写成方向预测。"""
    facts = payload["facts"]["facts"]
    morning = facts.get("盘前主链路") if isinstance(facts, dict) else None
    lines = ["# A股盘前情景报告", "", f"数据时点：{payload['as_of']}"]
    lines.extend(["", "## 数据边界"])
    lines.extend(f"- {note}" for note in payload["facts"].get("notes", []))
    overseas = payload.get("overseas_snapshot") or {}
    lines.extend(["", "## 海外隔夜"])
    if overseas.get("readings"):
        for item in overseas["readings"]:
            if item.get("last") is None:
                lines.append(f"- {item['name']}：未覆盖（{item.get('note') or '无可用读数'}）")
            else:
                lines.append(
                    f"- {item['name']}：{item['last']}，较前收 {item.get('change_pct')}%，"
                    f"来源 {item.get('source', '未知')}，时点 {item.get('as_of') or '未知'}"
                )
    else:
        lines.append("- 未请求海外快照；不对隔夜风险偏好作数值判断。")
    lines.extend(["", "## A股盘前事实"])
    if not isinstance(morning, dict):
        lines.append("- 尚无当日盘前主链路落盘结果，不能生成主线排序。")
    else:
        market = morning.get("market") or {}
        lines.append(
            f"- 系统市场读数：{market.get('direction', '未覆盖')}；原因：{'；'.join(market.get('reasons') or ['未覆盖'])}"
        )
        for sector in (morning.get("sectors") or [])[:3]:
            lines.append(
                f"- 关注方向：{sector.get('name')}（{sector.get('direction', '未覆盖')}）。依据：{'；'.join(sector.get('reasons') or ['未覆盖'])}"
            )
        for warning in market.get("warnings") or []:
            lines.append(f"- 风险：{warning}")
    lines.extend(
        [
            "",
            "## 开盘预案",
            "- 仅当催化、A 股映射与竞价/开盘承接同时出现时，才将方向列入先手观察。",
            "- 高开而没有成交扩散、龙头承接或板块跟随时，按兑现风险处理，不追高。",
            "- 地缘、政策与国家队信息只以原始公告或多源快讯为证；缺证据时保留“未覆盖”。",
        ]
    )
    return "\n".join(lines)


def _cmd_chat(args) -> None:
    adapter = _tickflow(args)

    def compute_symbols(symbols: list[str]) -> dict:
        return run_advise(
            adapter=adapter,
            symbols=symbols,
            period=args.period,
            notifiers=[],
            security_master_adapter=_security_master_reference(),
        )["facts"]

    run_chat(
        compute_symbols=compute_symbols,
        compute_sectors=lambda: _morning_facts(adapter),
        symbols=_resolve_symbols(args),
        responder=ChatResponder(LlmClient(provider=args.llm_provider, model=args.llm_model))
        if args.llm
        else None,
    )


def _cmd_watchlist(args) -> None:
    _dump_json(_watchlist_command(args))


def _cmd_review(args) -> None:
    summary = run_review(
        adapter=_tickflow(args),
        horizon=args.horizon,
        notifiers=_notifiers(args, args.output_dir, "review"),
    )
    _dump_json(_trim(summary))


def _cmd_journal(args) -> None:
    journal = Journal()
    if args.symbol:
        _dump_json(journal.history_for(args.symbol, limit=args.limit))
    else:
        _dump_json(summarize(journal.outcomes()))


def _cmd_codex_adjudication_packet(args) -> None:
    facts = json.loads(Path(args.facts).read_text(encoding="utf-8"))
    _dump_json(build_adjudication_packet(facts), args.output)


def _cmd_codex_adjudication_validate(args) -> None:
    packet = json.loads(Path(args.packet).read_text(encoding="utf-8"))
    decision = json.loads(Path(args.decision).read_text(encoding="utf-8"))
    result = validate_external_decisions(
        packet,
        decision,
        now=beijing_now().replace(tzinfo=None),
        max_age_minutes=args.max_age_minutes,
    )
    _dump_json(result.to_dict(), args.output)


def _cmd_post_close(args) -> None:
    summary = run_post_close(
        adapter=_tickflow(args),
        symbols=_resolve_symbols(args),
        output_dir=args.output_dir,
        days=args.days,
        strategy_version=args.strategy_version,
        position_factor=args.position_factor,
        use_limit_pool=not args.no_limit_pool,
        top_n=args.top,
        debate=_debate(args),
        security_master_adapter=_security_master_reference(),
    )
    _dump_json(_trim(summary))


def _cmd_pre_open(args) -> None:
    summary = run_pre_open(
        payload_path=args.payload,
        output_dir=args.output_dir,
        adapter=_tickflow(args) if args.refresh else None,
    )
    _dump_json(_trim(summary))


def _cmd_wyckoff_scan(args) -> None:
    adapter = _source_adapter(args)
    end = _optional_date(args.end, beijing_today())
    start = _optional_date(args.start, end.replace(year=end.year - 1))
    bars = adapter.get_historical_bars(args.symbols, start, end)
    signals = WyckoffAnalyzer().analyze_many(bars)
    _dump_json([signal.model_dump(mode="json") for signal in signals], args.output)


def _cmd_strategy_screen(args) -> None:
    """Run the deterministic full-universe strategy scanner for AI consumption."""
    taxonomy = ThsIndustryTaxonomy(level="SW1")
    profiles = taxonomy.snapshot.profiles
    symbols = sorted(profiles)
    adapter = _tickflow(args)
    end = beijing_today()
    start = end - timedelta(days=max(90, args.days))
    bars = fetch_daily_bars(adapter, symbols, start, end)
    names = {symbol: profile.name for symbol, profile in profiles.items()}
    sectors = {symbol: profile.industry("SW1") for symbol, profile in profiles.items()}
    common = {
        "price_min": args.price_min,
        "price_max": args.price_max,
        "min_amount_yi": args.min_amount_yi,
        "allow_st": not args.exclude_st,
        "main_board_only": not args.all_a,
        "limit": args.limit,
        "min_history_bars": args.min_history_bars,
        "min_atr_pct": args.min_atr_pct,
        "max_atr_pct": args.max_atr_pct,
        "min_range_position": args.min_range_position,
        "max_range_position": args.max_range_position,
        "min_pullback_pct": args.min_pullback_pct,
        "max_pullback_pct": args.max_pullback_pct,
        "min_close_vs_ma20_pct": args.min_close_vs_ma20_pct,
        "max_volume_contraction": args.max_volume_contraction,
        "min_volume_ratio": args.min_volume_ratio,
        "max_volume_ratio": args.max_volume_ratio,
        "min_close_position": args.min_close_position,
        "max_close_position": args.max_close_position,
    }
    if args.profile == "all":
        results = {
            profile: screen_overnight(
                bars,
                names=names,
                sectors=sectors,
                # Calculate the full accepted set before applying the display limit;
                # otherwise cross-profile overlap would depend on ranking cutoffs.
                config=StrategyScreenConfig(profile=profile, **{**common, "limit": 0}),
            )
            for profile in sorted(PROFILES)
        }
        candidate_sets = {name: {item.symbol for item in result.candidates} for name, result in results.items()}
        memberships: dict[str, list[str]] = {}
        for profile, symbols_for_profile in candidate_sets.items():
            for symbol in symbols_for_profile:
                memberships.setdefault(symbol, []).append(profile)
        overlap = {
            symbol: sorted(profiles_for_symbol)
            for symbol, profiles_for_symbol in sorted(memberships.items())
            if len(profiles_for_symbol) > 1
        }
        profile_payloads = {}
        for name, result in results.items():
            item = result.to_dict()
            total = len(item["candidates"])
            item["summary"]["matched_total"] = total
            item["config"]["limit"] = args.limit
            item["candidates"] = item["candidates"][: args.limit]
            item["summary"]["candidates"] = len(item["candidates"])
            profile_payloads[name] = item
        payload = {
            "schema": "ashare-strategy-screen-batch-v2",
            "as_of": str(end),
            "config": common | {"profile": "all"},
            "profiles": profile_payloads,
            "diversified_shortlist": diversified_shortlist(results, args.limit),
            "matched_by_multiple_profiles": overlap,
            "notes": [
                "综合短名单按策略轮询取各自前列，不把不同策略的 setup_rank 当成同一尺度",
                "策略位置区间可能互斥；多策略重复命中不代表更高胜率或收益率",
            ],
        }
    else:
        result = screen_overnight(
            bars,
            names=names,
            sectors=sectors,
            config=StrategyScreenConfig(profile=args.profile, **common),
        )
        payload = result.to_dict()
        payload["as_of"] = str(end)
        payload["data_notes"] = [
            f"日线回溯区间约 {args.days} 个自然日，信号日为最近一根已取得日线",
            "这是候选观察事实，不是订单、收益保证或模型预测",
            *taxonomy.notes,
        ]
    if args.profile == "all":
        payload["data_notes"] = [
            f"日线回溯区间约 {args.days} 个自然日，信号日为最近一根已取得日线",
            "这是候选观察事实，不是订单、收益保证或模型预测",
            *taxonomy.notes,
        ]
    output = args.output or str(Path(args.output_dir) / "strategy_screen_latest.json")
    _dump_json(payload, output)


def _cmd_tick_health(args) -> None:
    _dump_json(_tickflow(args).health_check())


def _cmd_intraday_check(args) -> None:
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


def _cmd_qmt_health(args) -> None:
    _dump_json(_qmt_health())


def _cmd_state(args) -> None:
    _dump_json(_state_report())


COMMANDS = {
    "daemon": _cmd_daemon,
    "morning": _cmd_morning,
    "monitor": _cmd_monitor,
    "advise": _cmd_advise,
    "ask": _cmd_ask,
    "facts": _cmd_facts,
    "preopen-research": _cmd_preopen_research,
    "chat": _cmd_chat,
    "watchlist": _cmd_watchlist,
    "review": _cmd_review,
    "journal": _cmd_journal,
    "codex-adjudication-packet": _cmd_codex_adjudication_packet,
    "codex-adjudication-validate": _cmd_codex_adjudication_validate,
    "post-close-brief": _cmd_post_close,
    "pre-open-brief": _cmd_pre_open,
    "wyckoff-scan": _cmd_wyckoff_scan,
    "strategy-screen": _cmd_strategy_screen,
    "tick-health": _cmd_tick_health,
    "intraday-check": _cmd_intraday_check,
    "qmt-health": _cmd_qmt_health,
    "state": _cmd_state,
}


def main() -> None:
    # 先于解析参数：`.env.tickflow` 里的 key 决定了 TickFlow 走付费档还是免费档，
    # 而适配器在命令分支里就构造了。已经 export 的变量不受影响。
    load_env_files()
    args = build_parser().parse_args()
    # `required=True` 的 subparsers 保证 command 一定在表里；真取不到就抛，
    # 那是接线漏了一条命令，不该悄悄什么都不做。
    COMMANDS[args.command](args)


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

    today = beijing_today()
    book, pool = Watchlist(), CandidatePool(today=today)
    alerts = AlertState.load(today)
    daemon = DaemonState.load(today)
    calendar = TradingCalendar()
    journal = Journal()
    outcomes = journal.outcomes()
    return {
        "state_home": str(state_home()),
        "env_files_loaded": loaded_files(),
        "tickflow_mode": "full" if os.environ.get("TICKFLOW_API_KEY") else "free",
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
        "checked_at": beijing_now().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    main()
