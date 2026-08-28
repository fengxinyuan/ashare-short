"""按时刻装配上下文。

这个模块只做一件事：决定「现在该把什么放进模型看得见的范围」，然后去把它们读
出来。它**不做任何计算**——每一块都来自某个已经算好的地方（状态目录、台账、
落盘的 facts），或者来自调用方传进来的现算函数。

每一块都独立守卫。台账读不出来不该让板块排序也消失——这是 `agents/base.py` 那条
「单个源挂了不能拖垮跑批」在问答这一侧的同一个要求。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from ashare_decision_support.advice.view import (
    entry_band,
    is_advice_like,
    short,
    sort_advice_items,
    trade_context_parts_from_dict,
)
from ashare_decision_support.artifacts import (
    ADVICE_LATEST_NAME,
    MORNING_LATEST_NAME,
    POST_CLOSE_LATEST_NAME,
    STRATEGY_SCREEN_LATEST_NAME,
)
from ashare_decision_support.runhealth import HealthLedger
from ashare_decision_support.schedule.clock import AFTERNOON_CLOSE
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.strategy.portfolio import MIN_CLUSTER_SYMBOLS, read_portfolio

PHASE_PRE_OPEN = "pre_open"
PHASE_INTRADAY = "intraday"
PHASE_POST_CLOSE = "post_close"
PHASE_OFF_HOURS = "off_hours"

PHASE_LABELS = {
    PHASE_PRE_OPEN: "开盘前",
    PHASE_INTRADAY: "盘中",
    PHASE_POST_CLOSE: "盘后",
    PHASE_OFF_HOURS: "非交易时段",
}
PHASES = tuple(PHASE_LABELS)

# 时段边界。09:15 起算盘前是因为集合竞价从那一刻开始报价；15:00 收盘之后到
# 次日 05:00 都算盘后，因为那段时间你问的仍然是「今天发生了什么」。
PRE_OPEN_FROM = clock_time(5, 0)
SESSION_OPEN = clock_time(9, 15)
# 收盘时刻取 `schedule/clock.py` 那一份，不在这里再写一遍 `time(15, 0)`。
#
# 09:15 那个开盘时刻是**本层特有**的（集合竞价起算，不是 clock 的 09:30），所以
# 它留在本地；收盘时刻则和全仓一致，没有第二种口径。共用的是取值，不是叫法——
# 本层的时段判断读作 `SESSION_CLOSE` 更顺，别名保留这个词。
SESSION_CLOSE = AFTERNOON_CLOSE


@dataclass
class AskContext:
    """装配好的上下文，以及它是怎么装配出来的。"""

    phase: str
    as_of: str
    symbols: list[str] = field(default_factory=list)
    blocks: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # 问题点名的那几只。空表示没点名，此时全部标的一视同仁。
    focus: list[str] = field(default_factory=list)
    # 每一块的时点判定。装配过程中逐块记录，装配结束时自己也变成一个块。
    health: HealthLedger = field(default_factory=lambda: HealthLedger(today=beijing_today()))

    @property
    def phase_label(self) -> str:
        return PHASE_LABELS.get(self.phase, self.phase)

    def facts(self) -> dict[str, Any]:
        return {
            "时段": self.phase_label,
            "时刻": self.as_of,
            "标的": self.symbols,
            **self.blocks,
            "装配说明": self.notes,
        }

    def facts_text(self) -> str:
        return json.dumps(self.facts(), ensure_ascii=False, indent=2, default=str)

    def render(self) -> str:
        """不经模型的原样呈现。没配 key 时这是全部功能。"""
        lines = [f"【{self.phase_label}】{self.as_of}"]
        if self.symbols:
            lines.append(f"标的 {len(self.symbols)} 只：{'、'.join(self.symbols[:20])}")
        for name, block in self.blocks.items():
            lines.append("")
            lines.append(f"[{name}]")
            if is_advice_like(block):
                lines.extend(_render_advice_like_block(block))
                continue
            lines.extend(_render_block(block))
        if self.notes:
            lines.append("")
            lines.extend(f"（{note}）" for note in self.notes)
        return "\n".join(lines)

    def render_compact(self) -> str:
        """输出适合终端快速阅读的摘要，完整结构仍由 `render()` 保留。"""
        lines = [f"【{self.phase_label}】{self.as_of}"]
        if self.symbols:
            lines.append(f"标的 {len(self.symbols)} 只：{'、'.join(self.symbols[:20])}")
        for name, block in self.blocks.items():
            lines.extend(["", f"[{name}]"])
            lines.extend(_render_compact_block(name, block))
        if self.notes:
            lines.extend(["", "[数据提示]"])
            lines.extend(f"- {note}" for note in self.notes[:12])
            if len(self.notes) > 12:
                lines.append(f"- … 另有 {len(self.notes) - 12} 条")
        return "\n".join(lines)


def current_phase(moment: datetime) -> str:
    """现在算哪个时段。只看钟点，不判交易日——交易日历由调用方决定。"""
    now = moment.time()
    if now < PRE_OPEN_FROM:
        return PHASE_OFF_HOURS
    if now < SESSION_OPEN:
        return PHASE_PRE_OPEN
    if now < SESSION_CLOSE:
        return PHASE_INTRADAY
    return PHASE_POST_CLOSE


def resolve_phase(moment: datetime, is_trading_day: bool | None = None) -> str:
    """钟点与交易日历合并之后的时段。

    单独抽出来是因为**装配和提问都要用它**：`plan.py` 得先知道现在是哪个时段
    才能定默认关注点，而它不该为了拿这一个字符串把整份上下文先装配一遍。
    两处各判一次早晚会分叉。
    """
    phase = current_phase(moment)
    if is_trading_day is False and phase != PHASE_OFF_HOURS:
        return PHASE_OFF_HOURS
    return phase


def load_known_names() -> dict[str, str]:
    """简称 → 代码。只认持仓表里的名字。

    **不去全市场查名字**：那要么是一次网络往返，要么是一份会过期的本地表，
    而这一层的作用只是让「丸美生物低开了怎么办」里的名字能对上一个代码。对不上
    的名字由 `question.py` 明确报出来，比猜一个更有用。
    """
    try:
        from ashare_decision_support.state import Positions

        entries = list(Positions().entries)
    except Exception:
        return {}
    return {
        str(entry.name): entry.symbol
        for entry in entries
        if getattr(entry, "name", None) and entry.symbol
    }


def assemble(
    moment: datetime,
    symbols: list[str] | None = None,
    output_dir: str | Path | None = None,
    compute_symbols: Callable[[list[str]], dict] | None = None,
    phase: str | None = None,
    is_trading_day: bool | None = None,
    focus: list[str] | None = None,
) -> AskContext:
    """把这个时刻该有的上下文全部读出来。

    `compute_symbols` 是唯一会触发计算的入口（盘中的实时价位）。传 None 就只读
    已经落盘的那份，问答仍然可用——只是价位是上一次跑出来的，这一点会写进
    `notes`，不会假装是实时的。

    `focus` 非空时，实时计算**只覆盖这几只**。问三只票就取三只票的分钟线，而不是
    把持仓也一起算一遍——那是几十次 HTTP，而其中绝大多数不会出现在答案里。
    装配好的上下文仍然包含全部标的列表，只是没有它们的实时价位。

    显式传 `phase` 时**必须是四个时段之一**。认不出来的值以前会一路带下去：
    `phase_label` 原样回显，装配走进「非盘中」那条分支，产出一份自称
    `phase: banana` 的事实包——对着它问「现在该不该买」，答的是一个不存在的
    时段。调用方是模型时这一点尤其重要：拼错的枚举值要当场退回，不能变成一份
    看起来正常的结果。判据同 `research/leaders.py` 对 `levels` 的处置。
    """
    if phase is not None and phase not in PHASE_LABELS:
        raise ValueError(f"unsupported phase: {phase!r}; expected one of {list(PHASES)}")
    resolved = phase or resolve_phase(moment, is_trading_day)

    ctx = AskContext(phase=resolved, as_of=moment.isoformat(timespec="seconds"))
    ctx.health = HealthLedger(today=moment.date())
    held = _load_positions(ctx)
    ctx.symbols = list(dict.fromkeys((symbols or []) + held))
    ctx.focus = [symbol for symbol in (focus or []) if symbol]

    # 盘前 / 非交易时段：读最近一次主链路的产出。
    if resolved in (PHASE_PRE_OPEN, PHASE_OFF_HOURS, PHASE_POST_CLOSE):
        _load_payload(
            ctx, output_dir, MORNING_LATEST_NAME, "盘前主链路", trim=_trim_morning
        )
        _load_payload(
            ctx,
            output_dir,
            STRATEGY_SCREEN_LATEST_NAME,
            "参数化策略扫描",
            trim=_trim_strategy_screen,
        )

    if resolved == PHASE_INTRADAY:
        _load_payload(
            ctx, output_dir, MORNING_LATEST_NAME, "盘前主链路", trim=_trim_morning
        )
        _load_payload(
            ctx,
            output_dir,
            STRATEGY_SCREEN_LATEST_NAME,
            "参数化策略扫描",
            trim=_trim_strategy_screen,
        )
        _load_alerts(ctx, moment.date())
        _load_sector_confirmation(ctx, output_dir)
        _load_live_levels(ctx, compute_symbols)
    elif resolved == PHASE_POST_CLOSE:
        _load_alerts(ctx, moment.date())
        _load_sector_confirmation(ctx, output_dir)
        _load_payload(ctx, output_dir, POST_CLOSE_LATEST_NAME, "次日执行卡")
    else:
        # 盘前与非交易时段没有当日实时价位可谈，用最近一次算过的那份。
        _load_payload(ctx, output_dir, ADVICE_LATEST_NAME, "最近一次买卖点")

    _load_journal(ctx)

    # 时点判定放在最后，因为它读的是上面每一步记下来的状态。它是一个**块**而不是
    # 一条 note：这份包的主要读者是另一个模型，而对它来说「盘前主链路是三个交易日
    # 之前的」写在散文第七条里，和没写没有区别。
    ctx.blocks["数据健康"] = ctx.health.to_dict()

    if resolved == PHASE_OFF_HOURS:
        ctx.notes.append("非交易时段：以下全部是最近一次跑出来的旧数据，不是实时行情")
    if resolved == PHASE_PRE_OPEN:
        ctx.notes.append("开盘前：价位为昨收口径的结构位，开盘后需要用当日分时重算")
    return ctx


def _load_sector_confirmation(ctx: AskContext, output_dir: str | Path | None) -> None:
    """Join two local artifacts; failure must not hide the rest of the facts bundle."""
    try:
        ctx.blocks["板块预测盘中验证"] = _build_sector_confirmation(
            output_dir or "briefs"
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        ctx.notes.append(f"板块预测盘中验证不可用（{type(exc).__name__}: {exc}）")


def _build_sector_confirmation(output_dir: str | Path) -> dict[str, Any]:
    """Lazy import avoids research.monthly -> facts -> context import cycle."""
    from ashare_decision_support.research.sector_confirmation import (
        build_sector_confirmation,
    )

    return build_sector_confirmation(output_dir)


# 全市场筛选进上下文时保留几条。**这是唯一一处按体积截断的地方**，理由是它的
# 体积和它的信息量不成比例：实测一份三策略的扫描结果 110KB，其中 109KB 是 120 行
# 结构完全相同的候选，而这一层要回答的是「最近一次筛选挑出了什么样的票」。
# 完整结果一直在 `briefs/strategy_screen_latest.json` 里，截断多少条写进 notes——
# **静默截断读起来和「全都在这里」一模一样**，那正是这套系统各处都在防的事。
SCREEN_KEEP_PER_PROFILE = 5
SCREEN_KEEP_SHORTLIST = 10
MORNING_KEEP_CANDIDATES = 12


def _trim_morning(payload: Any) -> tuple[Any, list[str]]:
    """Keep decision facts; leave large audit matrices in morning_latest.json."""
    if not isinstance(payload, dict):
        return payload, []

    keep = (
        "trade_date", "news_as_of", "market", "rotation_price", "turnover_migration",
        "seesaw", "sector_index", "limit_pool", "rotation", "position_factor",
        "suggested_position_factor", "position_factor_reasons", "sectors", "candidates",
        "theme_radar", "macro_links", "sector_forecast_validation", "sector_clusters", "data_notes",
    )
    trimmed = {key: payload.get(key) for key in keep if payload.get(key) is not None}
    sectors = payload.get("sectors") or []
    trimmed["sectors"] = [_pick_sector(item) for item in sectors[:10]]
    candidates = payload.get("candidates") or []
    trimmed["candidates"] = [_pick_candidate(item) for item in candidates[:MORNING_KEEP_CANDIDATES]]
    trimmed["data_notes"] = (payload.get("data_notes") or [])[:20]
    for key in ("rotation_price", "turnover_migration", "seesaw", "sector_index"):
        trimmed[key] = _trim_diagnostic(trimmed.get(key))

    dropped_candidates = max(0, len(candidates) - len(trimmed["candidates"]))
    dropped_notes = max(0, len(payload.get("data_notes") or []) - len(trimmed["data_notes"]))
    omitted_sections = [key for key in payload if key not in keep]
    note = (
        "盘前主链路已为外部分析压缩："
        f"候选保留前 {len(trimmed['candidates'])} 只（省略 {dropped_candidates}），"
        f"数据说明省略 {dropped_notes} 条，审计块省略 {omitted_sections}；"
        f"完整结果见 {MORNING_LATEST_NAME}"
    )
    trimmed["截断说明"] = note
    return trimmed, [note]


def _pick_sector(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    keys = (
        "name", "level", "score", "direction", "ret_1d", "ret_5d", "breadth",
        "volume_ratio", "net_inflow", "reasons", "warnings", "headlines",
    )
    return {key: item[key] for key in keys if key in item}


def _pick_candidate(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    keys = (
        "symbol", "name", "sector", "score", "last_close", "trend_state",
        "stop_reference", "target_reference", "wyckoff_bias", "reasons", "warnings",
        "score_components", "setup", "playbook", "industry_path", "concepts", "trade_flow",
    )
    return {key: item[key] for key in keys if key in item}


def _trim_diagnostic(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "sectors":
            continue
        if isinstance(item, list):
            result[key] = item[:3] if key in {"observations", "notes"} else item[:5]
        else:
            result[key] = item
    return result


def _trim_strategy_screen(payload: Any) -> tuple[Any, list[str]]:
    """把筛选结果收成「每个策略的漏斗 + 前几名」。

    留下的三样是读的人真正会用的：`config`（这次按什么条件筛的）、`summary`
    （漏斗，回答「为什么只剩这么几只」）、以及每个策略的头几名。剩下的行不是
    没有价值，而是**它们的价值在文件里，不在上下文里**——把 120 行塞进模型的
    输入，换来的是它读不完、也答不准。
    """
    if not isinstance(payload, dict):
        return payload, []

    dropped = 0
    trimmed = dict(payload)
    profiles = payload.get("profiles")
    if isinstance(profiles, dict):
        kept_profiles = {}
        for name, item in profiles.items():
            if not isinstance(item, dict):
                kept_profiles[name] = item
                continue
            candidates = item.get("candidates")
            if isinstance(candidates, list) and len(candidates) > SCREEN_KEEP_PER_PROFILE:
                dropped += len(candidates) - SCREEN_KEEP_PER_PROFILE
                item = {**item, "candidates": candidates[:SCREEN_KEEP_PER_PROFILE]}
            kept_profiles[name] = item
        trimmed["profiles"] = kept_profiles

    shortlist = payload.get("diversified_shortlist")
    if isinstance(shortlist, list) and len(shortlist) > SCREEN_KEEP_SHORTLIST:
        dropped += len(shortlist) - SCREEN_KEEP_SHORTLIST
        trimmed["diversified_shortlist"] = shortlist[:SCREEN_KEEP_SHORTLIST]

    if not dropped:
        return payload, []
    note = (
        f"参数化策略扫描已按体积截断：每个策略保留前 {SCREEN_KEEP_PER_PROFILE} 只、"
        f"多样化短名单保留前 {SCREEN_KEEP_SHORTLIST} 只，共省略 {dropped} 行；"
        f"完整结果见 {STRATEGY_SCREEN_LATEST_NAME}"
    )
    trimmed["截断说明"] = note
    return trimmed, [note]


# ---- blocks --------------------------------------------------------------


def _load_positions(ctx: AskContext) -> list[str]:
    """持仓进上下文。**它是唯一还会自动进来的名单。**

    自选股与候选池删掉之后，其余标的一律由提问时显式给出——问哪只装哪只。
    持仓是例外，因为「我该不该卖」这个问题不该要求你先把代码再敲一遍，而回答
    它必须有成本价、计划止损和持有天数。
    """
    try:
        from ashare_decision_support.state import Positions

        book = Positions()
    except Exception as exc:
        ctx.notes.append(f"持仓读取失败（{type(exc).__name__}），本次不含这一块")
        return []

    # 摊平成几个短字段而不是直接 `to_dict()`。原因在渲染那一侧：`_short` 会把
    # 超过 160 字的条目截断，而 `asdict` 的字段顺序把成本价、计划止损、计划目标
    # 排在最后——正好是被截掉的那几个。没配 key 时 `render()` 就是全部输出，
    # 于是「我该不该卖」最需要的三个数恰恰看不见。
    #
    # 组合层那几个字段摊在同一层（`**read.to_dict()`）而不是嵌一层「组合」：
    # 「三只票里两只是同一个题材」和「这三只分别是什么」是同一个问题的两半，
    # 中间隔一个键名会让读的人（和模型）把它当成另一块可选内容跳过去。
    read = read_portfolio(book.entries, master=_cached_master(book.entries))
    ctx.blocks["持仓"] = {
        **read.to_dict(),
        "明细": [
            {
                "symbol": entry.symbol,
                "成本价": entry.cost_price,
                "股数": entry.shares,
                "建仓日": entry.opened_on,
                "持有天数": entry.held_days,
                "计划止损": entry.plan_stop,
                "计划目标": entry.plan_target,
            }
            for entry in book.entries
        ],
    }
    ctx.notes.extend(read.describe())
    if not book.entries:
        ctx.notes.append(
            "持仓表是空的；要问某只票直接把代码写进问题里，"
            "登记持仓用 `ashare-ds position open <代码> --cost <成本价>`"
        )
    return book.symbols()


def _cached_master(entries: list[Any]) -> Any:
    """标的元数据，只读缓存。取不到返回 None，归并那一层照旧能说出「没有分类」。

    在这里读而不是在 `strategy/portfolio.py` 里读：那一层是纯计算，它不该知道
    元数据是从缓存来的还是调用方传进来的——`jobs/advise.py` 那条链路手里已经有
    一份 master，将来接上时不该再读一次盘。

    **不到两只就不读。** 那份缓存是 1.9MB / 5550 只，解析一次 58ms，而这个函数在
    每次问答、每次 `facts`、每次 MCP 调用上都会走到——空仓和单票归并不出任何东西，
    那 58ms 是纯付出。判据用 `MIN_CLUSTER_SYMBOLS`，和归并那一侧同一个数。
    """
    if len(entries) < MIN_CLUSTER_SYMBOLS:
        return None
    try:
        from ashare_decision_support.adapters.security_master import SecurityMasterAdapter

        return SecurityMasterAdapter().cached()
    except Exception:
        return None


def _load_alerts(ctx: AskContext, today: date) -> None:
    try:
        from ashare_decision_support.state import AlertState

        fired = AlertState.load(today).fired_summary()
    except Exception as exc:
        ctx.notes.append(f"当日告警读取失败（{type(exc).__name__}）")
        return
    ctx.blocks["当日告警"] = {"日期": str(today), "已触发": fired, "条数": len(fired)}

    # 板块告警存在另一个文件里（个股与板块的 AlertState 不能共用一个路径），
    # 所以要单独读一次。少了这一次，「今天板块有什么异动」会答「没有任何告警」。
    try:
        from ashare_decision_support.monitor.sector_triggers import fired_sectors_today

        sectors = fired_sectors_today(today)
    except Exception as exc:
        ctx.notes.append(f"当日板块告警读取失败（{type(exc).__name__}）")
        sectors = []
    if sectors:
        ctx.blocks["当日板块告警"] = {"日期": str(today), "已触发": sectors, "条数": len(sectors)}

    if not fired and not sectors:
        ctx.notes.append("今天还没有触发任何告警")


def _load_journal(ctx: AskContext) -> None:
    try:
        from ashare_decision_support.journal import Journal, rolling_validation, summarize

        outcomes = Journal().outcomes()
        stats = summarize(outcomes)
    except Exception as exc:
        ctx.notes.append(f"台账统计读取失败（{type(exc).__name__}）")
        return
    ctx.blocks["台账统计"] = stats
    ctx.blocks["近期验证"] = rolling_validation(outcomes)
    if not stats.get("samples"):
        ctx.notes.append("台账里还没有已兑现的记录，统计全为空是正常的")
    else:
        ctx.notes.append("近期验证按交易日切窗，只用于判断近期有效性，不自动改写策略")


def _load_live_levels(
    ctx: AskContext, compute_symbols: Callable[[list[str]], dict] | None
) -> None:
    targets = ctx.focus or ctx.symbols
    if not targets:
        return
    if compute_symbols is None:
        ctx.notes.append("未提供实时计算入口，盘中价位取最近一次落盘结果，可能已过时")
        return
    if ctx.focus and len(ctx.focus) < len(ctx.symbols):
        ctx.notes.append(
            f"实时价位只算问题点名的 {len(ctx.focus)} 只，其余 "
            f"{len(ctx.symbols) - len(ctx.focus)} 只本次没有实时价位"
        )
    try:
        ctx.blocks["实时买卖点"] = compute_symbols(targets)
        ctx.health.live("实时买卖点")
    except Exception as exc:
        ctx.notes.append(f"实时买卖点计算失败（{type(exc).__name__}: {exc}），本次不含这一块")
        ctx.health.failed("实时买卖点", type(exc).__name__)


def _load_payload(
    ctx: AskContext,
    output_dir: str | Path | None,
    name: str,
    label: str,
    trim: Callable[[Any], tuple[Any, list[str]]] | None = None,
) -> None:
    if output_dir is None:
        return
    path = Path(output_dir) / name
    if not path.exists():
        ctx.notes.append(f"{label}尚无落盘结果（{path}），本次不含这一块")
        ctx.health.missing(label, "尚无落盘结果")
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        ctx.notes.append(f"{label}读取失败（{type(exc).__name__}）")
        ctx.health.failed(label, type(exc).__name__)
        return
    if trim is not None:
        payload, trim_notes = trim(payload)
        ctx.notes.extend(trim_notes)
    ctx.blocks[label] = payload
    stamp = payload.get("trade_date") if isinstance(payload, dict) else None
    read = ctx.health.stamped(label, stamp)
    if read.degraded:
        # 陈旧要在 notes 里也说一句：`render()`（没配 key 时的全部输出）是顺着读的，
        # 而这句话的作用是让人在读那份排序**之前**就知道它是哪天的。它已经包含
        # 数据日期，所以不再另外写一条「数据日期是 X」——两条并排只会互相稀释。
        ctx.notes.append(read.describe())
    elif stamp:
        ctx.notes.append(f"{label}的数据日期是 {stamp}")


def _render_block(block: Any, indent: str = "  ") -> list[str]:
    if isinstance(block, dict):
        lines = []
        for key, value in block.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{indent}{key}:")
                lines.extend(_render_block(value, indent + "  "))
            else:
                lines.append(f"{indent}{key}: {short(value)}")
        return lines
    if isinstance(block, list):
        return [f"{indent}- {short(item)}" for item in block[:12]] + (
            [f"{indent}… 另有 {len(block) - 12} 条"] if len(block) > 12 else []
        )
    return [f"{indent}{short(block)}"]


def _render_compact_block(name: str, block: Any) -> list[str]:
    """保留每块的决策字段，避免终端默认输出整份嵌套事实。"""
    if not isinstance(block, dict):
        return _render_block(block)[:16]
    special_renderer = {
        "数据健康": _render_data_health,
        "板块预测盘中验证": _render_sector_confirmation,
    }.get(name)
    if special_renderer is not None:
        return special_renderer(block)
    if name == "盘前主链路":
        market = block.get("market") or {}
        lines = [
            (
                f"  大盘：{market.get('direction', '—')}；评分 {short(market.get('score'))}；"
                f"上涨宽度 {short(market.get('breadth'))}；5日中位 {short(market.get('median_ret_5d'))}"
            )
        ]
        sectors = block.get("sectors") or []
        if sectors:
            lines.append(
                "  板块："
                + "；".join(
                    f"{item.get('name', '—')} {item.get('direction', '—')} {short(item.get('score'))}"
                    for item in sectors[:8]
                    if isinstance(item, dict)
                )
            )
        candidates = block.get("candidates") or []
        if candidates:
            lines.append(
                "  候选："
                + "；".join(
                    f"{item.get('symbol', '—')} {item.get('name', '')}".strip()
                    for item in candidates[:8]
                    if isinstance(item, dict)
                )
            )
        lines.extend(f"  边界：{note}" for note in (block.get("data_notes") or [])[:4])
        return lines
    if name == "参数化策略扫描":
        lines: list[str] = []
        for profile, item in (block.get("profiles") or {}).items():
            if not isinstance(item, dict):
                continue
            summary = item.get("summary") or {}
            candidates = item.get("candidates") or []
            symbols = "、".join(
                str(row.get("symbol")) for row in candidates[:4] if isinstance(row, dict)
            )
            lines.append(
                f"  {profile}：候选 {summary.get('candidates', len(candidates))}；"
                f"前列 {symbols or '—'}"
            )
        shortlist = block.get("diversified_shortlist") or []
        if shortlist:
            lines.append(
                "  综合短名单："
                + "、".join(str(row.get("symbol")) for row in shortlist[:8] if isinstance(row, dict))
            )
        return lines or ["  暂无筛选结果。"]
    if name in {"台账统计", "近期验证"}:
        keys = ("samples", "sessions", "trusted", "model_ready", "available_sessions")
        return [f"  {key}: {short(block[key])}" for key in keys if key in block]
    full = _render_block(block)
    return full[:24] + (["  … 已压缩，完整字段请使用 JSON"] if len(full) > 24 else [])


def _render_sector_confirmation(block: dict[str, Any]) -> list[str]:
    lines = [f"  截至：{block.get('as_of') or '—'}；{block.get('data_status', '—')}"]
    lines.extend(
        f"  - {item.get('name', '—')}：{item.get('status', '—')}；"
        f"超额 {_reading(item.get('excess_pct'))}；广度 {_reading(item.get('breadth'))}"
        for item in (block.get("results") or [])[:8]
        if isinstance(item, dict)
    )
    return lines


def _reading(value: Any) -> str:
    return "—" if value is None else short(value)


def _render_data_health(block: dict[str, Any]) -> list[str]:
    status = block.get("status") or block.get("判定") or "—"
    lines = [f"  总体：{status}"]
    for item in (block.get("blocks") or block.get("块") or [])[:8]:
        if isinstance(item, dict):
            lines.append(
                f"  - {item.get('name', item.get('名称', '—'))}："
                f"{item.get('status', item.get('判定', '—'))}"
            )
    return lines


def _render_advice_like_block(block: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    items = sort_advice_items(block.get("symbols"))
    for item in items[:10]:
        symbol = item.get("symbol") or ""
        name = item.get("name") or ""
        title = f"  - {symbol} {name}".rstrip()
        summary = _advice_summary(item)
        lines.append(title + (f"：{summary}" if summary else ""))
        reasons = (item.get("shortline") or {}).get("reasons") or []
        if reasons:
            lines.append("    依据：" + "；".join(str(reason) for reason in reasons[:4]))
        warnings = item.get("warnings") or []
        if warnings:
            lines.append("    风险：" + "；".join(str(warning) for warning in warnings[:3]))
    if not items:
        lines.append("  - 暂无买卖点数据。")
    return lines


def _advice_summary(item: dict[str, Any]) -> str:
    parts: list[str] = []
    shortline = item.get("shortline") or {}
    if shortline.get("summary"):
        parts.append(str(shortline.get("summary")))
    if item.get("trend_state"):
        parts.append(f"趋势 {item.get('trend_state')}")
    if item.get("last_price") is not None:
        parts.append(f"现价 {item.get('last_price')}")
    if item.get("entry_low") is not None:
        parts.append(f"买入 {entry_band(item)}")
    if item.get("stop") is not None:
        parts.append(f"止损 {item.get('stop')}")
    if item.get("targets"):
        parts.append(f"目标 {item.get('targets')}")
    parts.extend(trade_context_parts_from_dict(item))
    return "；".join(parts)
