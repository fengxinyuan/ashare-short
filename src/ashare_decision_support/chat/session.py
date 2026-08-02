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
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from ashare_decision_support.writer.llm import LlmClient, unverified_numbers

SYSTEM_PROMPT = """你是一名 A 股短线交易助理，服务于一位自己做超短线的交易者。

你能看到的全部信息，就是每轮对话附带的「当前事实」JSON。它由规则引擎算出。

严格约束：
1. 只依据「当前事实」回答。事实里没有的，直接说「当前数据里没有」，不要推测、不要用常识补。
2. 不得出现事实中没有的数字。价格、评分、涨跌幅一律照抄。
3. 不得给出「概率」「胜率」这类数字——系统没有拟合模型，给不出来。
4. 不得说「建议买入/卖出」。你可以陈述系统算出的价位、情景与风险，由用户自己决定。
5. 回答简短，直接给结论和价位，不铺陈背景。

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
        self.client = client or LlmClient()

    def answer(self, session: ChatSession, question: str) -> tuple[str, list[str]]:
        """Return (answer, notes). Never raises."""
        if not self.client.configured:
            return (
                f"未配置 {self.client.provider.api_key_env}，对话功能不可用。"
                "价位和情景可以直接用 `/show` 查看。",
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

    lines: list[str] = []
    for key, bundle in session.facts.items():
        lines.append(f"[{key}]")
        if not isinstance(bundle, dict):
            continue
        for item in bundle.get("symbols", []) or []:
            entry = (
                f"{item.get('entry_low')}-{item.get('entry_high')}"
                if item.get("entry_low") is not None
                else "—"
            )
            lines.append(
                f"  {item.get('symbol')} {item.get('name') or ''} "
                f"现价 {item.get('last_price')} 买入 {entry} "
                f"止损 {item.get('stop')} 目标 {item.get('targets')} 盈亏比 {item.get('rrr')}"
            )
        for item in (bundle.get("sectors") or [])[:8]:
            lines.append(
                f"  {item.get('name')} 评分 {item.get('score')} {item.get('direction')}"
            )
    return "\n".join(lines)


HELP_TEXT = """可用命令：
  /add <代码> [代码...]   把标的加入上下文并计算
  /drop <代码>            从上下文移除
  /refresh                重新计算当前所有标的
  /sectors                拉取盘前板块排序与大盘方向并加入上下文
  /watch                  载入自选股与今日候选池
  /alerts                 今天已触发的盘中告警
  /journal [代码]         台账：历史建议与兑现情况
  /debate                 对当前事实做一次多空辩论（多头举证 · 空头举证 · 分歧点）
  /show                   直接打印上下文里的原始数据（不经 LLM）
  /clear                  清空对话历史（保留事实）
  /help                   显示本帮助
  /quit                   退出

直接输入问题即可对话，例如「这几只哪个结构最好」「600519 跌破均价怎么办」。"""


def handle_command(
    session: ChatSession,
    line: str,
    compute_symbols: Callable[[list[str]], dict],
    compute_sectors: Callable[[], dict],
) -> str:
    """Execute a `/` command. Returns the text to print."""
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
    if not session.has_facts():
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
    return f"已载入自选股 {len(book)} 只、候选池 {len(pool)} 只并完成计算。"


def _load_alerts(session: ChatSession) -> str:
    from datetime import date

    from ashare_decision_support.state import AlertState

    fired = AlertState.load(date.today()).fired_summary()
    if not fired:
        return "今天还没有触发任何告警。"
    session.set_facts("alerts", {"date": str(date.today()), "fired": fired})
    lines = [f"今日已触发 {len(fired)} 条："]
    lines.extend(f"  {row['at'][-8:]}  {row['symbol']}  {row['trigger']}" for row in fired[:15])
    return "\n".join(lines)


def _load_journal(session: ChatSession, symbol: str | None) -> str:
    from ashare_decision_support.journal import Journal, summarize

    journal = Journal()
    if symbol:
        rows = journal.history_for(symbol)
        if not rows:
            return f"台账里没有 {symbol} 的记录。"
        session.set_facts("journal", {"symbol": symbol, "rows": rows})
        return f"已载入 {symbol} 的 {len(rows)} 条台账记录，用 /show 查看明细。"

    stats = summarize(journal.outcomes())
    session.set_facts("journal", stats)
    if not stats["samples"]:
        return "台账里还没有已兑现的记录。跑几天 `morning` 之后 `review` 会自动结算。"
    overall = stats["overall"]
    return (
        f"已兑现 {stats['samples']} 条"
        f"（距上模型门槛还差 {stats['model_ready_gap']} 条）；"
        f"先到目标 {_rate(overall['target_first_rate'])}、"
        f"先到止损 {_rate(overall['stop_first_rate'])}。用 /show 查看分组明细。"
    )


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"
