from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from ashare_decision_support import cli
from ashare_decision_support.cli import build_parser
from ashare_decision_support.mcp_server import handle
from ashare_decision_support.research.leaders import build_sector_leaders
from ashare_decision_support.research.monthly import build_monthly_context
from ashare_decision_support.sectors.base import SectorDefinition


def _bars(drift: float, amount: float, count: int = 80) -> list[dict]:
    price = 10.0
    rows = []
    start = date(2026, 4, 1)
    for index in range(count):
        price *= 1.0 + drift
        rows.append(
            {
                "timestamp": datetime.combine(start + timedelta(days=index), datetime.min.time()),
                "open": price * 0.99,
                "high": price * 1.01,
                "low": price * 0.98,
                "close": price,
                "volume": 1_000_000 + index * 1000,
                "amount": amount,
            }
        )
    return rows


def test_sector_leaders_are_relative_facts_not_return_predictions() -> None:
    symbols = ("600001.SH", "600002.SH", "600003.SH")
    payload = build_sector_leaders(
        {
            symbols[0]: _bars(0.012, 8e8),
            symbols[1]: _bars(0.004, 4e8),
            symbols[2]: _bars(-0.003, 1e8),
        },
        [SectorDefinition(name="测试行业", level="THS_INDUSTRY", symbols=symbols)],
        names={symbols[0]: "强势股", symbols[1]: "中间股", symbols[2]: "弱势股"},
        as_of="2026-08-10",
        top_per_sector=2,
    )

    leaders = payload["sectors"][0]["leaders"]
    assert [item["symbol"] for item in leaders] == [symbols[0], symbols[1]]
    assert payload["contract"]["not_business_leader"] is True
    assert payload["methodology"]["warning"].endswith("not a probability")


def test_equal_component_values_receive_equal_percentiles() -> None:
    symbols = ("600001.SH", "600002.SH")
    rows = _bars(0.004, 4e8)
    payload = build_sector_leaders(
        dict.fromkeys(symbols, rows),
        [SectorDefinition(name="并列行业", level="THS_INDUSTRY", symbols=symbols)],
    )
    components = [item["rank_components"] for item in payload["sectors"][0]["leaders"]]
    assert components[0] == components[1]


def test_monthly_context_delegates_external_event_research(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))
    leaders = {
        "schema": "ashare-sector-leaders-v1",
        "as_of": "2026-09-15",
        "sectors": [],
    }
    (tmp_path / "sector_leaders_latest.json").write_text(json.dumps(leaders), encoding="utf-8")

    payload = build_monthly_context(
        output_dir=tmp_path,
        moment=datetime(2026, 9, 15, 8, 30),
        horizon_days=31,
    )

    assert payload["contract"]["external_ai_role"].startswith("collect current external events")
    assert any(item["name"] == "国庆节" for item in payload["event_window"])
    assert payload["sector_leaders"]["schema"] == "ashare-sector-leaders-v1"
    assert payload["sector_leaders"]["view"] == "summary"
    assert "recent_validation" in payload["historical_validation"]
    assert "industry_level" in payload["sector_rotation_memory"]
    assert any(
        task["topic"] == "business leadership verification"
        for task in payload["external_research_tasks"]
    )


def test_mcp_lists_read_only_and_refresh_tools() -> None:
    initialized = handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    listed = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert initialized["result"]["serverInfo"]["name"] == "ashare-decision-support"
    tools = {item["name"]: item for item in listed["result"]["tools"]}
    assert tools["get_facts"]["annotations"]["readOnlyHint"] is True
    assert tools["daily_sector_analysis"]["annotations"]["readOnlyHint"] is True
    assert tools["preopen_research_context"]["annotations"]["readOnlyHint"] is True
    assert tools["refresh_sector_leaders"]["annotations"]["readOnlyHint"] is False


def test_daily_sector_tool_reads_the_cached_analysis(tmp_path) -> None:
    from ashare_decision_support.tool_api import daily_sector_analysis

    (tmp_path / "morning_latest.json").write_text(
        json.dumps({"trade_date": "2026-08-26", "sectors": []}), encoding="utf-8"
    )

    payload = daily_sector_analysis(output_dir=str(tmp_path))

    assert payload["schema"] == "ashare-sector-analysis-v1"
    assert payload["trade_date"] == "2026-08-26"
    assert payload["view"] == "summary"

    full = daily_sector_analysis(output_dir=str(tmp_path), detail="full")
    assert "view" not in full


def test_preopen_tool_can_be_used_without_global_snapshot(tmp_path, monkeypatch) -> None:
    from ashare_decision_support.tool_api import preopen_research_context

    monkeypatch.setattr(
        "ashare_decision_support.tool_api.build_preopen_research",
        lambda **kwargs: {
            "output_dir": kwargs["output_dir"],
            "market_adapter": kwargs["market_adapter"],
        },
    )

    payload = preopen_research_context(output_dir=str(tmp_path), include_global=False)

    assert payload == {"output_dir": str(tmp_path), "market_adapter": None}


def test_mcp_monthly_tool_returns_structured_content(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))
    response = handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "monthly_outlook_context",
                "arguments": {"output_dir": str(tmp_path), "horizon_days": 31},
            },
        }
    )
    assert response["result"]["isError"] is False
    assert response["result"]["structuredContent"]["schema"].startswith("ashare-monthly")


def test_cli_exposes_external_research_commands() -> None:
    leaders = build_parser().parse_args(["sector-leaders", "--top", "5"])
    monthly = build_parser().parse_args(["monthly-context", "--horizon-days", "45"])
    assert leaders.command == "sector-leaders" and leaders.top == 5
    assert monthly.command == "monthly-context" and monthly.horizon_days == 45


def test_sector_leaders_cli_reads_the_cached_pack(tmp_path, capsys) -> None:
    cached = {"schema": "ashare-sector-leaders-v1", "sectors": []}
    (tmp_path / "sector_leaders_latest.json").write_text(json.dumps(cached), encoding="utf-8")
    args = build_parser().parse_args(["sector-leaders", "--output-dir", str(tmp_path)])
    cli.COMMANDS[args.command](args)
    assert json.loads(capsys.readouterr().out)["schema"] == cached["schema"]


def test_monthly_context_cli_calls_the_shared_builder(monkeypatch, capsys) -> None:
    seen = {}

    def fake_builder(**kwargs):
        seen.update(kwargs)
        return {"schema": "ashare-monthly-research-context-v1"}

    monkeypatch.setattr(cli, "build_monthly_context", fake_builder)
    args = build_parser().parse_args(["monthly-context", "--horizon-days", "45"])
    cli.COMMANDS[args.command](args)
    assert seen["horizon_days"] == 45
    assert json.loads(capsys.readouterr().out)["schema"].startswith("ashare-monthly")


def test_every_mcp_tool_input_is_self_describing() -> None:
    """schema 是给**模型**看的：没有字段说明它只能靠参数名猜语义。

    `additionalProperties: false` 同理——拼错的键会一路走到 handler 变成
    TypeError，报成 isError 虽然不假，但不如在 schema 里先说清楚。
    """
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    for tool in listed["result"]["tools"]:
        schema = tool["inputSchema"]
        assert schema["additionalProperties"] is False, tool["name"]
        for name, spec in schema["properties"].items():
            assert spec.get("description"), f"{tool['name']}.{name} 缺字段说明"


def test_get_facts_sections_schema_does_not_advertise_unstable_blocks() -> None:
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {item["name"]: item for item in listed["result"]["tools"]}

    description = tools["get_facts"]["inputSchema"]["properties"]["sections"]["description"]

    assert "最近一次买卖点" not in description
    assert "实际可用块随 phase" in description
    assert "unknown sections" in description


def test_sector_leaders_cli_accepts_mcp_parameter_aliases() -> None:
    args = cli.build_parser().parse_args(
        ["sector-leaders", "--top-per-sector", "4", "--lookback-days", "200"]
    )
    assert args.top == 4 and args.days == 200


def test_the_server_reports_the_package_version() -> None:
    """两处各写一个字面量的话，`initialize` 报的版本和 `pip show` 的会各说各话。"""
    from ashare_decision_support import __version__

    initialized = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert initialized["result"]["serverInfo"]["version"] == __version__


def test_sector_tool_text_is_compact_and_not_duplicate_json() -> None:
    from ashare_decision_support.mcp_server import _text_content

    forecast = {
        "schema": "ashare-sector-forecast-v1",
        "target_session": "2026-08-28",
        "market": {"state": "偏强但快速轮动", "direction": "偏强"},
        "confidence": {"status": "uncalibrated"},
        "forecasts": [
            {"name": "科技成长", "scenario": "延续观察", "score": 80, "members": ["电子"]}
        ],
        "execution_protocol": {"confirm_after": "09:35"},
    }
    backtest = {
        "schema": "sector-price-baseline-backtest-v1",
        "date_range": {"start": "2020-01-01", "end": "2026-08-27"},
        "sector_count": 90,
        "methods": [
            {
                "method": "momentum_5d",
                "label": "5日强势延续",
                "holdout": [
                    {"horizon": 5, "samples": 100, "win_rate": 0.48, "mean_excess": -0.001}
                ],
            }
        ],
    }

    forecast_text = _text_content(forecast)
    backtest_text = _text_content(backtest)

    assert forecast_text.startswith("# 板块情景预测")
    assert "structuredContent" in forecast_text
    assert backtest_text.startswith("# 板块价格基线历史验证")
    assert "48.00%" in backtest_text and "-0.10%" in backtest_text
    assert not forecast_text.lstrip().startswith("{")


def test_an_unknown_argument_comes_back_as_a_tool_error_not_a_dead_server() -> None:
    response = handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_facts", "arguments": {"nope": 1}},
        }
    )
    assert response["result"]["isError"] is True
    assert "TypeError" in response["result"]["content"][0]["text"]


# MCP 传输层


def test_a_datetime_in_the_payload_does_not_kill_the_response(monkeypatch) -> None:
    """CLI 打得出来的 payload，MCP 这一侧不能死在上面。

    `_dump_json` 一直带 `default=str`（facts 里有 `datetime`，盘中现算的那批就带），
    而 MCP 那一侧曾经是裸 `json.dumps`——抛出的 TypeError **穿过 handle()**，
    于是这一次 tools/call 收到的是一条 `id: null` 的内部错误，客户端等的那个 id
    永远等不到回复：表现是「工具没反应」，不是「工具报错」。
    """
    from ashare_decision_support import mcp_server

    monkeypatch.setitem(
        mcp_server._HANDLERS, "get_facts", lambda **kw: {"as_of": datetime(2026, 8, 11, 9, 30)}
    )
    response = mcp_server.handle(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "get_facts"}}
    )

    assert response["id"] == 7
    assert response["result"]["isError"] is False
    assert "2026-08-11" in response["result"]["content"][0]["text"]


def test_an_unknown_phase_is_refused_instead_of_echoed(tmp_path, monkeypatch) -> None:
    """拼错的枚举值要当场退回。

    以前它一路带下去：产出一份自称 `phase: banana` 的事实包，装配走的是「非盘中」
    那条分支——对着它问「现在该不该买」，答的是一个不存在的时段。
    """
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))
    response = handle(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "get_facts", "arguments": {"phase": "banana"}},
        }
    )

    assert response["result"]["isError"] is True
    text = response["result"]["content"][0]["text"]
    assert "unsupported phase" in text and "off_hours" in text


def test_notifications_get_no_reply_and_ping_gets_an_empty_one() -> None:
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle({"jsonrpc": "2.0", "id": 9, "method": "ping"})["result"] == {}


def test_an_unknown_method_is_a_protocol_error_not_a_tool_error() -> None:
    response = handle({"jsonrpc": "2.0", "id": 10, "method": "resources/list"})
    assert response["error"]["code"] == -32601


def test_the_stdio_loop_answers_line_by_line_and_survives_garbage() -> None:
    """整条链路上唯一不经过 `handle()` 的部分，也是失败最不显眼的部分：
    它一坏，客户端看到的是「服务器没反应」，而全部单测照常绿。"""
    import io

    from ashare_decision_support import mcp_server

    lines = [
        '{"jsonrpc":"2.0","id":1,"method":"ping"}',
        "",  # 空行跳过，不产生回复
        "{ not json",  # 坏行只回一条 parse error
        '{"jsonrpc":"2.0","method":"notifications/initialized"}',
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
    ]
    stdin = io.StringIO("".join(f"{line}\n" for line in lines))
    stdout = io.StringIO()

    mcp_server.main(stdin=stdin, stdout=stdout)

    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [item.get("id") for item in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700
    assert len(replies[2]["result"]["tools"]) == len(mcp_server._TOOLS)


# 体积：这是给模型调的工具，每一份多余的拷贝都是它的上下文


def test_the_text_content_is_the_readable_view_not_a_second_json_dump(monkeypatch) -> None:
    """实测一次 `get_facts` 的 payload 是 222KB。原样再 dump 一遍塞进 `content`，
    一次调用就是 445KB，而两份说的是同一件事。"""
    from ashare_decision_support import mcp_server

    monkeypatch.setitem(
        mcp_server._HANDLERS,
        "get_facts",
        lambda **kw: {"facts": {"盘前主链路": "x" * 500}, "rendered": "【非交易时段】读数摘要"},
    )
    response = mcp_server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_facts"}}
    )

    result = response["result"]
    assert result["content"][0]["text"] == "【非交易时段】读数摘要"
    # 机器要的那份没少。
    assert result["structuredContent"]["facts"]["盘前主链路"].startswith("x")


def test_monthly_mcp_text_is_a_summary_not_a_second_payload(monkeypatch) -> None:
    from ashare_decision_support.mcp_server import _text_content

    monkeypatch.setattr(
        "ashare_decision_support.mcp_server.monthly_outlook_context",
        lambda **kwargs: {},
    )
    payload = {
        "schema": "ashare-monthly-research-context-v1",
        "as_of": "2026-08-28T08:00:00+08:00",
        "horizon": {"days": 31, "end_date": "2026-09-28"},
        "a_share_facts": {"rendered": "事实摘要"},
        "sector_leaders": None,
        "notes": ["龙头缓存缺失"],
        "large_matrix": list(range(1000)),
    }

    text = _text_content(payload)

    assert "事实摘要" in text
    assert "龙头缓存缺失" in text
    assert "large_matrix" not in text


def test_a_payload_without_a_readable_view_still_gets_json_content(monkeypatch) -> None:
    """龙头包与月度包没有 `rendered`，那两个仍然回落到 JSON。"""
    from ashare_decision_support import mcp_server

    monkeypatch.setitem(mcp_server._HANDLERS, "get_facts", lambda **kw: {"schema": "x-v1"})
    response = mcp_server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_facts"}}
    )
    assert json.loads(response["result"]["content"][0]["text"]) == {"schema": "x-v1"}


def test_sections_returns_only_what_was_asked_and_says_what_it_dropped(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))
    from ashare_decision_support.tool_api import get_facts

    full = get_facts(output_dir=str(tmp_path), phase="off_hours")
    assert len(full["facts"]) > 1

    picked = get_facts(output_dir=str(tmp_path), phase="off_hours", sections=["持仓"])
    assert "持仓" in picked["facts"] and len(picked["facts"]) < len(full["facts"])
    # 少给数据可以，让人以为「就这些」不行。
    assert any("省略的块" in note for note in picked["notes"])


def test_an_unknown_section_is_refused_instead_of_returning_an_empty_pack(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path / "state"))
    from ashare_decision_support.tool_api import get_facts

    with pytest.raises(ValueError, match="unknown sections"):
        get_facts(output_dir=str(tmp_path), phase="off_hours", sections=["盘前主链路x"])


def test_the_screen_block_is_trimmed_with_the_drop_written_down(tmp_path) -> None:
    """静默截断读起来和「全都在这里」一模一样——那正是这套系统各处都在防的事。"""
    from ashare_decision_support.ask.context import (
        SCREEN_KEEP_PER_PROFILE,
        SCREEN_KEEP_SHORTLIST,
        assemble,
    )

    rows = [{"symbol": f"60000{index}.SH", "setup_rank": index} for index in range(40)]
    (tmp_path / "strategy_screen_latest.json").write_text(
        json.dumps(
            {
                "schema": "ashare-strategy-screen-v1",
                "profiles": {"high-elasticity": {"summary": {"kept": 40}, "candidates": rows}},
                "diversified_shortlist": rows,
            }
        ),
        encoding="utf-8",
    )

    ctx = assemble(datetime(2026, 8, 8, 20, 0), output_dir=tmp_path, is_trading_day=False)
    block = ctx.blocks["参数化策略扫描"]

    assert len(block["profiles"]["high-elasticity"]["candidates"]) == SCREEN_KEEP_PER_PROFILE
    assert len(block["diversified_shortlist"]) == SCREEN_KEEP_SHORTLIST
    # 漏斗留着——它回答的是「为什么只剩这么几只」，比多留几行候选重要。
    assert block["profiles"]["high-elasticity"]["summary"] == {"kept": 40}
    assert any("共省略" in note for note in ctx.notes)


# 声明与实现对不对得上


def test_every_declared_tool_has_a_handler_and_every_handler_is_declared() -> None:
    """`_TOOLS` 是给模型看的目录，`_HANDLERS` 是真正能调的东西。

    两张表各写一份，漂了不会有任何告警：多出来的那条工具在目录里看得见、调用时
    返回 `unknown tool`；少写的那条则永远不会被发现。
    """
    from ashare_decision_support import mcp_server

    assert {item["name"] for item in mcp_server._TOOLS} == set(mcp_server._HANDLERS)


def test_declared_parameters_match_the_function_signatures() -> None:
    """schema 里的参数必须真的存在于 handler 签名里。

    同一类漂移的第三处（前两处是文档里的命令、文档里的链接）：schema 加了个
    `sections` 而函数没有，模型照着 schema 传，换来的是一条 TypeError；函数加了
    参数而 schema 没写，那个能力谁也不知道。
    """
    import inspect

    from ashare_decision_support import mcp_server

    for tool in mcp_server._TOOLS:
        handler = mcp_server._HANDLERS[tool["name"]]
        accepted = set(inspect.signature(handler).parameters)
        declared = set(tool["inputSchema"]["properties"])
        assert declared <= accepted, f"{tool['name']} 的 schema 多声明了 {declared - accepted}"
        assert accepted <= declared, f"{tool['name']} 的 schema 漏了 {accepted - declared}"
