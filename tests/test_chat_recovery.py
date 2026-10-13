"""用户实际聊天流程：代码归一化、取数失败后的重试和启动恢复。"""

from copy import deepcopy

import pytest

from ashare_decision_support.chat.repl import run_chat
from ashare_decision_support.chat.session import ChatSession, handle_command


def _facts(symbols):
    return {"symbols": [{"symbol": symbol, "last_price": 100} for symbol in symbols]}


def test_add_aliases_once_and_drop_their_canonical_fact():
    session = ChatSession()
    calls = []

    def compute(symbols):
        calls.append(list(symbols))
        return _facts(symbols)

    handle_command(session, "/add 600519 sh600519 600519.SH 000001.SZ", compute, dict)
    assert calls == [["600519.SH", "000001.SZ"]]
    handle_command(session, "/drop sh600519", compute, dict)
    assert session.symbols == ["000001.SZ"]
    assert session.facts["advice"]["symbols"] == _facts(["000001.SZ"])["symbols"]
    assert calls == [["600519.SH", "000001.SZ"]]


def test_failed_add_preserves_the_context_and_the_same_command_can_retry():
    session = ChatSession()
    session.add_symbols(["600519.SH"])
    session.set_facts("advice", _facts(session.symbols))
    before = deepcopy(session.facts)

    def down(symbols):
        raise OSError("行情暂时不可用")

    with pytest.raises(OSError, match="行情暂时不可用"):
        handle_command(session, "/add 000001.SZ", down, dict)
    assert session.symbols == ["600519.SH"]
    assert session.facts == before
    handle_command(session, "/add 000001.SZ", _facts, dict)
    assert session.symbols == ["600519.SH", "000001.SZ"]
    assert session.facts["advice"] == _facts(session.symbols)


def test_startup_failure_keeps_the_repl_available_for_refresh():
    attempts = []
    commands = iter(["/refresh", "/quit"])
    output = []

    def compute(symbols):
        attempts.append(list(symbols))
        if len(attempts) == 1:
            raise OSError("行情暂时不可用")
        return _facts(symbols)

    session = run_chat(
        compute,
        dict,
        symbols=["600519"],
        read_line=lambda prompt: next(commands),
        write=output.append,
    )
    assert attempts == [["600519.SH"], ["600519.SH"]]
    assert any("失败" in line for line in output)
    assert session.facts["advice"] == _facts(["600519.SH"])
