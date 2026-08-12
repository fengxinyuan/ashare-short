"""Dependency-free MCP STDIO server for local Codex/Claude clients."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from ashare_decision_support import __version__
from ashare_decision_support.tool_api import (
    TOOL_INSTRUCTIONS,
    get_facts,
    monthly_outlook_context,
    refresh_sector_leaders_tool,
)

SERVER_NAME = "ashare-decision-support"
# 版本跟着包走。两处各写一个字面量的话，客户端 `initialize` 报的版本和 `pip show`
# 报的版本会各说各话，而这个字段唯一的用处就是让人对得上是哪一份代码在跑。
SERVER_VERSION = __version__
PROTOCOL_VERSION = "2025-06-18"

_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_facts": get_facts,
    "monthly_outlook_context": monthly_outlook_context,
    "refresh_sector_leaders": refresh_sector_leaders_tool,
}

# 每个参数都带 description，`additionalProperties: false` 明确拒收拼错的键。
# 这两件事都是给**调用方是模型**准备的：没有字段说明它只能靠参数名猜语义，而多
# 传一个键在这里会变成 handler 的 TypeError——报成 isError 比静默忽略好，但不如
# 在 schema 里先说清楚。
_TOOLS = [
    {
        "name": "get_facts",
        "description": (
            "读取项目已落盘的确定性 A 股事实包（盘前/盘中/盘后按时段自动选），"
            "含板块排序、候选、持仓、台账统计与近期兑现核验。不调模型、不下单。"
            "**完整一份是 130KB 量级**——只要其中几块时用 sections。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "default": "briefs",
                    "description": "读落盘结果的目录，默认 briefs",
                },
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要额外带上的标的，如 600519.SH；省略则只带持仓与已落盘结果",
                },
                "phase": {
                    "type": "string",
                    "enum": ["pre_open", "intraday", "post_close", "off_hours"],
                    "description": "强制按某个时段装配；省略则按当前北京时间判断",
                },
                "sections": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "只返回这几块事实（键名如 盘前主链路 / 参数化策略扫描 / 持仓 / "
                        "最近一次买卖点 / 台账统计）；省略即全量，省掉的块会写进 notes"
                    ),
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "monthly_outlook_context",
        "description": (
            "装配未来 7-93 天的 A 股月度研究资料包：本地事实、台账兑现、板块轮动记忆、"
            "全板块龙头候选、日历事件窗口与外部核验任务。事件收集与推理由客户端负责。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "default": "briefs",
                    "description": "读落盘结果的目录，默认 briefs",
                },
                "horizon_days": {
                    "type": "integer",
                    "default": 31,
                    "minimum": 7,
                    "maximum": 93,
                    "description": "展望窗口天数，超出范围会被夹到边界",
                },
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要额外带上的标的；省略则只带持仓与已落盘结果",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "refresh_sector_leaders",
        "description": (
            "取全市场日线并生成每个行业/概念的量价龙头候选。**会联网并写入 JSON**；"
            "leadership_rank 只是同板块内的相对量价名次，不是产业地位或收益预测。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "default": "briefs",
                    "description": "结果写到哪个目录，默认 briefs",
                },
                "levels": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["industry", "concept"]},
                    "description": "要算哪几层分类，默认两层都算",
                },
                "top_per_sector": {
                    "type": "integer",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 10,
                    "description": "每个板块保留几个候选",
                },
                "lookback_days": {
                    "type": "integer",
                    "default": 180,
                    "minimum": 90,
                    "maximum": 365,
                    "description": "日线回溯天数",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "openWorldHint": True},
    },
]


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _text_content(payload: dict[str, Any]) -> str:
    """`content` 那一份文本。**不再是 payload 的第二份 JSON。**

    实测一次 `get_facts` 的 payload 是 222KB；把它原样再 dump 一遍塞进 `content`，
    一次工具调用就是 445KB——对一个专门给模型调的工具，这本身就是最大的那处
    浪费：客户端两份都读，而两份说的是同一件事。

    事实包自带 `rendered`，那正是同一份事实的可读版（41KB），MCP 也允许
    `content` 是 `structuredContent` 的等价可读形式。所以有 `rendered` 就用它，
    没有的（龙头包、月度包）才回落到 JSON。
    """
    rendered = payload.get("rendered") if isinstance(payload, dict) else None
    if isinstance(rendered, str) and rendered.strip():
        return rendered
    return json.dumps(payload, ensure_ascii=False, default=str)


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
        # 序列化放在 try 里面，`default=str` 与 CLI 的 `_dump_json` 同口径。
        # 两件事都是被同一个洞逼出来的：facts 里带 `datetime` 时（盘中现算的那批
        # 就带），裸 `json.dumps` 抛的 TypeError 会**穿过 handle()**——于是这一次
        # tools/call 收到的是一条 `id: null` 的 -32603，客户端等的那个 id 永远等不到
        # 回复。CLI 打得出来的 payload，MCP 这一侧不能死在上面。
        text = _text_content(payload)
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
            "content": [{"type": "text", "text": text}],
        },
    )


def main(stdin: Any = None, stdout: Any = None) -> None:
    """STDIO 传输循环。

    两个流是参数而不是直接用 `sys.*`，只为一件事：**这个循环能被测试**。它是
    整条链路上唯一不经过 `handle()` 的部分，也是失败最不显眼的部分——它一坏，
    客户端那一侧看到的是「服务器没反应」，而全部单测照常绿。
    """
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    for line in source:
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
            sink.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
            sink.flush()


if __name__ == "__main__":
    main()
