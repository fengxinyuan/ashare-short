"""每条 CLI 命令真的能调起它那条主链路吗。

**为什么单独测这个。** `tests/test_daily_cycle_wiring.py` 已经为 daemon 那一侧
立过这条标准：`daily_cycle` 和 `cli` 各有一组调用点，签名一旦漂移，没被测的那
一侧要到真正跑的时候才炸。当时只补了 daemon 一侧。

CLI 这一侧是同一个洞，而且大 20 倍——21 个 `_cmd_*` 里此前只有
`_cmd_strategy_screen` 有测试引用，`cli.py` 覆盖率 66%，未覆盖的那 188 行正好
是这批 handler。它们不是薄薄一层转发：`_cmd_morning` 往 `MorningConfig` 传 14
个关键字、往 `Sources` 传 5 个，`_cmd_monitor` 往 `run_monitor` 传 11 个。而测
试全都自己构造 `MorningConfig`，于是**改一个字段名，整套测试照绿，
`ashare-ds morning` 当场炸**。

下面的用例不测业务结果，只测接线：命令行参数能不能解析成 handler 要的形状、
传过去的实参在**真实签名**下是否合法、以及 `COMMANDS` 表里有没有命令没人管。
"""

from __future__ import annotations

import inspect
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ashare_decision_support import cli

# argv 里的这个占位符会被换成 tmp_path。默认的 `--output-dir briefs` 是**相对
# 当前目录**的，跑测试时会在仓库根上长出一个 briefs/。
OUT = "{out}"


class _Bundle:
    """`build_facts_bundle` 的返回形状：handler 会读这三样。"""

    rendered = "（事实包正文）"
    notes: list[str] = []

    def to_dict(self) -> dict:
        return {"schema": "ashare-facts-v1"}


# 命令 → (argv, 应该被调起的符号, 替身的返回值)
#
# 返回值不是随便填的：handler 拿到结果之后还要用它（`_trim` 要 dict、
# `_cmd_ask` 要读 `notes` 和 `mode`、`_cmd_morning --explain` 要读 `funnel`），
# 形状不对的话失败会指向替身而不是接线。
CASES: dict[str, tuple[list[str], str, Any]] = {
    "daemon": (["daemon", "--dry-run", "--quiet", "--output-dir", OUT], "run_daily_cycle", {}),
    "morning": (["morning", "--quiet", "--output-dir", OUT], "run_morning", {"funnel": {}}),
    "monitor": (["monitor", "--once", "--quiet", "--output-dir", OUT], "run_monitor", {}),
    "advise": (["advise", "--quiet", "--symbols", "600519.SH"], "run_advise", {"facts": {}}),
    "ask": (["ask", "--no-live", "今天板块怎么排"], "run_ask", {"notes": [], "mode": "facts"}),
    "facts": (["facts", "--no-live"], "build_facts_bundle", _Bundle()),
    "preopen-research": (["preopen-research", "--no-global"], "build_facts_bundle", _Bundle()),
    "chat": (["chat"], "run_chat", None),
    "review": (["review", "--quiet", "--output-dir", OUT], "run_review", {}),
    "post-close-brief": (
        ["post-close-brief", "--symbols", "600519.SH", "--output-dir", OUT],
        "run_post_close",
        {},
    ),
    "pre-open-brief": (["pre-open-brief", "--output-dir", OUT], "run_pre_open", {}),
}

# 不调主链路的命令：只读状态目录或只做诊断。它们在下面另有用例，或者需要外网。
STATE_ONLY = {"watchlist", "journal", "state", "qmt-health"}
NEEDS_NETWORK = {"tick-health", "intraday-check", "wyckoff-scan"}
# 单独写了用例（要打桩适配器，塞不进上面那张表驱动的形状）
OWN_CASES = {"calendar-refresh"}
COVERED_ELSEWHERE = {
    # tests/test_ask.py::_cmd_strategy_screen
    "strategy-screen",
    # tests/test_external_adjudication.py 走的是同一对函数的完整用例
    "codex-adjudication-packet",
    "codex-adjudication-validate",
}


def _bind_recorder(real: Any, sink: dict, result: Any):
    """替身：按**真实签名**绑定实参，不合法就抛 TypeError。

    手法与 `test_daily_cycle_wiring.py` 相同，理由也相同——只记参数不校验的话，
    handler 传一个已经被改名的关键字，替身照收不误。
    """
    signature = inspect.signature(real)

    def recorder(*args, **kwargs):
        signature.bind(*args, **kwargs)  # 不合法立即 TypeError
        sink["args"], sink["kwargs"] = args, kwargs
        return result

    return recorder


@pytest.mark.parametrize("command", sorted(CASES))
def test_every_cli_command_calls_its_chain_with_a_valid_signature(
    command, monkeypatch, tmp_path, capsys
) -> None:
    argv, target, result = CASES[command]
    sink: dict[str, Any] = {}
    monkeypatch.setattr(cli, target, _bind_recorder(getattr(cli, target), sink, result))

    args = cli.build_parser().parse_args(
        [item.replace(OUT, str(tmp_path / "briefs")) for item in argv]
    )
    assert args.command == command

    cli.COMMANDS[command](args)

    assert sink, f"{command} 没有调起 {target}"
    capsys.readouterr()


def test_handlers_pass_keywords_that_the_chain_actually_declares(monkeypatch, tmp_path) -> None:
    """逐个关键字对一遍真实签名的参数名。

    上面那条用 `signature.bind` 已经能抓住改名和必填项变动。这一条抓的是另一半：
    `bind` 对 `**kwargs` 形式的签名是**放行**的，而主链路里有几条正好收了兜底
    参数。真名单比“能绑上”严格。
    """
    argv, target, result = CASES["morning"]
    sink: dict[str, Any] = {}
    # 真实签名要在打补丁**之前**取——补丁之后 `cli.run_morning` 就是替身了，
    # 替身是 `(*args, **kwargs)`，读它等于什么都没对。
    declared = set(inspect.signature(cli.run_morning).parameters)
    monkeypatch.setattr(cli, target, _bind_recorder(getattr(cli, target), sink, result))

    args = cli.build_parser().parse_args(
        [item.replace(OUT, str(tmp_path / "briefs")) for item in argv]
    )
    cli.COMMANDS["morning"](args)

    assert set(sink["kwargs"]) <= declared

    # `MorningConfig` 是 `_cmd_morning` 里第二处逐字段构造的地方，字段改名同样
    # 只会在真实运行时才炸。它是 dataclass，构造本身就是那道闸门——这里确认
    # handler 真的把它构造出来了，而不是传了个 None 过去。
    config = sink["kwargs"]["config"]
    assert isinstance(config, cli.MorningConfig)
    assert config.sector_level == "SW1"


def test_state_only_commands_run_against_the_isolated_state_home(capsys) -> None:
    """只读状态的命令要能在空状态下跑完并给出合法 JSON。

    空自选股、空台账是**第一次使用**时的正常状态，不是异常。这几条命令在那种
    状态下崩掉的话，新用户遇到的第一个反馈就是一段栈。
    """
    for argv in (["watchlist", "list"], ["journal"], ["state"], ["qmt-health"]):
        args = cli.build_parser().parse_args(argv)
        cli.COMMANDS[args.command](args)
        printed = capsys.readouterr().out
        assert json.loads(printed) is not None, f"{argv} 没有输出合法 JSON"


def test_codex_adjudication_round_trip_reads_and_writes_files(tmp_path, capsys) -> None:
    """这两条命令的入参是**文件路径**，不是数据结构。

    它们是 Codex 工作流的两端，平时只在命令行上跑；路径读写这一层没有别的调用方
    覆盖，写坏了要到手工跑那一步才知道。
    """
    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps({"facts": {"symbols": []}}), encoding="utf-8")
    packet_path = tmp_path / "packet.json"

    args = cli.build_parser().parse_args(
        ["codex-adjudication-packet", "--facts", str(facts_path), "--output", str(packet_path)]
    )
    cli.COMMANDS[args.command](args)
    capsys.readouterr()

    assert json.loads(packet_path.read_text(encoding="utf-8")) is not None

    decision_path = tmp_path / "decision.json"
    decision_path.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    args = cli.build_parser().parse_args(
        [
            "codex-adjudication-validate",
            "--packet",
            str(packet_path),
            "--decision",
            str(decision_path),
        ]
    )
    cli.COMMANDS[args.command](args)
    assert json.loads(capsys.readouterr().out) is not None


def test_calendar_refresh_writes_the_holiday_table(monkeypatch, capsys) -> None:
    """`TradingCalendar.refresh_from` 此前没有任何入口。

    它是唯一能写 `holidays.json` 的代码，而没有命令调它，于是那张表永远是空的、
    判定退化成「周末以外都算交易日」。这不是静默错误（`explain()` 会说「未经假期
    校验」），但除了手工编辑之外没有补上的办法。
    """
    class _Adapter:
        def get_trading_calendar(self):
            # 只给 1 月：1/1 是假日，1/3(周六) 调休上班。
            return ["2026-01-02", "2026-01-03", "2026-01-05"]

    monkeypatch.setattr(cli, "AkshareAdapter", lambda: _Adapter())
    args = cli.build_parser().parse_args(["calendar-refresh", "--year", "2026"])
    cli.COMMANDS[args.command](args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["trading_days"] == 3
    calendar = cli.TradingCalendar()
    assert calendar.is_trading_day(date(2026, 1, 1)) is False      # 元旦
    assert calendar.is_trading_day(date(2026, 1, 3)) is True       # 周六调休
    assert calendar.is_trading_day(date(2026, 1, 2)) is True


def test_calendar_refresh_keeps_the_old_table_when_the_feed_has_none(monkeypatch, capsys) -> None:
    """免费档给不出日历时**保持原状**，不能把已有的表清空。

    半张表比没有表更危险：`explain()` 会因为 `configured` 为真而不再提示「未经
    假期校验」，于是一张空表冒充成校验过的。
    """
    class _Adapter:
        def get_trading_calendar(self):
            return []

    cli.TradingCalendar().save(["2026-10-01"], ["2026-10-10"])
    monkeypatch.setattr(cli, "AkshareAdapter", lambda: _Adapter())
    args = cli.build_parser().parse_args(["calendar-refresh", "--year", "2026"])
    cli.COMMANDS[args.command](args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["trading_days"] == 0
    assert "保持原状" in payload["note"]
    assert cli.TradingCalendar().is_trading_day(date(2026, 10, 1)) is False


def test_no_command_is_left_without_an_owner() -> None:
    """`COMMANDS` 表里每一条都得有归属。

    这条不测行为，测的是**这个文件本身别再退化**。加一条新命令而不加用例时，
    它会红——否则「CLI 有测试了」会重新变成一句只在今天成立的话。
    """
    accounted = set(CASES) | STATE_ONLY | NEEDS_NETWORK | COVERED_ELSEWHERE | OWN_CASES
    assert set(cli.COMMANDS) == accounted


def test_output_dir_default_is_relative_so_tests_must_redirect_it() -> None:
    """上面每条用例都把 `--output-dir` 指到 tmp 下，这条说明为什么。

    默认值是相对路径 `briefs`，真跑的时候相对的是**当前工作目录**。用例忘了改
    的话，跑一次测试就在仓库根上长出一个 briefs/，而且没有任何断言会红。
    """
    args = cli.build_parser().parse_args(["morning"])
    assert not Path(args.output_dir).is_absolute()
