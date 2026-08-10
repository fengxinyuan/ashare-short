"""Continuous analysis over a live fact context.

The session holds two things: **facts** the rule engine computed, and the
**conversation** about them. The split is the whole point — the model answers
from a fact bundle it did not produce and cannot extend, so "继续分析" means
recomputing with the engine and re-answering, never letting the model reason
its way to a new number.

`/` commands mutate the fact context. Free text queries it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ashare_decision_support.advice.view import (
    entry_band,
    is_advice_like,
    short,
    sort_advice_items,
    trade_context_parts_from_dict,
)
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.writer.llm import LlmClient, unverified_numbers

SYSTEM_PROMPT = """你是一名 A 股短线分析助手，服务于一位自己做超短线的交易者。

你能看到的全部信息，就是每轮对话附带的「当前事实」JSON。它由规则引擎算出。

回答风格：
1. 像研究员在讲盘，不像报表导出。先给一句判断，再给最关键的证据，最后说风险、分歧和下一步观察条件。
2. 只依据「当前事实」回答。事实里没有的，直接说「当前数据里没有」，不要推测、不要用常识补。
3. 不得出现事实中没有的数字。价格、评分、涨跌幅一律照抄。
4. 不得给出「概率」「胜率」「预期收益率」这类数字——系统没有拟合模型，给不出来。
5. 不要直接说「建议买入/卖出」。可以陈述系统算出的价位、情景、风险与失效条件，由用户自己决定。
6. 多个标的时先比优先级，不要按字段顺序逐条复述；能合并的信息就合并成自然语言。

你可以回答趋势分析、股价分析、自选股监控结果和买卖点相关问题。

如果用户问的标的不在事实里，提示他用 `/add <代码>` 加入后再问。"""

# Conversation turns kept in the prompt. Older turns fall out; the fact bundle
# is re-sent every turn, so dropping history loses phrasing, never data.
HISTORY_TURNS = 8


@dataclass
class ChatTurn:
    role: str
    content: str


@dataclass
class ChatSession:
    """Fact context plus conversation history."""

    symbols: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    history: list[ChatTurn] = field(default_factory=list)
    trade_date: date | None = None

    def facts_text(self) -> str:
        return json.dumps(self.facts, ensure_ascii=False, indent=2, default=str)

    def add_symbols(self, symbols: list[str]) -> list[str]:
        """Add symbols not already tracked. Returns the ones actually added."""
        added = [s for s in symbols if s not in self.symbols]
        self.symbols.extend(added)
        return added

    def remove_symbol(self, symbol: str) -> bool:
        if symbol not in self.symbols:
            return False
        self.symbols.remove(symbol)
        for bundle in self.facts.values():
            if isinstance(bundle, dict) and isinstance(bundle.get("symbols"), list):
                bundle["symbols"] = [
                    item for item in bundle["symbols"] if item.get("symbol") != symbol
                ]
        return True

    def set_facts(self, key: str, payload: dict) -> None:
        # Copied, not referenced: `remove_symbol` edits the bundle in place, and
        # holding the caller's object would let a `/drop` mutate whatever the
        # job layer still has a handle on.
        self.facts[key] = deepcopy(payload)

    def record(self, role: str, content: str) -> None:
        self.history.append(ChatTurn(role=role, content=content))

    def messages(self, question: str) -> list[dict]:
        recent = self.history[-HISTORY_TURNS * 2 :]
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            *({"role": turn.role, "content": turn.content} for turn in recent),
            {
                "role": "user",
                "content": (
                    f"当前事实：\n```json\n{self.facts_text()}\n```\n\n问题：{question}"
                ),
            },
        ]

    @property
    def has_facts(self) -> bool:
        return bool(self.facts)


class ChatResponder:
    """Answers questions about a session's facts, or explains why it cannot."""

    def __init__(self, client: LlmClient | None = None) -> None:
        self.client = client
        self.allow_llm = client is not None

    def answer(self, session: ChatSession, question: str) -> tuple[str, list[str]]:
        """Return (answer, notes). Never raises."""
        if not self.allow_llm:
            answer = render_facts_summary(session)
            session.record("user", question)
            session.record("assistant", answer)
            return (answer, ["默认不开模型对话；如需模型分析请显式开启 `--llm`"])
        if self.client is None or not self.client.configured:
            return (
                (f"未配置 {self.client.provider.api_key_env if self.client else 'LLM'}，对话功能不可用。"
                "价位和情景可以直接用 `/show` 查看。"),
                ["LLM 未配置"],
            )
        if not session.has_facts:
            return ("当前没有任何标的在上下文里，先用 `/add <代码>` 加入。", [])

        try:
            reply = self.client.chat(session.messages(question))
        except Exception as exc:
            return (f"LLM 调用失败（{type(exc).__name__}），本轮无法回答。", [str(exc)[:120]])

        offenders = unverified_numbers(reply, session.facts_text())
        if offenders:
            # Same gate as the brief writer: a number with no basis in the facts
            # is a fabrication, and in a conversation it is more dangerous than
            # in a report because it arrives without the surrounding table.
            return (
                "本轮回答包含数据中不存在的数字（"
                + "、".join(offenders[:5])
                + "），已丢弃。请换个问法，或用 `/show` 直接看原始数据。",
                [f"数字校验拦截：{'、'.join(offenders[:5])}"],
            )

        session.record("user", question)
        session.record("assistant", reply)
        return reply, []


def render_facts_summary(session: ChatSession) -> str:
    """Plain rendering of what is currently in context, no LLM involved."""
    if not session.has_facts:
        return "上下文为空。"

    lines: list[str] = ["当前上下文已装好，先看最接近动作的部分。"]
    for key, bundle in session.facts.items():
        if not isinstance(bundle, dict):
            lines.extend(_render_generic_summary(key, bundle))
            continue
        if key == "sectors":
            lines.extend(_render_sector_summary(bundle))
            continue
        if key == "advice":
            lines.extend(_render_advice_summary(bundle))
            continue
        if key == "watchlist":
            lines.extend(_render_watchlist_summary(bundle))
            continue
        if key == "alerts":
            lines.extend(_render_alert_summary(bundle))
            continue
        if key == "journal":
            lines.extend(_render_journal_summary(bundle))
            continue
        lines.extend(_render_generic_summary(key, bundle))
    return "\n".join(lines)


def _render_sector_summary(bundle: dict[str, Any]) -> list[str]:
    lines = ["", "## 盘前主链路"]
    market_direction = bundle.get("market_direction")
    if market_direction:
        score = bundle.get("market_score")
        if isinstance(score, (int, float)):
            lines.append(f"- 大盘 {market_direction}，综合读数 {float(score):.1f}/100")
        else:
            lines.append(f"- 大盘 {market_direction}")
    position = bundle.get("position_factor")
    if position is not None:
        lines.append(f"- 仓位建议 {position}")
    sectors = bundle.get("sectors_selected") or []
    if sectors:
        lines.append(f"- 先看板块：{'、'.join(sectors[:5])}")
    candidates = bundle.get("candidate_symbols") or []
    if candidates:
        lines.append(f"- 候选池 {len(candidates)} 只：{'、'.join(candidates[:6])}")
    if bundle.get("limit_up") is not None or bundle.get("blowups") is not None:
        pieces = []
        if bundle.get("limit_up") is not None:
            pieces.append(f"涨停 {bundle.get('limit_up')}")
        if bundle.get("blowups") is not None:
            pieces.append(f"炸板 {bundle.get('blowups')}")
        if bundle.get("max_streak") is not None:
            pieces.append(f"最高连板 {bundle.get('max_streak')}")
        if pieces:
            lines.append("- 情绪读数：" + "，".join(pieces))
    _append_notes(lines, bundle.get("data_notes") or [], limit=4)
    return lines


def _render_advice_summary(bundle: dict[str, Any]) -> list[str]:
    lines = ["", "## 买卖点"]
    items = sort_advice_items(bundle.get("symbols"))
    if not items:
        lines.append("- 暂无买卖点数据。")
        return lines
    for item in items[:8]:
        parts: list[str] = []
        shortline = item.get("shortline") or {}
        if shortline.get("summary"):
            parts.append(str(shortline.get("summary")))
        if item.get("trend_state"):
            parts.append(f"趋势 {item.get('trend_state')}")
        if item.get("score") is not None:
            parts.append(f"评分 {item.get('score')}")
        if item.get("last_price") is not None:
            parts.append(f"现价 {item.get('last_price')}")
        parts.append(f"买入 {entry_band(item)}")
        if item.get("stop") is not None:
            parts.append(f"止损 {item.get('stop')}")
        if item.get("targets"):
            parts.append(f"目标 {item.get('targets')}")
        if item.get("rrr") is not None:
            parts.append(f"盈亏比 {item.get('rrr')}")
        parts.extend(trade_context_parts_from_dict(item))
        lines.append(f"- {item.get('symbol')} {item.get('name') or ''}".rstrip() + "：" + "；".join(parts))
        reasons = shortline.get("reasons") or []
        if reasons:
            lines.append("  - 短线依据：" + "；".join(str(reason) for reason in reasons[:4]))
        warnings = item.get("warnings") or []
        if warnings:
            lines.append("  - 风险：" + "；".join(warnings[:3]))
    return lines


def _render_watchlist_summary(bundle: dict[str, Any]) -> list[str]:
    lines = ["", "## 自选与持仓"]
    watchlist = bundle.get("watchlist") or []
    candidates = bundle.get("candidates") or []
    holdings = [item for item in watchlist if item.get("holding")]
    watch_items = [item for item in watchlist if not item.get("holding")]
    lines.append(
        f"- 自选 {len(watchlist)} 只，持仓 {len(holdings)} 只，候选池 {len(candidates)} 只"
    )
    trade_date = bundle.get("candidates_trade_date")
    if trade_date:
        lines.append(f"- 候选池日期 {trade_date}")
    if holdings:
        lines.append(
            "- 持仓：" + "、".join((item.get("symbol") or "") for item in holdings[:8])
        )
    if watch_items:
        lines.append(
            "- 自选：" + "、".join((item.get("symbol") or "") for item in watch_items[:8])
        )
    if candidates:
        lines.append(
            "- 候选：" + "、".join((item.get("symbol") or "") for item in candidates[:8])
        )
    return lines


def _render_alert_summary(bundle: dict[str, Any]) -> list[str]:
    lines = ["", "## 告警"]
    fired = bundle.get("fired") or []
    if not fired:
        lines.append("- 今天还没有触发任何告警。")
        return lines
    lines.append(f"- 今日已触发 {len(fired)} 条告警")
    for row in fired[:5]:
        stamp = str(row.get("at") or "")[-8:]
        lines.append(f"  - {stamp} {row.get('symbol') or ''} {row.get('trigger') or ''}".rstrip())
    return lines


def _render_journal_summary(bundle: dict[str, Any]) -> list[str]:
    lines = ["", "## 台账"]
    if "rows" in bundle:
        rows = bundle.get("rows") or []
        lines.append(f"- {bundle.get('symbol') or '台账'} 共 {len(rows)} 条")
        for row in rows[:5]:
            lines.append(
                f"  - {row.get('date') or ''} {row.get('action') or ''} {row.get('price') or ''}".rstrip()
            )
    else:
        lines.append(f"- 已兑现 {bundle.get('samples')} 条")
        overall = bundle.get("overall") or {}
        lines.append(
            f"- 先到目标 {_rate(overall.get('target_first_rate'))}，"
            f"先到止损 {_rate(overall.get('stop_first_rate'))}"
        )
    return lines


def _render_generic_summary(key: str, bundle: object) -> list[str]:
    lines = ["", f"## {key}"]
    if not isinstance(bundle, dict):
        lines.append(f"- {short(bundle)}")
        return lines
    if is_advice_like(bundle):
        return _render_advice_summary(bundle)
    symbols = [item for item in (bundle.get("symbols") or []) if isinstance(item, dict)]
    if not symbols:
        lines.append(f"- {short(bundle)}")
        return lines
    for item in symbols[:8]:
        parts: list[str] = []
        if item.get("trend_state"):
            parts.append(f"趋势 {item.get('trend_state')}")
        if item.get("score") is not None:
            parts.append(f"评分 {item.get('score')}")
        if item.get("direction"):
            parts.append(str(item.get("direction")))
        if item.get("last_price") is not None:
            parts.append(f"现价 {item.get('last_price')}")
        lines.append(
            f"- {(item.get('symbol') or '')} {item.get('name') or ''}".rstrip()
            + ("：" + "；".join(parts) if parts else "")
        )
    return lines


def _append_notes(lines: list[str], notes: list[str], limit: int = 6) -> None:
    if not notes:
        return
    picked = notes[:limit]
    for note in picked:
        lines.append(f"- 说明：{note}")
    if len(notes) > limit:
        lines.append(f"- 另有 {len(notes) - limit} 条说明未展开")


HELP_TEXT = """可用命令：
  /add <代码> [代码...]   把标的加入上下文并计算
  /drop <代码>            从上下文移除
  /refresh                重新计算当前所有标的
  /sectors                拉取盘前趋势分析：大盘方向与板块排序
  /watch                  载入自选股、候选池和对应买卖点
  /alerts                 今天已触发的盘中告警
  /journal [代码]         台账：历史建议与兑现情况
  /debate                 显式做一次多空辩论（需要模型）
  /show                   直接打印上下文里的原始数据（默认路径，不经 LLM）
  /clear                  清空对话历史（保留事实）
  /help                   显示本帮助
  /quit                   退出

直接输入问题即可对话，例如「今天板块趋势怎么样」「600519 现在的买卖点是什么」「自选股里谁先触发告警」。"""


def handle_command(  # noqa: PLR0911, PLR0912 - 见下
    session: ChatSession,
    line: str,
    compute_symbols: Callable[[list[str]], dict],
    compute_sectors: Callable[[], dict],
) -> str:
    """Execute a `/` command. Returns the text to print.

    这里**故意**保留 if 链，没有像 `cli.py::main` 那样改成分发表。两处形似，
    但代价不同：`main` 的每个分支是十几行的调用，拆出去各自成篇；这里十二个
    分支多数只有一到六行，而它们需要的入参各不相同（`args` / `compute_symbols`
    / `compute_sectors` / 只要 `session`）。要塞进一张表就得给每个处理函数配一副
    统一签名，于是每个一行的处理逻辑变成三行样板，读者要跳两次才能看到那一行。

    换句话说：`PLR0911/0912` 数的是分支个数，而这里真正影响可读性的是**分支的
    厚度**。十二条并列的短分支就是一份菜单，摊平比拆开好读。
    """
    parts = line.split()
    command, args = parts[0].lower(), parts[1:]

    if command in {"/help", "/h"}:
        return HELP_TEXT
    if command == "/show":
        return render_facts_summary(session)
    if command == "/clear":
        session.history.clear()
        return "对话历史已清空，事实上下文保留。"
    if command == "/add":
        if not args:
            return "用法：/add 600519.SH 000001.SZ"
        added = session.add_symbols(args)
        if not added:
            return "这些标的已在上下文里。"
        session.set_facts("advice", compute_symbols(session.symbols))
        return f"已加入 {'、'.join(added)}，并完成计算。用 /show 查看。"
    if command == "/drop":
        if not args:
            return "用法：/drop 600519.SH"
        removed = [symbol for symbol in args if session.remove_symbol(symbol)]
        return f"已移除 {'、'.join(removed)}。" if removed else "上下文里没有这些标的。"
    if command == "/refresh":
        if not session.symbols:
            return "上下文为空，先用 /add 加入标的。"
        session.set_facts("advice", compute_symbols(session.symbols))
        return f"已重新计算 {len(session.symbols)} 只标的。"
    if command == "/sectors":
        session.set_facts("sectors", compute_sectors())
        return "板块排序与大盘方向已加入上下文。"

    # 下面三条读的是自动化链路正在维护的那份状态——对话问的和守护进程看到的
    # 是同一批数据，而不是各算各的。
    if command == "/watch":
        return _load_watchlist(session, compute_symbols)
    if command == "/alerts":
        return _load_alerts(session)
    if command == "/journal":
        return _load_journal(session, args[0] if args else None)
    if command == "/debate":
        return _run_debate(session)
    return f"未知命令 {command}，输入 /help 查看可用命令。"


def _run_debate(session: ChatSession) -> str:
    """对当前事实跑一次三角色辩论。

    对话默认是单个回答者，天然会把反面证据压进从句。`/debate` 强制把同一份事实
    拆给对立两方各自举证，再让第三方指出分歧点取决于哪个价位——和盘前简报用的是
    同一套角色与同一道数字闸门。
    """
    if not session.has_facts:
        return "上下文为空，先用 /add 或 /watch 加入标的。"

    from ashare_decision_support.agents.debate import DebatePanel

    panel = DebatePanel()
    if not getattr(panel.client, "configured", False):
        return "未配置 LLM key，多空辩论不可用；确定性结论不受影响，可用 /show 查看。"

    result = panel.run(session.facts)
    body = result.markdown()
    notes = "\n".join(f"（{note}）" for note in result.notes)
    if not body:
        return notes or "本轮辩论没有产出可用内容。"
    return f"{body}\n\n{notes}".strip()


def _load_watchlist(session: ChatSession, compute_symbols: Callable[[list[str]], dict]) -> str:
    from ashare_decision_support.state import CandidatePool, Watchlist

    book, pool = Watchlist(), CandidatePool()
    symbols = list(dict.fromkeys(book.symbols() + pool.symbols()))
    if not symbols:
        return "自选股与候选池都为空。用 `ashare-ds watchlist add <代码>` 加入标的。"

    session.add_symbols(symbols)
    session.set_facts("advice", compute_symbols(session.symbols))
    session.set_facts(
        "watchlist",
        {
            "watchlist": [entry.to_dict() for entry in book.entries],
            "candidates": [entry.to_dict() for entry in pool.entries],
            "candidates_trade_date": pool.trade_date,
        },
    )
    return (
        f"已载入自选股 {len(book)} 只、持仓 {len(book.holdings())} 只、候选池 {len(pool)} 只。"
        "先看持仓，再看自选，最后看候选。"
    )


def _load_alerts(session: ChatSession) -> str:

    from ashare_decision_support.monitor.sector_triggers import fired_sectors_today
    from ashare_decision_support.state import AlertState

    today = beijing_today()
    fired = AlertState.load(today).fired_summary()
    # 板块告警是另一个状态文件，不读它的话「今天板块有什么异动」永远答「没有」。
    sectors = fired_sectors_today(today)
    if not fired and not sectors:
        return "今天还没有触发任何告警。"

    session.set_facts(
        "alerts", {"date": str(today), "fired": fired, "sectors": sectors}
    )
    lines: list[str] = []
    if fired:
        lines.append(f"今日已触发 {len(fired)} 条个股告警，先看持仓，再看候选。")
        lines.extend(f"  {row['at'][-8:]}  {row['symbol']}  {row['trigger']}" for row in fired[:15])
    if sectors:
        lines.append(f"今日已推 {len(sectors)} 条板块告警（先看哪里，不是买什么）。")
        lines.extend(
            f"  {str(row['at'])[-8:]}  {row['sector_label']}·{row['sector']}  {row['label']}"
            for row in sectors[:10]
        )
    return "\n".join(lines)


def _load_journal(session: ChatSession, symbol: str | None) -> str:
    from ashare_decision_support.journal import Journal, summarize

    journal = Journal()
    if symbol:
        rows = journal.history_for(symbol)
        if not rows:
            return f"台账里没有 {symbol} 的记录。"
        session.set_facts("journal", {"symbol": symbol, "rows": rows})
        return f"已载入 {symbol} 的 {len(rows)} 条台账记录，先看最近几条，再看完整明细。"

    stats = summarize(journal.outcomes())
    session.set_facts("journal", stats)
    if not stats["samples"]:
        return "台账里还没有已兑现的记录。跑几天 `morning` 之后 `review` 会自动结算。"
    overall = stats["overall"]
    return (
        f"已兑现 {stats['samples']} 条（距模型门槛还差 {stats['model_ready_gap']} 条）；"
        f"先到目标 {_rate(overall['target_first_rate'])}，先到止损 {_rate(overall['stop_first_rate'])}。"
    )


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"
