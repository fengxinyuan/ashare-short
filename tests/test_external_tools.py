from __future__ import annotations

import json
from datetime import date, datetime, timedelta

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
        [SectorDefinition(name="测试行业", level="SW1", symbols=symbols)],
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
        [SectorDefinition(name="并列行业", level="SW1", symbols=symbols)],
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
    (tmp_path / "sector_leaders_latest.json").write_text(
        json.dumps(leaders), encoding="utf-8"
    )

    payload = build_monthly_context(
        output_dir=tmp_path,
        moment=datetime(2026, 9, 15, 8, 30),
        horizon_days=31,
    )

    assert payload["contract"]["external_ai_role"].startswith("collect current external events")
    assert any(item["name"] == "国庆节" for item in payload["event_window"])
    assert payload["sector_leaders"]["schema"] == "ashare-sector-leaders-v1"
    assert "recent_validation" in payload["historical_validation"]
    assert any(task["topic"] == "business leadership verification" for task in payload["external_research_tasks"])


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
    assert tools["refresh_sector_leaders"]["annotations"]["readOnlyHint"] is False


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
    (tmp_path / "sector_leaders_latest.json").write_text(
        json.dumps(cached), encoding="utf-8"
    )
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
