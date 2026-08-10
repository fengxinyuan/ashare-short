"""Dependency-free MCP STDIO server for local Codex/Claude clients."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from ashare_decision_support.tool_api import (
    TOOL_INSTRUCTIONS,
    get_facts,
    monthly_outlook_context,
    refresh_sector_leaders_tool,
)

SERVER_NAME = "ashare-decision-support"
SERVER_VERSION = "0.3.0"
PROTOCOL_VERSION = "2025-06-18"

_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_facts": get_facts,
    "monthly_outlook_context": monthly_outlook_context,
    "refresh_sector_leaders": refresh_sector_leaders_tool,
}

_TOOLS = [
    {
        "name": "get_facts",
        "description": "读取项目已落盘的确定性 A 股事实包；不调模型、不下单。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_dir": {"type": "string", "default": "briefs"},
                "symbols": {"type": "array", "items": {"type": "string"}},
                "phase": {"type": "string"},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "monthly_outlook_context",
        "description": "装配未来 7-93 天的 A 股月度研究资料包；客户端负责外部事件收集和推理。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_dir": {"type": "string", "default": "briefs"},
                "horizon_days": {"type": "integer", "default": 31, "minimum": 7, "maximum": 93},
                "symbols": {"type": "array", "items": {"type": "string"}},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "refresh_sector_leaders",
        "description": "取全市场日线并生成每个行业/概念的量价龙头候选；会联网并写入 JSON。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_dir": {"type": "string", "default": "briefs"},
                "levels": {"type": "array", "items": {"type": "string", "enum": ["industry", "concept"]}},
                "top_per_sector": {"type": "integer", "default": 3, "minimum": 1, "maximum": 10},
                "lookback_days": {"type": "integer", "default": 180, "minimum": 90, "maximum": 365},
            },
        },
        "annotations": {"readOnlyHint": False, "openWorldHint": True},
    },
]


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message; notifications intentionally produce no response."""
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": message.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": TOOL_INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": _TOOLS})
    if method != "tools/call":
        return _error(request_id, -32601, f"method not found: {method}")

    params = message.get("params") or {}
    name = params.get("name")
    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(request_id, -32602, f"unknown tool: {name}")
    try:
        payload = handler(**(params.get("arguments") or {}))
    except Exception as exc:  # tool failure is data for the client, not a dead server
        return _result(
            request_id,
            {
                "isError": True,
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
            },
        )
    return _result(
        request_id,
        {
            "isError": False,
            "structuredContent": payload,
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        },
    )


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = handle(message)
        except json.JSONDecodeError as exc:
            response = _error(None, -32700, f"invalid JSON: {exc}")
        except Exception as exc:
            response = _error(None, -32603, f"internal error: {type(exc).__name__}: {exc}")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
