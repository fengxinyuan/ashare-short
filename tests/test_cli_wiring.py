"""验证 CLI 参数解析、真实任务签名与命令分派。

各命令的依赖和返回值均使用测试替身，避免接线测试访问真实数据源。"""

from __future__ import annotations

import inspect
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from ashare_decision_support import cli
from ashare_decision_support.journal import Journal, Recommendation
from ashare_decision_support.schedule.timezone import beijing_today

# argv 里的这个占位符会被换成 tmp_path。默认的 `--output-dir briefs` 是**相对
# 当前目录**的，跑测试时会在仓库根上长出一个 briefs/。
OUT = "{out}"


class _Bundle:
    """`build_facts_bundle` 的返回形状：handler 会读这三样。"""

    rendered = "（事实包正文）"
    notes: list[str] = []

    def to_dict(self, sections: list[str] | None = None) -> dict:
        # 签名跟着真实的 `FactsBundle` 走：`facts --sections` 是从这里传下去的，
        # 替身少一个参数时这条用例正是唯一会红的地方。
        return {"schema": "ashare-facts-v1", "sections": sections}


# 命令 → (argv, 应该被调起的符号, 替身的返回值)

# 返回值不是随便填的：handler 拿到结果之后还要用它（`_trim` 要 dict、
# `_cmd_ask` 要读 `notes` 和 `mode`、`_cmd_morning --explain` 要读 `funnel`），
# 形状不对的话失败会指向替身而不是接线。
CASES: dict[str, tuple[list[str], str, Any]] = {
    "daemon": (["daemon", "--dry-run", "--quiet", "--output-dir", OUT], "run_daily_cycle", {}),
    "morning": (["morning", "--quiet", "--output-dir", OUT], "run_morning", {"funnel": {}}),
    "sector-analysis": (
        ["sector-analysis", "--format", "json", "--output-dir", OUT],
        "load_daily_sector_analysis",
        {"trade_date": "2026-08-26"},
    ),
    "monitor": (["monitor", "--once", "--quiet", "--output-dir", OUT], "run_monitor", {}),
    "advise": (["advise", "--quiet", "--symbols", "600519.SH"], "run_advise", {"facts": {}}),
    "ask": (["ask", "--no-live", "今天板块怎么排"], "run_ask", {"notes": [], "mode": "facts"}),
    "facts": (["facts", "--no-live"], "build_facts_bundle", _Bundle()),
    "preopen-research": (
        ["preopen-research", "--no-global"],
        # 盘前研报组包由 `research/preopen.py` 负责，
        # 所以这里盯的是 `cli` 真正调起的那个入口，而不是它内部再往下调的
        # `build_facts_bundle`——盯里面那层的话，命令改成调别的组包函数也不会红。
        "build_preopen_research",
        {"as_of": "2026-08-15T09:00:00", "facts": {}, "overseas_snapshot": None},
    ),
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
STATE_ONLY = {"position", "journal", "state"}
NEEDS_NETWORK = {"tick-health", "intraday-check"}
# 单独写了用例（要打桩适配器，塞不进上面那张表驱动的形状）
OWN_CASES = {"calendar-refresh", "history-refresh"}
COVERED_ELSEWHERE = {
    # tests/test_sector_backtest.py 覆盖纯计算；此命令只做本地序列读取与 JSON 输出
    "sector-backtest",
    # tests/test_sector_forecast.py 覆盖组包；命令只做缓存读取与 JSON 输出
    "sector-forecast",
    # tests/test_sector_confirmation.py 覆盖纯计算；命令只读当天盘中序列
    "sector-confirmation",
    # tests/test_ask.py::_cmd_strategy_screen
    "strategy-screen",
    # tests/test_external_tools.py 覆盖纯计算、MCP 调用与两条命令的解析
    "sector-leaders",
    "monthly-context",
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


@pytest.mark.parametrize("kind", ["json", "text"])
def test_cli_export_failure_preserves_previous_file_and_can_retry(
    kind, tmp_path, monkeypatch, capsys
) -> None:
    from ashare_decision_support.state import store

    target = tmp_path / "exports" / "report"
    target.parent.mkdir()
    target.write_text("previous report\n", encoding="utf-8")
    export = cli._dump_json if kind == "json" else cli._write_text
    payload = {"title": "当前事实"} if kind == "json" else "当前事实"

    def fail_replace(source, destination):
        raise OSError("replacement failed")

    with monkeypatch.context() as patch:
        patch.setattr(store.os, "replace", fail_replace)
        with pytest.raises(OSError, match="replacement failed"):
            export(payload, str(target))

    assert target.read_text(encoding="utf-8") == "previous report\n"
    assert list(target.parent.iterdir()) == [target]
    assert capsys.readouterr().out == ""

    export(payload, str(target))
    content = target.read_text(encoding="utf-8")
    assert (json.loads(content) if kind == "json" else content.rstrip("\n")) == payload


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
    assert sink["args"][0].industry_taxonomy.name == "ths-industry"
    sources = sink["args"][0]
    assert sources.industry_taxonomy.source is sources.concept_taxonomy.source


@pytest.mark.parametrize(
    "argv",
    [["morning", "--level", "SW1"], ["sector-leaders", "--industry-level", "SW2"]],
)
def test_retired_industry_hierarchy_flags_are_rejected(argv):
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(argv)
    assert error.value.code == 2


@pytest.mark.parametrize("news_flags", [[], ["--no-news"]])
def test_monitor_cli_passes_output_dir_and_news_choice_to_the_intraday_job(
    monkeypatch, tmp_path, news_flags
) -> None:
    sink: dict[str, Any] = {}
    monkeypatch.setattr(
        cli,
        "run_monitor",
        _bind_recorder(cli.run_monitor, sink, {}),
    )
    out = tmp_path / "briefs"
    args = cli.build_parser().parse_args(
        [
            "monitor",
            "--once",
            "--quiet",
            "--output-dir",
            str(out),
            *news_flags,
        ]
    )

    cli.COMMANDS["monitor"](args)

    assert sink["kwargs"]["output_dir"] == str(out)
    assert sink["kwargs"]["news_cache_dir"] == str(out)
    assert sink["kwargs"]["use_news"] is (not news_flags)


def test_tickflow_adapter_banner_does_not_pollute_stdout(monkeypatch, capsys) -> None:
    class NoisyAdapter:
        def __init__(self, **kwargs):
            print("third-party banner")
            self.kwargs = kwargs

    monkeypatch.setattr(cli, "TickFlowAdapter", NoisyAdapter)

    adapter = cli._tickflow(cli.build_parser().parse_args(["state"]))

    assert isinstance(adapter, NoisyAdapter)
    assert capsys.readouterr().out == ""


def test_explain_prints_the_control_sample_its_help_text_promises(
    monkeypatch, tmp_path, capsys
) -> None:
    """`--explain` 的帮助承诺了「对照样本」，屏幕上就得有。

    `control_sample` 一直被算出来、也一直随事实包落盘，但**从没进过 `--explain`
    的输出**——四个标签的那张表里没有它。漏掉的偏偏是最不能漏的一档：入选组
    自己不构成证据，`review` 的 edge 是拿它跟对照组比出来的，对照组塌成 0 的
    时候整张兑现表都不成立，而那正是该在盘前就看见的一次。

    断言落在**那两行打印**上，不是「数字出现过」——`_dump_json` 会把整份
    summary 打出来，funnel 的数字在 JSON 里本来就有，按数字断言的话把这两行
    删掉测试照样绿。
    """
    funnel = {
        "universe": 5550,
        "selected_sector_constituents": 812,
        "screened_constituents": 320,
        "final_candidates": 15,
        "control_sample": 12,
    }
    monkeypatch.setattr(cli, "run_morning", lambda *args, **kwargs: {"funnel": funnel})

    args = cli.build_parser().parse_args(
        ["morning", "--quiet", "--explain", "--output-dir", str(tmp_path / "briefs")]
    )
    cli.COMMANDS["morning"](args)

    lines = capsys.readouterr().out.splitlines()
    funnel_line = next(line for line in lines if line.startswith("筛选漏斗："))
    for count in ("5550", "812", "320", "15"):
        assert count in funnel_line

    control_line = next(
        (line for line in lines if line.startswith("对照样本：")),
        None,
    )
    assert control_line is not None, "帮助里写了对照样本，`--explain` 却没打印它"
    assert "12" in control_line


def test_text_output_writes_nested_path_without_printing_report(
    tmp_path, capsys, monkeypatch
) -> None:
    target = tmp_path / "nested" / "sector.md"
    payload = {"trade_date": "2026-08-26", "market": {}, "sectors": []}
    monkeypatch.setattr(cli, "load_daily_sector_analysis", lambda output_dir: payload)
    args = cli.build_parser().parse_args(
        [
            "sector-analysis",
            "--format",
            "text",
            "--output",
            str(target),
            "--output-dir",
            str(tmp_path),
        ]
    )
    cli.COMMANDS[args.command](args)

    assert target.read_text(encoding="utf-8").startswith("# 每日板块分析 2026-08-26")
    assert capsys.readouterr().out == ""


def test_state_only_commands_run_against_the_isolated_state_home(capsys) -> None:
    """只读状态的命令要能在空状态下跑完并给出合法 JSON。

    空持仓、空台账是**第一次使用**时的正常状态，不是异常。这几条命令在那种
    状态下崩掉的话，新用户遇到的第一个反馈就是一段栈。
    """
    for argv in (["position", "list"], ["journal"], ["state"]):
        args = cli.build_parser().parse_args(argv)
        cli.COMMANDS[args.command](args)
        printed = capsys.readouterr().out
        assert json.loads(printed) is not None, f"{argv} 没有输出合法 JSON"


def test_state_journal_counts_explain_control_and_veto_samples() -> None:
    journal = Journal()
    trade_day = str(beijing_today() - timedelta(days=2))
    for kind, symbol in (
        ("recommendation", "600000.SH"),
        ("control", "600001.SH"),
        ("vetoed", "600002.SH"),
    ):
        row = Recommendation(
            symbol=symbol,
            trade_date=trade_day,
            source="morning",
            reference_price=10.0,
        )
        row.kind = kind
        journal.record(row)

    report = cli._state_report()["journal"]

    assert report["recommendations"] == 1
    assert report["controls"] == 1
    assert report["vetoed"] == 1
    assert report["verifiable_total"] == 3
    assert report["pending_verification"] == 3
    assert report["pending_by_kind"] == {
        "recommendation": 1,
        "control": 1,
        "vetoed": 1,
    }
    assert report["model_samples"] == 0


def test_pre_open_missing_payload_has_an_actionable_cli_error(tmp_path) -> None:
    args = cli.build_parser().parse_args(
        ["pre-open-brief", "--payload", str(tmp_path / "missing.json")]
    )

    with pytest.raises(SystemExit, match="先运行 `ashare-ds post-close-brief`"):
        cli.COMMANDS[args.command](args)


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
            # 前一年末到 1/5 有覆盖：1/1 是假日，1/3 是源明确提供的开市日。
            return ["2025-12-31", "2026-01-02", "2026-01-03", "2026-01-05"]

    monkeypatch.setattr(cli, "AkshareCalendarAdapter", lambda: _Adapter())
    args = cli.build_parser().parse_args(["calendar-refresh", "--year", "2026"])
    cli.COMMANDS[args.command](args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["trading_days"] == 3
    assert "覆盖范围" in payload["note"]
    calendar = cli.TradingCalendar()
    assert calendar.is_trading_day(date(2026, 1, 1)) is False  # 元旦
    assert calendar.is_trading_day(date(2026, 1, 3)) is True  # 周六调休
    assert calendar.is_trading_day(date(2026, 1, 2)) is True
    assert calendar.is_trading_day(date(2026, 1, 6)) is True  # 未提供的未来日期不推断休市


def test_calendar_refresh_keeps_the_old_table_when_the_feed_has_none(monkeypatch, capsys) -> None:
    """免费档给不出日历时**保持原状**，不能把已有的表清空。

    半张表比没有表更危险：`explain()` 会因为 `configured` 为真而不再提示「未经
    假期校验」，于是一张空表冒充成校验过的。
    """

    class _Adapter:
        def get_trading_calendar(self):
            return []

    cli.TradingCalendar().save(["2026-10-01"], ["2026-10-10"])
    monkeypatch.setattr(cli, "AkshareCalendarAdapter", lambda: _Adapter())
    args = cli.build_parser().parse_args(["calendar-refresh", "--year", "2026"])
    cli.COMMANDS[args.command](args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["trading_days"] == 0
    assert "保持原状" in payload["note"]
    assert cli.TradingCalendar().is_trading_day(date(2026, 10, 1)) is False


def test_history_refresh_calls_both_history_adapters(monkeypatch, capsys) -> None:
    """历史缓存有明确维护入口，基准率层不再只是写好没人能填样本。"""
    calls: dict[str, Any] = {}

    class _SectorResult:
        notes = ["板块历史已回填"]

        def to_dict(self):
            return {"ok": True, "codes": 2, "notes": self.notes}

    class _OverseasResult:
        notes = ["海外历史已刷新"]

        def to_dict(self):
            return {"ok": True, "keys": ["sox"], "notes": self.notes}

    class _SectorAdapter:
        def backfill(self, *, since_year, until_year):
            calls["sector"] = (since_year, until_year)
            return _SectorResult()

    class _OverseasAdapter:
        def refresh(self, keys):
            calls["overseas"] = tuple(keys)
            return _OverseasResult()

    monkeypatch.setattr(cli, "ThsSectorIndexAdapter", _SectorAdapter)
    monkeypatch.setattr(cli, "OverseasHistoryAdapter", _OverseasAdapter)

    args = cli.build_parser().parse_args(
        ["history-refresh", "--since-year", "2025", "--until-year", "2026", "--keys", "sox"]
    )
    cli.COMMANDS[args.command](args)

    payload = json.loads(capsys.readouterr().out)
    assert calls == {"sector": (2025, 2026), "overseas": ("sox",)}
    assert payload["tasks"] == ["sector-index", "overseas"]
    assert payload["sector_index"]["ok"] is True
    assert payload["overseas"]["keys"] == ["sox"]


def test_history_refresh_can_run_only_overseas(monkeypatch, capsys) -> None:
    class _OverseasResult:
        notes = []

        def to_dict(self):
            return {"ok": True}

    class _OverseasAdapter:
        def refresh(self, keys):
            return _OverseasResult()

    monkeypatch.setattr(
        cli,
        "ThsSectorIndexAdapter",
        lambda: (_ for _ in ()).throw(AssertionError("sector should not run")),
    )
    monkeypatch.setattr(cli, "OverseasHistoryAdapter", _OverseasAdapter)

    args = cli.build_parser().parse_args(["history-refresh", "--only", "overseas"])
    cli.COMMANDS[args.command](args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"] == ["overseas"]
    assert "sector_index" not in payload


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
