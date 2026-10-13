"""Dependency-free MCP STDIO server for local Codex/Claude clients."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from typing import Any

import ashare_decision_support.research.monthly as monthly_research
import ashare_decision_support.research.preopen as preopen_research
import ashare_decision_support.research.sector_confirmation as confirmation_research
import ashare_decision_support.research.sector_forecast as forecast_research
import ashare_decision_support.sectors.backtest as sector_backtest
from ashare_decision_support import __version__
from ashare_decision_support.envfile import load_env_files
from ashare_decision_support.numeric import finite
from ashare_decision_support.tool_api import (
    TOOL_INSTRUCTIONS,
    daily_sector_analysis,
    get_facts,
    monthly_outlook_context,
    preopen_research_context,
    refresh_sector_leaders_tool,
    sector_baseline_backtest,
    sector_confirmation,
    sector_forecast,
)

SERVER_NAME = "ashare-decision-support"
# 版本跟着包走。两处各写一个字面量的话，客户端 `initialize` 报的版本和 `pip show`
# 报的版本会各说各话，而这个字段唯一的用处就是让人对得上是哪一份代码在跑。
SERVER_VERSION = __version__
PROTOCOL_VERSION = "2025-06-18"

_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_facts": get_facts,
    "daily_sector_analysis": daily_sector_analysis,
    "preopen_research_context": preopen_research_context,
    "monthly_outlook_context": monthly_outlook_context,
    "refresh_sector_leaders": refresh_sector_leaders_tool,
    "sector_baseline_backtest": sector_baseline_backtest,
    "sector_forecast": sector_forecast,
    "sector_confirmation": sector_confirmation,
}

# 每个参数都带 description，`additionalProperties: false` 明确拒收拼错的键。
# 声明与执行共用同一份 schema；错误参数在取数或写入之前拒绝。
_TOOLS = [
    {
        "name": "sector_confirmation",
        "description": "读取当天盘中板块截面，逐个确认或否决最新盘前主题情景；不联网。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "default": "briefs",
                    "description": "morning 产物目录",
                }
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "sector_forecast",
        "description": (
            "读取最新盘前结果，按主题簇输出板块情景排序、拥挤度、历史校准状态及"
            "09:35后的盘中确认/失效条件；不联网、不下单。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "default": "briefs",
                    "description": "morning 产物目录",
                }
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "sector_baseline_backtest",
        "description": (
            "读取本地同花顺行业指数历史，滚动评价5日动量、超跌反转、趋势回调和"
            "强弱加速四个固定基线的未来1/3/5日相对收益；不联网、不拟合参数。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "top": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                    "description": "每个信号日选择排名前几个行业",
                }
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "daily_sector_analysis",
        "description": (
            "读取最近一次 morning 生成的每日板块分析；包含主攻板块、行业指数位置、"
            "价格轮动、成交额迁移与情绪旁证。不联网、不重复取数。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "default": "briefs",
                    "description": "morning 落盘事实所在目录，默认 briefs",
                },
                "detail": {
                    "type": "string",
                    "enum": ["summary", "full"],
                    "default": "summary",
                    "description": "默认返回模型摘要；full 返回完整审计矩阵",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "preopen_research_context",
        "description": (
            "生成给 Codex 的盘前研究资料包：A 股最新落盘事实、隔夜外盘快照、"
            "外盘到 A 股板块映射及历史基准率。只提供事实和研究要求，不生成交易指令。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "default": "briefs",
                    "description": "读取 A 股落盘事实的目录，默认 briefs",
                },
                "include_global": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否请求隔夜外盘快照；关闭后只返回 A 股事实",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
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
                        "只返回这几块事实（常见键名如 盘前主链路 / 参数化策略扫描 / 持仓 / "
                        "台账统计；实际可用块随 phase 与落盘结果变化）；省略即全量，"
                        "省掉的块会写进 notes，未知块会报 unknown sections 并列出 available"
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
                    "description": "展望窗口天数，须在 7-93 天范围内",
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

_INPUT_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in _TOOLS}
_ARGUMENT_TYPES = {"string": str, "integer": int, "boolean": bool, "array": list}


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

    按 `schema` 分派时认的是各研究模块自己的常量，不在这里再抄一遍字面量：
    某个包升到 v2 而这里还写着 v1 的话不会报错，只会静默回落到整份 JSON——
    对一个专门给模型调的工具，那正是上面说的那种浪费。
    """
    rendered = payload.get("rendered") if isinstance(payload, dict) else None
    if isinstance(rendered, str) and rendered.strip():
        return rendered
    schema = payload.get("schema")
    if schema == preopen_research.SCHEMA:
        return preopen_research.render_preopen_research(payload)
    if schema == monthly_research.MONTHLY_SCHEMA:
        facts = payload.get("a_share_facts") or {}
        fact_text = facts.get("rendered") or "（事实摘要不可用）"
        horizon = payload.get("horizon") or {}
        notes = payload.get("notes") or []
        leaders = payload.get("sector_leaders")
        lines = [
            f"# A股月度研究资料包 {payload.get('as_of', '—')}",
            f"展望窗口：{horizon.get('days', '—')} 天，至 {horizon.get('end_date', '—')}",
            f"板块龙头缓存：{'可用' if leaders else '不可用'}",
            "",
            str(fact_text),
        ]
        if notes:
            lines.extend(["", "## 数据边界", *(f"- {note}" for note in notes[:12])])
        lines.append("\n完整研究任务、事件窗口和结构化字段见 structuredContent。")
        return "\n".join(lines)
    if schema == forecast_research.SCHEMA:
        market = payload.get("market") or {}
        confidence = payload.get("confidence") or {}
        lines = [
            f"# 板块情景预测 {payload.get('target_session', '—')}",
            (
                f"市场：{market.get('state', '—')}；方向 {market.get('direction', '—')}；"
                f"轮动 {market.get('rotation', '—')}；校准 {confidence.get('status', '—')}"
            ),
            "",
        ]
        for item in (payload.get("forecasts") or [])[:8]:
            members = "、".join(str(name) for name in (item.get("members") or []))
            lines.append(
                f"- {item.get('name', '—')}｜{item.get('scenario', '—')}｜"
                f"分数 {item.get('score', '—')}｜成员 {members or '—'}"
            )
        protocol = payload.get("execution_protocol") or {}
        lines.extend(
            [
                "",
                f"确认时点：{protocol.get('confirm_after', '—')}",
                "完整确认、失效条件和结构化指标见 structuredContent。",
            ]
        )
        return "\n".join(lines)
    if schema == confirmation_research.SCHEMA:
        lines = [
            f"# 板块盘中确认 {payload.get('target_session', '—')}",
            f"截至：{payload.get('as_of') or '—'}；{payload.get('data_status', '—')}",
            "",
        ]
        lines.extend(
            f"- {item.get('name', '—')}｜{item.get('status', '—')}｜"
            f"超额 {_percent_point(item.get('excess_pct'))}｜广度 {_percent(item.get('breadth'))}"
            for item in payload.get("results") or []
        )
        return "\n".join(lines)
    if schema == sector_backtest.SCHEMA:
        date_range = payload.get("date_range") or {}
        lines = [
            "# 板块价格基线历史验证",
            (
                f"样本：{date_range.get('start', '—')} 至 {date_range.get('end', '—')}；"
                f"行业数 {payload.get('sector_count', '—')}"
            ),
            "",
            "| 基线 | 近期样本 | 5日胜率 | 5日平均超额 |",
            "|---|---:|---:|---:|",
        ]
        for item in payload.get("methods") or []:
            recent = next(
                (row for row in (item.get("holdout") or []) if row.get("horizon") == 5), {}
            )
            win_rate = recent.get("win_rate")
            mean_excess = recent.get("mean_excess")
            lines.append(
                f"| {item.get('label', item.get('method', '—'))} | "
                f"{recent.get('samples', 0)} | {_percent(win_rate)} | {_percent(mean_excess)} |"
            )
        lines.append("\n完整1/3/5日、全样本与留出集结果见 structuredContent。")
        return "\n".join(lines)
    return json.dumps(payload, ensure_ascii=False, default=str)


def _percent(value: Any) -> str:
    if not isinstance(value, int | float):
        return "—"
    return f"{float(value) * 100:.2f}%"


def _percent_point(value: Any) -> str:
    if not isinstance(value, int | float):
        return "—"
    return f"{float(value):+.2f}%"


def _validate_argument(name: str, value: Any, spec: dict[str, Any]) -> None:
    """校验工具声明实际使用的类型、枚举、数组元素与数值边界。"""
    kind = spec["type"]
    if not isinstance(value, _ARGUMENT_TYPES[kind]) or (
        kind == "integer" and isinstance(value, bool)
    ):
        raise TypeError(f"{name} must be {kind}")
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError(f"unsupported {name}: {value!r}; expected one of {spec['enum']}")
    if "minimum" in spec and value < spec["minimum"]:
        raise ValueError(f"{name} must be at least {spec['minimum']}")
    if "maximum" in spec and value > spec["maximum"]:
        raise ValueError(f"{name} must be at most {spec['maximum']}")
    if kind == "array":
        for index, item in enumerate(value):
            _validate_argument(f"{name}[{index}]", item, spec["items"])


def _call_tool(request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or name not in _HANDLERS:
        return _error(request_id, -32602, f"unknown tool: {name}")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        return _error(request_id, -32602, "tool arguments must be an object")
    try:
        properties = _INPUT_SCHEMAS[name]["properties"]
        for key, value in arguments.items():
            if key not in properties:
                raise TypeError(f"unexpected argument: {key}")
            _validate_argument(key, value, properties[key])
        # SDK 的启动横幅只能去 stderr；stdout 每行必须保持一条协议消息。
        with redirect_stdout(sys.stderr):
            payload = _HANDLERS[name](**arguments)
        # 先验证整份结果可序列化，含有 rendered 的结果也不能夹带 NaN/Infinity。
        # datetime 等保持与 CLI 一致的字符串口径；错误仍关联本次请求编号。
        json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False)
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


def handle(message: Any) -> dict[str, Any] | None:
    """Handle one JSON-RPC message; notifications intentionally produce no response."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid JSON-RPC request")
    method = message.get("method")
    request_id = message.get("id")
    if not isinstance(method, str) or ("id" in message and type(request_id) not in (str, int)):
        return _error(None, -32600, "invalid request method or id")
    if "id" not in message:
        return None
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _error(request_id, -32602, "params must be an object")
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": TOOL_INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": _TOOLS})
    if method == "tools/call":
        return _call_tool(request_id, params)
    return _error(request_id, -32601, f"method not found: {method}")


def _json_number(value: str) -> float:
    number = finite(value)
    if number is None:
        raise ValueError("JSON numbers must be finite")
    return number


def _response_line(line: str) -> str | None:
    """解析并编码一条消息；错误保留请求编号，不中断后续消息。"""
    try:
        message = json.loads(line, parse_constant=_json_number, parse_float=_json_number)
    except (ValueError, RecursionError) as exc:
        response = _error(None, -32700, f"invalid JSON: {exc}")
    else:
        try:
            response = handle(message)
        except Exception as exc:
            request_id = message.get("id") if isinstance(message, dict) else None
            if type(request_id) not in (str, int):
                request_id = None
            response = _error(request_id, -32603, f"internal error: {type(exc).__name__}: {exc}")
    if response is None:
        return None
    try:
        return json.dumps(response, ensure_ascii=False, default=str, allow_nan=False)
    except Exception as exc:
        error = _error(
            response.get("id"), -32603, f"serialization error: {type(exc).__name__}: {exc}"
        )
        return json.dumps(error, ensure_ascii=False)


def main(stdin: Any = None, stdout: Any = None) -> None:
    """逐行处理 STDIO 消息；可注入流以验证传输和异常恢复。"""
    load_env_files()
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    for line in source:
        if not line.strip():
            continue
        response = _response_line(line)
        if response is not None:
            sink.write(response + "\n")
            sink.flush()


if __name__ == "__main__":
    main()
