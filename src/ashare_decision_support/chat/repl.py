"""Interactive loop for continuous analysis.

Deliberately a plain stdin REPL rather than a server: the state that matters
(the fact bundle) lives in one `ChatSession`, and a single-user tool does not
need a transport in front of it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from ashare_decision_support.chat.session import (
    HELP_TEXT,
    ChatResponder,
    ChatSession,
    handle_command,
    render_facts_summary,
)

BANNER = """A 股超短线助理 · 连续分析（趋势 / 股价 / 监控 / 买卖点）
系统不下单、不接管账户；默认只输出事实包，显式 `--llm` 才会走模型回答。
输入 /help 看命令，直接提问即可对话，/quit 退出。"""

PROMPT = "\n> "


def run_chat(
    compute_symbols: Callable[[list[str]], dict],
    compute_sectors: Callable[[], dict],
    symbols: list[str] | None = None,
    responder: ChatResponder | None = None,
    read_line: Callable[[str], str] = input,
    write: Callable[[str], Any] = print,
    trade_date: date | None = None,
) -> ChatSession:
    """Run until the user quits or stdin closes. Returns the final session."""
    session = ChatSession(trade_date=trade_date)
    answerer = responder or ChatResponder()

    write(BANNER)
    if symbols:
        session.add_symbols(symbols)
        write(f"\n正在装配 {len(session.symbols)} 只标的的上下文 ...")
        try:
            session.set_facts("advice", compute_symbols(list(session.symbols)))
        except Exception as exc:
            write(f"初始取数失败（{type(exc).__name__}）：{str(exc)[:200]}；可用 /refresh 重试。")
        else:
            write("已装配上下文，可以直接问趋势、买卖点、监控结果或如果跌破怎么办。")
            write(render_facts_summary(session))
    else:
        write("\n上下文为空，先用 /add <代码> 加入标的，或 /positions 载入持仓。")

    while True:
        try:
            line = read_line(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            write("\n退出。")
            return session

        if not line:
            continue
        if line.lower() in {"/quit", "/exit", "/q"}:
            write("退出。")
            return session

        if line.startswith("/"):
            try:
                write(handle_command(session, line, compute_symbols, compute_sectors))
            except Exception as exc:
                # A failed fetch must not end the session; the user can retry.
                write(f"命令执行失败（{type(exc).__name__}）：{str(exc)[:200]}")
            continue

        answer, notes = answerer.answer(session, line)
        write(answer)
        for note in notes:
            write(f"  [{note}]")


__all__ = ["BANNER", "HELP_TEXT", "run_chat"]
