"""模型结构化回答的共用 JSON 解析与原文锚点归一化。

先剥离 Markdown 围栏并解析整段；失败时尝试首尾括号包围的片段。
解析失败或类型不符返回 None，合法的空对象、空数组保持原样。

原文和模型回抄的标题前缀使用相同的锚点归一化规则。
新闻去重需要剥离更多来源标签和符号，因此在 `news/dedup.py` 单独处理。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 模型爱把 JSON 包在 markdown 围栏里，即便提示词里写了「不要代码块」。全文搜而不是
# 只看开头：寒暄一句再给围栏是最常见的一种不听话。
FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# 锚点比对时要忽略的装饰字符。全角与半角成对列出，因为语料里两种都有——模型回抄时
# 经常把全角括号抄成半角。
_ANCHOR_NOISE = re.compile(r"[\s　【】\[\]（）()《》「」“”\"':：,，.。!！?？、|]+")


def parse_object(text: str) -> dict[str, Any] | None:
    """从模型回答里抠出一个 JSON 对象。抠不出来返回 None。"""
    return _parse(text, "{", "}", dict)


def parse_array(text: str) -> list[Any] | None:
    """从模型回答里抠出一个 JSON 数组。抠不出来返回 None。"""
    return _parse(text, "[", "]", list)


def anchor_text(text: str) -> str:
    """归一化用于锚点比对的文本：去掉空白与常见装饰。

    语料与模型回抄的锚点**必须过同一个函数**——这是它收在这里的全部理由。
    """
    return _ANCHOR_NOISE.sub("", text or "")


def _parse(text: str, opener: str, closer: str, kind: type) -> Any | None:
    candidate = (text or "").strip()
    fenced = FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    # 先按整段试。绝大多数听话的回答走的是这一条，不必再扫一遍括号。
    parsed = _loads(candidate, kind)
    if parsed is not None:
        return parsed
    # 退到「首个开括号 → 末个闭括号」。前后废话、尾巴上多一句解释都能救回来。
    start, end = candidate.find(opener), candidate.rfind(closer)
    if start == -1 or end <= start:
        return None
    return _loads(candidate[start : end + 1], kind)


def _loads(candidate: str, kind: type) -> Any | None:
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    # 类型不对等于没解析出来：要对象却收到数组时，下游按 dict 用会当场炸，
    # 而这一层的契约是「拿不到就 None」，不是「拿到什么给什么」。
    return parsed if isinstance(parsed, kind) else None
