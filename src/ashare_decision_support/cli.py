"""命令行入口：全天调度、单次分析、持仓维护和诊断。

自动调度与手动命令复用 jobs 任务。个股命令优先使用显式代码，省略时读取持仓。
参数在各 _parser_* 中维护，处理函数由 COMMANDS 分派。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from ashare_decision_support.adapters.akshare_calendar import AkshareCalendarAdapter
from ashare_decision_support.adapters.base import SUPPORTED_PERIODS, fetch_daily_bars
from ashare_decision_support.adapters.freshness import check_intraday_freshness
from ashare_decision_support.adapters.global_market import GlobalMarketAdapter
from ashare_decision_support.adapters.overseas_history import OverseasHistoryAdapter
from ashare_decision_support.adapters.security_master import SecurityMasterAdapter
from ashare_decision_support.adapters.sina_intraday import SinaIntradayAdapter
from ashare_decision_support.adapters.ths_sector_index import (
    BACKFILL_START_YEAR,
    ThsSectorIndexAdapter,
    load_all_series,
)
from ashare_decision_support.adapters.tickflow_adapter import TickFlowAdapter
from ashare_decision_support.adapters.tickflow_intraday import build_intraday_adapter
from ashare_decision_support.advice import (
    LevelAdjudicator,
    build_adjudication_packet,
    validate_external_decisions,
)
from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.artifacts import (
    LEADERS_LATEST_NAME,
    MORNING_LATEST_NAME,
    POST_CLOSE_LATEST_NAME,
    STRATEGY_SCREEN_LATEST_NAME,
)
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
from ashare_decision_support.jobs.daily_review import run_daily_review
from ashare_decision_support.journal import Journal, summarize
from ashare_decision_support.journal.records import DEFAULT_STRATEGY_VERSION
from ashare_decision_support.monitor import (
    DEFAULT_LEVELS,
    LEVEL_ACTION,
    LEVEL_INFO,
    LEVEL_WARN,
    SECTOR_EVERY,
)
from ashare_decision_support.monitor.sector_triggers import fired_sectors_today
from ashare_decision_support.notes import compact_notes
from ashare_decision_support.notify import ConsoleNotifier, FileNotifier
from ashare_decision_support.notify.feishu import FeishuNotifier
from ashare_decision_support.research import (
    build_monthly_context,
    load_daily_sector_analysis,
    refresh_sector_leaders,
    render_daily_sector_analysis,
)
from ashare_decision_support.research.preopen import (
    build_preopen_research,
    render_preopen_research,
)
from ashare_decision_support.research.sector_confirmation import build_sector_confirmation
from ashare_decision_support.research.sector_forecast import build_sector_forecast
from ashare_decision_support.schedule import TradingCalendar
from ashare_decision_support.schedule.timezone import beijing_naive_now, beijing_now, beijing_today
from ashare_decision_support.sectors import ThsIndustryTaxonomy, static_taxonomies
from ashare_decision_support.sectors.backtest import backtest_price_baselines
from ashare_decision_support.sectors.confirmation_ledger import SectorConfirmationLedger
from ashare_decision_support.state import Positions, state_home
from ashare_decision_support.state.store import write_text
from ashare_decision_support.strategy.overnight import (
    PROFILES,
    StrategyScreenConfig,
    latest_signal_date,
    screen_all_profiles,
    screen_notes,
    screen_overnight,
)
from ashare_decision_support.strategy.portfolio import MIN_CLUSTER_SYMBOLS, read_portfolio
from ashare_decision_support.symbols import normalize_symbol
from ashare_decision_support.writer import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    LlmBriefWriter,
    TemplateWriter,
)
from ashare_decision_support.writer.llm import LlmClient

ALERT_LEVELS = [LEVEL_ACTION, LEVEL_WARN, LEVEL_INFO]


# 四组会被多条命令重复添加的参数。提到模块层而不是留在 `build_parser` 里做闭包，
# 是为了让每条命令的注册函数能独立存在——否则拆出去的函数还得把它们当参数传。
# `--llm-provider` / `--llm-model` 只在 `_add_llm_args` 定义一次：写稿组复用它，
# 而不是再写一遍同名参数。
def _add_tickflow_args(sub) -> None:
    sub.add_argument("--api-key", help="TickFlow API key; omit for free mode")
    sub.add_argument("--base-url", help="Optional TickFlow base URL override")


def _add_llm_args(sub) -> None:
    sub.add_argument("--llm-provider", default=DEFAULT_PROVIDER, choices=sorted(PROVIDERS))
    sub.add_argument("--llm-model", help="Override the provider's default model")


def _add_writer_args(sub) -> None:
    sub.add_argument(
        "--writer",
        default="template",
        choices=["auto", "template", "llm"],
        help="默认 template 只走确定性；auto=有 key 用 LLM 改写，无 key 回退模板；llm 强制用 LLM",
    )
    _add_llm_args(sub)


def _add_notify_args(sub) -> None:
    sub.add_argument(
        "--feishu",
        action="store_true",
        help="同时推送到飞书（需 webhook，或应用凭证 + receive_id）",
    )
    sub.add_argument("--quiet", action="store_true", help="不打印简报正文到控制台")


# 每条命令一个注册函数
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
    daemon.add_argument(
        "--no-sectors", action="store_true", help="盘中监控里关闭板块截面与板块告警"
    )
    daemon.add_argument(
        "--sector-every",
        type=int,
        default=SECTOR_EVERY,
        help=f"每多少轮个股监控做一次板块截面，默认 {SECTOR_EVERY}",
    )
    _add_writer_args(daemon)
    _add_notify_args(daemon)
    _add_tickflow_args(daemon)


def _parser_morning(subparsers) -> None:
    morning = subparsers.add_parser(
        "morning", help="[需求1] 盘前趋势分析：舆情 → 大盘趋势 → 板块排序 → 板块内选股 → 当日候选"
    )
    morning.add_argument("--output-dir", default="briefs")
    morning.add_argument("--strategy-version", default=DEFAULT_STRATEGY_VERSION)
    morning.add_argument("--top-sectors", type=int, default=5)
    morning.add_argument("--top-concepts", type=int, default=3)
    morning.add_argument("--per-sector", type=int, default=3)
    morning.add_argument(
        "--position-factor", type=float, help="手工仓位系数 (0-1)，省略用系统建议值"
    )
    morning.add_argument("--no-news", action="store_true", help="跳过舆情与龙虎榜")
    morning.add_argument(
        "--force-refresh-daily",
        action="store_true",
        help="忽略已完成交易日的日线复用，重新向行情源取板块动量数据",
    )
    morning.add_argument(
        "--no-llm-news", action="store_true", help="舆情归因只用关键词词典，不调大模型"
    )
    morning.add_argument("--no-limit-pool", action="store_true", help="跳过涨停池情绪读数")
    morning.add_argument("--no-concepts", action="store_true", help="只排行业，跳过概念板块")
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
        help="跳过候选的逐笔资金旁证（大单净额、尾盘净额），可省约 2 分钟",
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


def _parser_sector_analysis(subparsers) -> None:
    sector = subparsers.add_parser(
        "sector-analysis",
        help="只读最近一次每日板块分析，不重新取数",
    )
    sector.add_argument("--output-dir", default="briefs")
    sector.add_argument("--format", choices=["markdown", "text", "json"], default="markdown")
    sector.add_argument("--output", help="写入文件；不传则同时打印到控制台")

    backtest = subparsers.add_parser(
        "sector-backtest",
        help="板块预测校准：历史价格基线的滚动样本外评价",
    )
    backtest.add_argument("--top", type=int, default=5, help="每天选择排名前几的板块")
    backtest.add_argument("--output", help="写入 JSON；省略则打印")

    forecast = subparsers.add_parser(
        "sector-forecast",
        help="板块预测：主题簇情景、盘中确认条件与历史校准状态",
    )
    forecast.add_argument("--output-dir", default="briefs")
    forecast.add_argument("--output", help="写入 JSON；省略则打印")

    confirmation = subparsers.add_parser(
        "sector-confirmation",
        help="用当天盘中截面确认或否决最新板块预测",
    )
    confirmation.add_argument("--output-dir", default="briefs")
    confirmation.add_argument("--output", help="写入 JSON；省略则打印")


def _parser_monitor(subparsers) -> None:
    monitor = subparsers.add_parser(
        "monitor", help="[需求3] 盘中监控：持仓卖点 + 板块状态，按跃迁告警"
    )
    monitor.add_argument("--symbols", nargs="*", help="省略则盯持仓；板块监控与名单无关")
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
    monitor.add_argument(
        "--no-sectors",
        action="store_true",
        help="关闭盘中板块监控（默认开启：每 5 轮做一次全市场板块截面并按跃迁告警）",
    )
    monitor.add_argument(
        "--sector-every",
        type=int,
        default=SECTOR_EVERY,
        help=f"每多少轮个股监控做一次板块截面，默认 {SECTOR_EVERY}（约 10 分钟）",
    )
    _add_notify_args(monitor)
    _add_tickflow_args(monitor)


def _parser_advise(subparsers) -> None:
    advise = subparsers.add_parser("advise", help="[需求2/3] 个股股价分析与买卖点推荐")
    # 位置参数与 `--symbols` 等价。README / USAGE 从第一版起写的就是
    # `ashare-ds advise 600519.SH`，而解析器此前只认 `--symbols`——照文档敲的
    # 那条命令直接以 `unrecognized arguments` 退出。文档是这条命令的对外契约，
    # 所以补的是解析器，不是文档。
    advise.add_argument(
        "symbols_positional",
        nargs="*",
        metavar="SYMBOL",
        help="要分析的标的；与 --symbols 等价，两者都省略则读持仓",
    )
    advise.add_argument("--symbols", nargs="*", help="省略则读持仓")
    advise.add_argument("--output-dir", help="省略则只打印，不落盘")
    advise.add_argument("--strategy-version", default=DEFAULT_STRATEGY_VERSION)
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
    ask.add_argument("--symbols", nargs="*", help="要看的标的；省略则只带持仓与已落盘结果")
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
    facts.add_argument("--symbols", nargs="*", help="要看的标的；省略则只带持仓与已落盘结果")
    facts.add_argument("--output-dir", default="briefs", help="读盘前/盘后落盘结果的目录")
    facts.add_argument("--period", default="5", choices=list(SUPPORTED_PERIODS))
    facts.add_argument("--no-live", action="store_true", help="盘中也不现算价位，只读已落盘的")
    facts.add_argument(
        "--phase",
        choices=["pre_open", "intraday", "post_close", "off_hours"],
        help="强制按指定时段装配，便于自动化与复现；省略则按北京时间判断",
    )
    facts.add_argument(
        "--format",
        default="json",
        choices=["json", "text"],
        help="json=结构化事实包；text=直接输出人类可读摘要",
    )
    facts.add_argument(
        "--full", action="store_true", help="text 模式输出完整事实；默认输出精简摘要"
    )
    facts.add_argument(
        "--sections",
        nargs="+",
        help="只导出这几块事实（如 盘前主链路 持仓）；省略即全量，省掉的块会写进 notes",
    )
    facts.add_argument("--output", help="把 JSON bundle 写到文件；text 模式忽略")
    _add_tickflow_args(facts)


def _parser_chat(subparsers) -> None:
    chat = subparsers.add_parser(
        "chat", help="[需求4] 持续对话：对趋势、股价、监控与买卖点多轮追问"
    )
    chat.add_argument("--symbols", nargs="*", help="省略则读持仓；对话里用 /add 随时加")
    chat.add_argument("--period", default="5", choices=list(SUPPORTED_PERIODS))
    chat.add_argument("--llm", action="store_true", help="显式开启模型对话；默认只输出事实包")
    _add_llm_args(chat)
    _add_tickflow_args(chat)


def _parser_preopen_research(subparsers) -> None:
    research = subparsers.add_parser(
        "preopen-research",
        help="[Codex] 导出盘前研报包：已落盘 facts + 海外市场快照 + 固定研判要求",
    )
    research.add_argument(
        "--output-dir", default="briefs", help=f"读取 {MORNING_LATEST_NAME} 的目录"
    )
    research.add_argument("--output", help="写入 JSON 研报包；省略则打印到控制台")
    research.add_argument("--format", choices=["json", "markdown"], default="json")
    research.add_argument(
        "--no-global", action="store_true", help="跳过海外市场快照，只导出已落盘 facts"
    )


def _parser_position(subparsers) -> None:
    # 持仓是系统唯一还替你记着的名单。系统不下单、不接管账户，所以成本价、股数、
    # 计划止损与目标全部手工维护。
    position = subparsers.add_parser("position", help="持仓：盘中卖点监控的唯一名单")
    position_sub = position.add_subparsers(dest="action", required=True)
    position_sub.add_parser("list", help="列出当前持仓")

    opened = position_sub.add_parser("open", help="登记或修改一笔持仓")
    opened.add_argument("symbol")
    opened.add_argument("--cost", type=float, required=True, help="成本价")
    opened.add_argument("--shares", type=int, help="股数")
    opened.add_argument("--opened-on", help="建仓日 YYYY-MM-DD，默认今天")
    opened.add_argument("--stop", type=float, help="你自己定的止损，优先于系统值")
    opened.add_argument("--target", type=float, help="你自己定的目标")
    opened.add_argument("--note", help="备注")

    closed = position_sub.add_parser("close", help="平仓：从持仓表里删掉")
    closed.add_argument("symbols", nargs="+")
    position_sub.add_parser("clear", help="清空持仓表")


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def _parser_review(subparsers) -> None:
    review = subparsers.add_parser("review", help="兑现核对：T+1..T+5 对照实际走势，并出统计")
    review.add_argument(
        "--horizon", type=_positive_int, default=5, help="核对窗口（正整数，交易日）"
    )
    review.add_argument(
        "--daily",
        nargs="?",
        const="today",
        metavar="YYYY-MM-DD",
        help="离线还原指定日的计划、盘中事件与次日复查项；省略日期表示今天",
    )
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
    post_close = subparsers.add_parser("post-close-brief", help="盘后：给定标的打分与次日执行卡")
    post_close.add_argument("--symbols", nargs="*", help="省略则读持仓")
    post_close.add_argument("--output-dir", default="briefs")
    post_close.add_argument("--days", type=int, default=180)
    post_close.add_argument("--top", type=int)
    post_close.add_argument("--strategy-version", default=DEFAULT_STRATEGY_VERSION)
    post_close.add_argument(
        "--position-factor", type=float, help="手工仓位系数 (0-1)，省略用情绪周期上限"
    )
    post_close.add_argument("--no-limit-pool", action="store_true", help="跳过涨停池情绪读数")
    post_close.add_argument("--no-earnings", action="store_true", help="跳过业绩预告与财报排雷")
    post_close.add_argument("--no-debate", action="store_true", help="关闭多空辩论层")
    _add_tickflow_args(post_close)


def _parser_pre_open(subparsers) -> None:
    pre_open = subparsers.add_parser("pre-open-brief", help="次日盘前：把昨日执行卡复述一遍")
    pre_open.add_argument(
        "--payload",
        help=f"盘后落盘的 {POST_CLOSE_LATEST_NAME}，省略则读 --output-dir 下那份（默认 briefs/）",
    )
    pre_open.add_argument("--output-dir", help="执行卡所在目录，也是本次提示的落盘目录")
    pre_open.add_argument("--refresh", action="store_true", help="重新取价而非复用盘后快照")
    _add_tickflow_args(pre_open)


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
    screen.add_argument("--exclude-st", action="store_true", help="显式排除名称含 ST/退的标的")
    screen.add_argument(
        "--all-a", action="store_true", help="不限定沪深主板，纳入可识别的其他 A 股"
    )
    screen.add_argument("--limit", type=int, default=30)
    screen.add_argument("--days", type=int, default=240, help="日线回溯天数")
    screen.add_argument("--min-history-bars", type=int, default=65, help="最少有效日线根数")
    screen.add_argument("--output-dir", default="briefs")
    screen.add_argument(
        "--output", help=f"另写一份 JSON；省略则写 output-dir/{STRATEGY_SCREEN_LATEST_NAME}"
    )
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


def _parser_external_research(subparsers) -> None:
    leaders = subparsers.add_parser(
        "sector-leaders", help="每个行业/概念的量价龙头候选事实（供外部 AI 研究）"
    )
    leaders.add_argument("--output-dir", default="briefs")
    leaders.add_argument("--refresh", action="store_true", help="重新取全市场日线；默认只读缓存")
    leaders.add_argument("--level", choices=["industry", "concept", "all"], default="all")
    leaders.add_argument(
        "--top",
        "--top-per-sector",
        dest="top",
        type=int,
        default=3,
        help="每个板块保留几只",
    )
    leaders.add_argument(
        "--days",
        "--lookback-days",
        dest="days",
        type=int,
        default=180,
        help="日线回溯自然日",
    )
    leaders.add_argument("--min-history-bars", type=int, default=65)
    leaders.add_argument("--output", help="另写一份 JSON")
    _add_tickflow_args(leaders)

    monthly = subparsers.add_parser(
        "monthly-context", help="给 Codex/Claude 的月度展望事实与外部研究任务包"
    )
    monthly.add_argument("--output-dir", default="briefs")
    monthly.add_argument("--horizon-days", type=int, default=31)
    monthly.add_argument("--symbols", nargs="*")
    monthly.add_argument("--output")


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

    subparsers.add_parser("state", help="诊断：状态目录、持仓、任务完成情况")

    calendar_refresh = subparsers.add_parser(
        "calendar-refresh", help="从行情源刷新本地节假日表（holidays.json）"
    )
    calendar_refresh.add_argument("--year", type=int, default=None, help="默认当年")
    calendar_refresh.add_argument("--source", default="akshare", choices=["akshare", "tickflow"])
    _add_tickflow_args(calendar_refresh)

    history = subparsers.add_parser(
        "history-refresh",
        help="刷新历史缓存：同花顺板块指数 + 海外指数，用于外盘映射基准率",
    )
    history.add_argument(
        "--only",
        choices=["all", "sector-index", "overseas"],
        default="all",
        help="默认两类历史都刷新",
    )
    history.add_argument("--since-year", type=int, default=None, help="板块指数回填起始年份")
    history.add_argument("--until-year", type=int, default=None, help="板块指数回填截止年份")
    history.add_argument("--keys", nargs="*", help="只刷新这些海外品种 key；省略即全部")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A-share ultra-short analysis CLI: trend, price, monitor, chat"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 自动运行
    _parser_daemon(subparsers)
    # 主链路各段（可单独跑）
    _parser_morning(subparsers)
    _parser_sector_analysis(subparsers)
    _parser_monitor(subparsers)
    _parser_advise(subparsers)
    _parser_ask(subparsers)
    _parser_facts(subparsers)
    _parser_preopen_research(subparsers)
    _parser_chat(subparsers)
    # 持仓
    _parser_position(subparsers)
    # 台账与复盘
    _parser_review(subparsers)
    _parser_journal(subparsers)
    _parser_codex_adjudication(subparsers)
    # 日常闭环
    _parser_post_close(subparsers)
    _parser_pre_open(subparsers)
    _parser_strategy_screen(subparsers)
    _parser_external_research(subparsers)
    # 诊断
    _parser_diagnostics(subparsers)
    return parser


def _dump_json(
    payload: object, output_path: str | None = None, *, echo_when_written: bool = False
) -> None:
    """写 JSON 产物；指定文件后不再把同一份大对象重复输出到终端。"""
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    _write_text(rendered, output_path)
    if output_path and echo_when_written:
        print(rendered)


def _write_text(rendered: str, output_path: str | None = None) -> None:
    """写文本产物；未指定路径时才输出到终端。"""
    if output_path:
        write_text(Path(output_path), rendered + "\n")
        return
    print(rendered)


def _trim(summary: dict, drop: set[str] = frozenset({"content", "facts"})) -> dict:
    return {key: value for key, value in summary.items() if key not in drop}


def _morning_cli_summary(summary: dict) -> dict:
    """Return the operational receipt; the full auditable payload is already on disk."""
    keys = (
        "title",
        "trade_date",
        "market_direction",
        "market_score",
        "limit_up",
        "blowups",
        "blowup_rate",
        "max_streak",
        "sectors_ranked",
        "sectors_selected",
        "industry_sectors_selected",
        "concept_sectors_selected",
        "candidates",
        "candidate_symbols",
        "execution_ready",
        "funnel",
        "diagnostic",
        "diagnostic_mode",
        "suggested_position_factor",
        "position_factor",
        "rotation_regime",
        "rotation_switch",
        "rotation_adjacent",
        "turnover_total_change",
        "journal_records",
        "writer",
        "used_fallback",
        "delivered",
        "payload_path",
        "brief_path",
    )
    compact = {key: summary.get(key) for key in keys if key in summary}
    notes = summary.get("data_notes") or []
    compact["data_notes"] = compact_notes(notes, 8)
    compact["data_notes_omitted"] = max(0, len(notes) - 8)
    compact["full_payload"] = summary.get("payload_path")
    return compact


def _tickflow(args) -> TickFlowAdapter:
    # The free TickFlow SDK prints an informational banner to stdout during
    # construction. CLI commands often emit machine-readable JSON, so third-party
    # chatter must not share that stream. Capability notes still surface through
    # our own data_notes/health payloads.
    with contextlib.redirect_stdout(io.StringIO()):
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


def _explicit_symbols(args) -> list[str]:
    """合并位置参数与 --symbols，归一化代码并按输入顺序去重。"""
    raw = [
        *(getattr(args, "symbols_positional", None) or []),
        *(getattr(args, "symbols", None) or []),
    ]
    return list(dict.fromkeys(normalize_symbol(item) for item in raw if normalize_symbol(item)))


def _resolve_symbols(args) -> list[str]:
    """使用显式代码；未指定时读取持仓。"""
    return _explicit_symbols(args) or Positions().symbols()


def _optional_date(value: str | None, fallback: date) -> date:
    return date.fromisoformat(value) if value else fallback


def _morning_facts(adapter) -> dict:
    """板块与大盘读数，作为 chat 的上下文。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        industry_taxonomy, concept_taxonomy = static_taxonomies()
        run_morning(
            Sources(
                adapter=adapter,
                notifiers=[],
                journal=Journal(root=Path(tmp) / "journal"),
                industry_taxonomy=industry_taxonomy,
                concept_taxonomy=concept_taxonomy,
            ),
            Path(tmp),
            config=MorningConfig(top_sectors=8, candidates_per_sector=2),
        )
        return json.loads((Path(tmp) / MORNING_LATEST_NAME).read_text(encoding="utf-8"))


def _advise_facts(adapter, symbols: list[str], period: str) -> dict:
    """ask/facts/chat 共用的现算价位入口，不发送通知、不写台账。"""
    return run_advise(
        adapter=adapter,
        symbols=symbols,
        period=period,
        notifiers=[],
        security_master_adapter=SecurityMasterAdapter(),
    )["facts"]


def _live_facts(args) -> Callable[[list[str]], dict] | None:
    """`ask` / `facts` 用来现算标的价位的回调，`--no-live` 时是 None。

    闭包而不是直接算：只有真的问到标的时才构造适配器。盘前问「今天板块怎么排」
    不该因为建一个数据源失败而答不出来。
    """
    if args.no_live:
        return None
    return lambda symbols: _advise_facts(_tickflow(args), symbols, args.period)


# dispatch


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
        use_llm_news=not args.no_llm_news,
        sectors=not getattr(args, "no_sectors", False),
        sector_every=getattr(args, "sector_every", SECTOR_EVERY),
        progress=_progress(args),
    )
    _dump_json(_trim(summary))


def _cmd_morning(args) -> None:
    industry_taxonomy, concept_taxonomy = static_taxonomies()
    summary = run_morning(
        Sources(
            adapter=_tickflow(args),
            writer=_writer(args),
            debate=_debate(args),
            notifiers=_notifiers(args, args.output_dir, "morning"),
            global_market_adapter=GlobalMarketAdapter(),
            industry_taxonomy=industry_taxonomy,
            concept_taxonomy=concept_taxonomy,
        ),
        args.output_dir,
        config=MorningConfig(
            strategy_version=args.strategy_version,
            top_sectors=args.top_sectors,
            top_concepts=args.top_concepts,
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
            use_concepts=not args.no_concepts,
            force_refresh_daily=args.force_refresh_daily,
        ),
        progress=_progress(args),
    )
    _dump_json(_morning_cli_summary(summary))
    if args.explain:
        funnel = summary.get("funnel") or {}
        labels = (
            ("universe", "全市场样本"),
            ("selected_sector_constituents", "主攻板块成分"),
            ("screened_constituents", "通过过滤"),
            ("final_candidates", "最终候选"),
        )
        print("筛选漏斗：" + " → ".join(f"{label} {funnel.get(key, 0)}" for key, label in labels))
        # 对照样本单独一行，不串进上面那条箭头链：它不是漏斗的下一档，是**紧邻
        # 落选**的平行一组（只记台账不推送）。串进「→」里会读成「最终候选又被
        # 筛掉一批」，方向正好是反的。`review` 的 edge 就是拿它跟入选组比出来的，
        # 所以它是这条漏斗里唯一一个「越多越说明对照组够用」的数。
        print(
            f"对照样本：{funnel.get('control_sample', 0)}"
            "（紧邻落选，只记不推；`review` 用它算 edge）"
        )


def _cmd_sector_analysis(args) -> None:
    try:
        payload = load_daily_sector_analysis(args.output_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.format == "json":
        _dump_json(payload, args.output)
        return
    rendered = render_daily_sector_analysis(payload)
    _write_text(rendered, args.output)


def _cmd_sector_backtest(args) -> None:
    _dump_json(backtest_price_baselines(load_all_series(), top=args.top), args.output)


def _cmd_sector_forecast(args) -> None:
    _dump_json(build_sector_forecast(args.output_dir), args.output)


def _cmd_sector_confirmation(args) -> None:
    payload = build_sector_confirmation(args.output_dir)
    if payload.get("as_of"):
        payload["ledger"] = SectorConfirmationLedger().record(payload)
    else:
        payload["ledger"] = SectorConfirmationLedger().summary()
    _dump_json(payload, args.output)


def _cmd_monitor(args) -> None:
    summary = run_monitor(
        adapter=_tickflow(args),
        symbols=_explicit_symbols(args) or None,
        period=args.period,
        once=args.once,
        max_passes=args.max_passes,
        alert_levels=tuple(args.alert_levels),
        use_news=not getattr(args, "no_news", False),
        news_cache_dir=args.output_dir,
        output_dir=args.output_dir,
        use_order_book=not args.no_order_book,
        sectors=not getattr(args, "no_sectors", False),
        sector_every=getattr(args, "sector_every", SECTOR_EVERY),
        notifiers=_notifiers(args, args.output_dir, "alerts"),
    )
    _dump_json(_trim(summary))


def _cmd_advise(args) -> None:
    symbols = _resolve_symbols(args)
    if not symbols:
        raise SystemExit(
            "advise 需要标的：`ashare-ds advise 600519.SH`，"
            "或先用 `ashare-ds position open <代码> --cost <成本价>` 登记持仓"
        )
    summary = run_advise(
        adapter=_tickflow(args),
        symbols=symbols,
        output_dir=args.output_dir,
        period=args.period,
        strategy_version=args.strategy_version,
        use_news=not args.no_news,
        use_order_book=not args.no_order_book,
        use_earnings=not args.no_earnings,
        debate=_debate(args) if args.debate else None,
        adjudicator=_adjudicator(args) if args.adjudicate else None,
        writer=_writer(args),
        notifiers=_notifiers(args, args.output_dir, "advice"),
        security_master_adapter=SecurityMasterAdapter(),
        # 只有 `advise` 这条命令落台账。`ask` / `facts` / `chat` 也调 `run_advise`，
        # 但它们是**查**，不是发建议——把一次随口问的标的记成「系统当天的建议」，
        # 事后统计就分不清哪些是系统主动给的了。
        journal=Journal(),
        positions=Positions(),
        progress=_progress(args),
    )
    _dump_json(_trim(summary))


def _cmd_ask(args) -> None:
    summary = run_ask(
        question=" ".join(args.question).strip() or None,
        symbols=_explicit_symbols(args) or None,
        output_dir=args.output_dir,
        compute_symbols=_live_facts(args),
        client=LlmClient(provider=args.llm_provider, model=args.llm_model) if args.llm else None,
        is_trading_day=TradingCalendar().is_trading_day(beijing_today()),
        deep=args.deep,
    )
    if summary["notes"] and summary.get("mode") != "deep":
        # 深挖模式的报告里已经有「数据边界」一节，再打一遍就是同一批话说两次。
        print("\n".join(f"（{note}）" for note in summary["notes"]))


def _cmd_facts(args) -> None:
    bundle = build_facts_bundle(
        moment=beijing_now(),
        symbols=_explicit_symbols(args) or None,
        output_dir=args.output_dir,
        compute_symbols=_live_facts(args),
        phase=args.phase,
        is_trading_day=TradingCalendar().is_trading_day(beijing_today()),
        focus=_explicit_symbols(args) or None,
        sections=args.sections,
    )
    if args.format == "text":
        print(
            bundle.rendered if args.full else getattr(bundle, "compact_rendered", bundle.rendered)
        )
        return
    _dump_json(bundle.to_dict(), args.output)


def _cmd_preopen_research(args) -> None:
    """生成每日交给 Codex 的固定输入，避免手工拼接与遗漏外盘时点。"""
    payload = build_preopen_research(
        output_dir=args.output_dir,
        market_adapter=None if args.no_global else GlobalMarketAdapter(),
    )
    if args.format == "markdown":
        rendered = render_preopen_research(payload)
        _write_text(rendered, args.output)
        return
    _dump_json(payload, args.output)


def _cmd_chat(args) -> None:
    # 适配器只建一次：单票现算与板块事实共用同一个连接与配额。
    adapter = _tickflow(args)
    run_chat(
        compute_symbols=lambda symbols: _advise_facts(adapter, symbols, args.period),
        compute_sectors=lambda: _morning_facts(adapter),
        symbols=_resolve_symbols(args),
        responder=ChatResponder(LlmClient(provider=args.llm_provider, model=args.llm_model))
        if args.llm
        else None,
    )


def _cmd_position(args) -> None:
    _dump_json(_position_command(args))


def _cmd_review(args) -> None:
    if args.daily is not None:
        day = beijing_today() if args.daily == "today" else date.fromisoformat(args.daily)
        summary = run_daily_review(
            args.output_dir,
            trade_date=day,
            notifiers=_notifiers(args, None, "review"),
        )
        _dump_json(_trim({key: value for key, value in summary.items() if key != "payload"}))
        return
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
        now=beijing_naive_now(),
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
        use_earnings=not args.no_earnings,
        top_n=args.top,
        debate=_debate(args),
        security_master_adapter=SecurityMasterAdapter(),
        journal=Journal(),
    )
    _dump_json(_trim(summary))


def _cmd_pre_open(args) -> None:
    try:
        summary = run_pre_open(
            payload_path=args.payload,
            output_dir=args.output_dir,
            adapter=_tickflow(args) if args.refresh else None,
        )
    except FileNotFoundError as exc:
        source = args.payload or f"{args.output_dir or 'briefs'}/{POST_CLOSE_LATEST_NAME}"
        raise SystemExit(f"未找到盘后执行卡 {source}；先运行 `ashare-ds post-close-brief`") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"盘后执行卡不是有效 JSON：{exc.msg}") from exc
    _dump_json(_trim(summary))


def _cmd_strategy_screen(args) -> None:
    """Run the deterministic full-universe strategy scanner for AI consumption."""
    # 只要标的画像（名称与行业），不需要概念那一层，所以不走 `static_taxonomies`。
    taxonomy = ThsIndustryTaxonomy()
    profiles = taxonomy.snapshot.profiles
    end = beijing_today()
    print(
        f"正在读取 {len(profiles)} 只股票的日线并运行策略筛选；全市场模式通常需要 1–2 分钟…",
        file=sys.stderr,
        flush=True,
    )
    fetch_notes: list[str] = []
    bars = fetch_daily_bars(
        _tickflow(args),
        sorted(profiles),
        end - timedelta(days=max(90, args.days)),
        end,
        notes=fetch_notes,
    )
    usable = sum(bool(rows) for rows in bars.values())
    print(
        f"日线读取完成：{usable}/{len(profiles)} 只有数据，正在计算候选…",
        file=sys.stderr,
        flush=True,
    )
    names = {symbol: profile.name for symbol, profile in profiles.items()}
    sectors = {symbol: profile.industry for symbol, profile in profiles.items()}
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
        payload = screen_all_profiles(
            bars, names=names, sectors=sectors, common=common, limit=args.limit
        )
    else:
        payload = screen_overnight(
            bars,
            names=names,
            sectors=sectors,
            config=StrategyScreenConfig(profile=args.profile, **common),
        ).to_dict()
    # `trade_date` 是信号日（最后一根日线），`as_of` 是跑批那天。事实包按前者判时效：
    # 周一盘前读上周五的筛选，是「上一交易日」，不是「时点未知」。
    signal_date = latest_signal_date(bars)
    payload["trade_date"] = signal_date
    payload["as_of"] = str(end)
    payload["data_notes"] = screen_notes(
        args.days, [*taxonomy.notes, *fetch_notes], signal_date=signal_date
    )
    output_path = args.output or str(Path(args.output_dir) / STRATEGY_SCREEN_LATEST_NAME)
    _dump_json(payload, output_path)
    if args.output is None:
        profiles = payload.get("profiles") or {}
        counts = {
            name: item.get("summary", {}).get("matched_total", 0) for name, item in profiles.items()
        }
        _dump_json(
            {
                "as_of": payload["as_of"],
                "signal_date": signal_date,
                "profile_matches": counts or {args.profile: payload["summary"]["matched_total"]},
                "shortlist": len(
                    payload.get("diversified_shortlist") or payload.get("candidates") or []
                ),
                "data_notes": payload["data_notes"],
                "full_payload": output_path,
            }
        )


def _cmd_sector_leaders(args) -> None:
    if args.refresh:
        levels = ("industry", "concept") if args.level == "all" else (args.level,)
        payload = refresh_sector_leaders(
            output_dir=args.output_dir,
            levels=levels,
            top_per_sector=args.top,
            lookback_days=args.days,
            min_bars=args.min_history_bars,
            adapter=_tickflow(args),
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    else:
        path = Path(args.output_dir) / LEADERS_LATEST_NAME
        if not path.is_file():
            raise SystemExit(f"未找到 {path}；先加 --refresh 生成全板块龙头事实包")
        payload = json.loads(path.read_text(encoding="utf-8"))
    _dump_json(payload, args.output)


def _cmd_monthly_context(args) -> None:
    payload = build_monthly_context(
        output_dir=args.output_dir,
        horizon_days=args.horizon_days,
        symbols=args.symbols,
    )
    _dump_json(payload, args.output)


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


def _cmd_state(args) -> None:
    _dump_json(_state_report())


def _cmd_calendar_refresh(args) -> None:
    """刷新源数据覆盖范围内的日历，缺数据时保留原表。"""
    calendar = TradingCalendar()
    year = args.year if args.year is not None else beijing_today().year
    adapter = _tickflow(args) if args.source == "tickflow" else AkshareCalendarAdapter()
    trading_days = calendar.refresh_from(adapter, year)
    _dump_json(
        {
            "year": year,
            "source": args.source,
            "path": str(calendar.path),
            "trading_days": trading_days,
            "holidays": len(calendar.holidays),
            "workdays": len(calendar.workdays),
            "note": (
                f"已读取 {year} 年 {trading_days} 个交易日，仅更新源数据覆盖范围内的日历"
                if trading_days
                else f"{args.source} 未提供 {year} 年有效交易日历，节假日表保持原状（当前 "
                f"{len(calendar.holidays)} 个假日）"
            ),
        }
    )


def _cmd_history_refresh(args) -> None:
    """刷新历史序列缓存，让外盘映射基准率有样本可查。"""
    tasks: list[str] = []
    notes: list[str] = []
    payload: dict[str, object] = {"tasks": tasks, "notes": notes}
    if args.only in ("all", "sector-index"):
        sector = ThsSectorIndexAdapter()
        result = sector.backfill(
            since_year=args.since_year or BACKFILL_START_YEAR,
            until_year=args.until_year,
        )
        payload["sector_index"] = result.to_dict()
        tasks.append("sector-index")
        notes.extend(result.notes)

    if args.only in ("all", "overseas"):
        result = OverseasHistoryAdapter().refresh(args.keys)
        payload["overseas"] = result.to_dict()
        tasks.append("overseas")
        notes.extend(result.notes)

    _dump_json(payload)


COMMANDS = {
    "daemon": _cmd_daemon,
    "morning": _cmd_morning,
    "sector-analysis": _cmd_sector_analysis,
    "sector-backtest": _cmd_sector_backtest,
    "sector-forecast": _cmd_sector_forecast,
    "sector-confirmation": _cmd_sector_confirmation,
    "monitor": _cmd_monitor,
    "advise": _cmd_advise,
    "ask": _cmd_ask,
    "facts": _cmd_facts,
    "preopen-research": _cmd_preopen_research,
    "chat": _cmd_chat,
    "position": _cmd_position,
    "review": _cmd_review,
    "journal": _cmd_journal,
    "codex-adjudication-packet": _cmd_codex_adjudication_packet,
    "codex-adjudication-validate": _cmd_codex_adjudication_validate,
    "post-close-brief": _cmd_post_close,
    "pre-open-brief": _cmd_pre_open,
    "strategy-screen": _cmd_strategy_screen,
    "sector-leaders": _cmd_sector_leaders,
    "monthly-context": _cmd_monthly_context,
    "tick-health": _cmd_tick_health,
    "intraday-check": _cmd_intraday_check,
    "state": _cmd_state,
    "calendar-refresh": _cmd_calendar_refresh,
    "history-refresh": _cmd_history_refresh,
}


def main() -> None:
    # 先于解析参数：`.env.tickflow` 里的 key 决定了 TickFlow 走付费档还是免费档，
    # 而适配器在命令分支里就构造了。已经 export 的变量不受影响。
    load_env_files()
    args = build_parser().parse_args()
    # `required=True` 的 subparsers 保证 command 一定在表里；真取不到就抛，
    # 那是接线漏了一条命令，不该悄悄什么都不做。
    try:
        COMMANDS[args.command](args)
    except (OSError, RuntimeError, ValueError) as exc:
        # 数据源不可用或输入不合法时，输出原因和退出码。
        # 详细异常已经在各层 notes 中保留；手工调试仍可直接调用对应 Python API。
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from None


def _position_command(args) -> dict:
    book = Positions()
    if args.action == "open":
        entry = book.open(
            args.symbol,
            cost_price=args.cost,
            shares=args.shares,
            opened_on=date.fromisoformat(args.opened_on) if args.opened_on else None,
            plan_stop=args.stop,
            plan_target=args.target,
            note=args.note,
        )
        return {"position": entry.to_dict(), "total": len(book)}

    if args.action == "close":
        symbols = list(dict.fromkeys(normalize_symbol(symbol) for symbol in args.symbols))
        closed = [symbol for symbol in symbols if book.close(symbol)]
        missing = [symbol for symbol in symbols if symbol not in closed]
        return {"closed": closed, "not_found": missing, "total": len(book)}

    if args.action == "clear":
        return {"cleared": book.clear(), "total": 0}

    # `position list` 是唯一一个「一次看到全部持仓」的地方，所以组合层读数挂在
    # 这里：单笔的止损与目标每一行都有，而「这几笔合起来是不是一笔重仓」在逐行
    # 输出里永远看不出来。归并用缓存里的元数据，不打网络（见 `cached()`）。
    read = read_portfolio(book.entries, master=_cached_security_master(book.entries))
    return {
        "positions": [entry.to_dict() for entry in book.entries],
        "total": len(book),
        "portfolio": read.to_dict(),
        "portfolio_notes": read.describe(),
        "path": str(book.path),
    }


def _cached_security_master(entries: list):
    """缓存里的标的元数据；读不到就是 None，归并那一层会说明「没有分类」。

    走 `cached()` 读取本地元数据：`position list`
    是一条随手敲的查询命令，不该为了给持仓打行业标签去打一次外网。不到两只干脆
    不读——归并不出东西，而那份缓存解析一次 58ms（判据同 `MIN_CLUSTER_SYMBOLS`）。
    """
    if len(entries) < MIN_CLUSTER_SYMBOLS:
        return None
    try:
        return SecurityMasterAdapter().cached()
    except Exception:
        return None


def _state_report() -> dict:
    from collections import Counter

    from ashare_decision_support.journal.records import VERIFIABLE_KINDS
    from ashare_decision_support.state import AlertState, DaemonState

    today = beijing_today()
    book = Positions()
    alerts = AlertState.load(today)
    daemon = DaemonState.load(today)
    calendar = TradingCalendar()
    journal = Journal()
    journal_rows = journal.rows()
    pending_rows = journal.pending(today)
    outcomes = journal.outcomes()
    journal_kinds = Counter(str(row.get("kind") or "unknown") for row in journal_rows)
    pending_kinds = Counter(str(row.get("kind") or "unknown") for row in pending_rows)
    outcome_stats = summarize(outcomes)
    return {
        "state_home": str(state_home()),
        "env_files_loaded": loaded_files(),
        "tickflow_mode": "full" if os.environ.get("TICKFLOW_API_KEY") else "free",
        "today": str(today),
        "trading_day": calendar.explain(today),
        "positions": book.symbols(),
        "alerts_today": alerts.fired_summary()[:20],
        # 板块告警是另一个状态文件，不单独读的话 `state` 会漏报整条链路。
        "sector_alerts_today": fired_sectors_today(today)[:20],
        "daemon_tasks": daemon.tasks,
        "journal": {
            # 三类原始样本都会兑现。此前这里只拿 recommendation 和 pending 比，
            # 885 > 356 看起来像重复记录，实际多出来的是 control/vetoed 对照样本。
            "recommendations": journal_kinds["recommendation"],
            "controls": journal_kinds["control"],
            "vetoed": journal_kinds["vetoed"],
            "verifiable_total": sum(journal_kinds[kind] for kind in VERIFIABLE_KINDS),
            "outcomes": len(outcomes),
            "pending_verification": len(pending_rows),
            "pending_by_kind": {kind: pending_kinds[kind] for kind in VERIFIABLE_KINDS},
            "model_samples": outcome_stats["samples"],
            "model_ready_gap": outcome_stats["model_ready_gap"],
        },
        "feishu_configured": FeishuNotifier().configured,
        "checked_at": beijing_now().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    main()
