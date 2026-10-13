"""MCP 请求边界：错误关联、通知无副作用和 STDIO 失败恢复。"""

from __future__ import annotations

import io
import json

import pytest

from ashare_decision_support import mcp_server


def _request(method, params=None, request_id=7):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def test_stdio_loads_local_configuration_before_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server, "load_env_files", lambda: calls.append("load"))

    def read_config(**kwargs):
        assert calls == ["load"]
        return {"rendered": "ready"}

    monkeypatch.setitem(mcp_server._HANDLERS, "get_facts", read_config)
    sink = io.StringIO()
    mcp_server.main(
        stdin=io.StringIO(json.dumps(_request("tools/call", {"name": "get_facts"}))),
        stdout=sink,
    )
    assert calls == ["load"]
    assert json.loads(sink.getvalue())["result"]["isError"] is False


def test_excessively_nested_json_does_not_stop_the_stream():
    source = io.StringIO("[" * 2000 + "0" + "]" * 2000 + "\n" + json.dumps(_request("ping")))
    sink = io.StringIO()
    mcp_server.main(stdin=source, stdout=sink)
    replies = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert replies[0]["error"]["code"] in (-32700, -32600)
    assert replies[1]["result"] == {}


@pytest.mark.parametrize("request_id", [None, 1.5])
def test_request_ids_require_strings_or_integers(request_id):
    response = mcp_server.handle(_request("ping", request_id=request_id))
    assert response["id"] is None
    assert response["error"]["code"] == -32600


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_json_input_is_rejected_without_stopping_the_stream(number):
    source = io.StringIO(
        '{"jsonrpc":"2.0","id":7,"method":"ping","params":{"number":'
        + number
        + "}}\n"
        + json.dumps(_request("ping", request_id=8))
        + "\n"
    )
    sink = io.StringIO()
    mcp_server.main(stdin=source, stdout=sink)
    replies = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1] == {"jsonrpc": "2.0", "id": 8, "result": {}}


def test_unexpected_dispatch_error_preserves_the_request_id(monkeypatch):
    handle = mcp_server.handle

    def fail_once(message):
        if message["id"] == 7:
            raise RuntimeError("unexpected failure")
        return handle(message)

    monkeypatch.setattr(mcp_server, "handle", fail_once)
    source = io.StringIO("\n".join(json.dumps(_request("ping", request_id=n)) for n in (7, 8)))
    sink = io.StringIO()
    mcp_server.main(stdin=source, stdout=sink)
    replies = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert replies[0]["id"] == 7
    assert replies[0]["error"]["code"] == -32603
    assert replies[1]["result"] == {}


@pytest.mark.parametrize(
    "message",
    [
        None,
        [],
        42,
        "ping",
        {},
        {"jsonrpc": "1.0", "id": 7, "method": "ping"},
        {"jsonrpc": "2.0", "id": [], "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 7, "method": []},
    ],
)
def test_invalid_request_is_reported_without_raising(message):
    response = mcp_server.handle(message)
    assert response["error"]["code"] == -32600
    assert response["id"] is None


@pytest.mark.parametrize(
    "method",
    [
        "notifications/initialized",
        "notifications/cancelled",
        "ping",
        "tools/call",
        "unknown-method",
    ],
)
def test_notifications_never_reply_or_execute_a_tool(method, monkeypatch):
    calls = []
    monkeypatch.setitem(
        mcp_server._HANDLERS, "refresh_sector_leaders", lambda **kwargs: calls.append(kwargs)
    )
    response = mcp_server.handle(
        {
            "jsonrpc": "2.0",
            "method": method,
            "params": {"name": "refresh_sector_leaders", "arguments": {}},
        }
    )
    assert response is None
    assert calls == []


@pytest.mark.parametrize("params", [None, [], "get_facts", False, 12])
@pytest.mark.parametrize("method", ["initialize", "tools/call"])
def test_invalid_params_keep_the_request_id(method, params):
    message = _request(method, request_id="client-42")
    message["params"] = params
    response = mcp_server.handle(message)
    assert response["id"] == "client-42"
    assert response["error"]["code"] == -32602


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"name": []},
        {"name": False},
        {"name": "missing"},
        {"name": "get_facts", "arguments": []},
        {"name": "get_facts", "arguments": None},
        {"name": "get_facts", "arguments": False},
    ],
)
def test_malformed_tool_calls_are_protocol_errors(params):
    response = mcp_server.handle(_request("tools/call", params))
    assert response["id"] == 7
    assert response["error"]["code"] == -32602


@pytest.mark.parametrize("version", ["2025-06-18", "2024-11-05", "2099-01-01"])
def test_initialization_only_negotiates_a_supported_version(version):
    response = mcp_server.handle(_request("initialize", {"protocolVersion": version}))
    assert response["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_facts", {"symbols": "600519.SH"}),
        ("get_facts", {"symbols": [600519]}),
        ("get_facts", {"sections": "持仓"}),
        ("get_facts", {"output_dir": None}),
        ("preopen_research_context", {"include_global": "false"}),
        ("sector_baseline_backtest", {"top": True}),
        ("sector_baseline_backtest", {"top": 0}),
        ("sector_baseline_backtest", {"top": 21}),
        ("monthly_outlook_context", {"horizon_days": 3.5}),
        ("refresh_sector_leaders", {"levels": ["invalid"]}),
        ("refresh_sector_leaders", {"lookback_days": 0}),
        ("refresh_sector_leaders", {"top_per_sector": 11}),
    ],
)
def test_invalid_tool_arguments_do_not_reach_business_handlers(name, arguments, monkeypatch):
    calls = []
    monkeypatch.setitem(mcp_server._HANDLERS, name, lambda **kwargs: calls.append(kwargs) or {})
    response = mcp_server.handle(_request("tools/call", {"name": name, "arguments": arguments}))
    assert response["id"] == 7
    assert response["result"]["isError"] is True
    assert calls == []


def test_stdio_recovers_with_correlated_errors_and_no_notification_output():
    messages = [
        "[]",
        "{broken-json",
        json.dumps(_request("tools/call", {"name": []})),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled"}),
        json.dumps(_request("ping", request_id=8)),
    ]
    sink = io.StringIO()
    mcp_server.main(stdin=io.StringIO("\n".join(messages) + "\n"), stdout=sink)
    replies = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert [row["id"] for row in replies] == [None, None, 7, 8]
    assert [row["error"]["code"] for row in replies[:-1]] == [-32600, -32700, -32602]
    assert replies[-1]["result"] == {}


def test_sdk_banner_is_sent_to_stderr_and_not_the_protocol_stream(monkeypatch, capsys):
    def noisy_tool(**kwargs):
        print("SDK startup banner")
        return {"schema": "test"}

    monkeypatch.setitem(mcp_server._HANDLERS, "get_facts", noisy_tool)
    source = io.StringIO(json.dumps(_request("tools/call", {"name": "get_facts"})) + "\n")
    sink = io.StringIO()
    mcp_server.main(stdin=source, stdout=sink)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SDK startup banner" in captured.err
    assert json.loads(sink.getvalue())["result"]["isError"] is False


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_tool_payload_keeps_id_and_does_not_poison_the_stream(value, monkeypatch):
    monkeypatch.setitem(
        mcp_server._HANDLERS, "get_facts", lambda **kwargs: {"rendered": "摘要", "price": value}
    )
    source = io.StringIO(
        "\n".join(
            json.dumps(row)
            for row in [
                _request("tools/call", {"name": "get_facts"}),
                _request("ping", request_id=8),
            ]
        )
        + "\n"
    )
    sink = io.StringIO()
    mcp_server.main(stdin=source, stdout=sink)
    assert "NaN" not in sink.getvalue()
    assert "Infinity" not in sink.getvalue()
    replies = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert [row["id"] for row in replies] == [7, 8]
    assert replies[0]["result"]["isError"] is True
    assert replies[1]["result"] == {}
