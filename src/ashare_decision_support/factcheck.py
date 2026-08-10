"""数字追溯闸门的共用件：怎么把一段文字里的数字抠出来，以及什么数字不必追溯。

**放在包根**，理由同 `fanout.py` 与 `numeric.py`：它约束的不是写作，也不是裁决，
是**一件横跨三层的事**——凡是让模型出文字的地方，文字里的每个数字都要能在喂进去
的事实里找到出处。今天有三道这样的闸门：

| 位置 | 用在哪 | 严到什么程度 |
|---|---|---|
| `writer/llm.py:unverified_numbers` | 简报改写、深挖分步、多空辩论、舆情通读、问答 | 允许「按你写的精度四舍五入后相等」，并认 万/亿/% 后缀 |
| `advice/adjudicate.py:offending_numbers` | 价位裁决 | 只认**数值相等** |
| `advice/external.py:_untraceable_numbers` | 外部裁决包 | 只认 token 原样出现 |

**三种严格程度是有意的，不该统一。** 散文改写本来就会重排数字，把
12198154.379999977 写成 12198154.38 是正常表述；同一件事发生在挂单价上，
22.9512 写成 22.95 就是另一个价了。所以这里收的是**共用的那部分**——数字长什么样、
哪些小整数是序号——判定本身留在各自那层。

收口之前 `ORDINAL_CEILING = 20` 有两份定义（`writer/llm.py` 和
`advice/adjudicate.py`），而 `advice/external.py` 从**两个不同模块**分别拿这道闸门
的两个常量。两份 20 漂成 20 和 15 不会有任何测试变红，只会让同一个「第 18 档」
在简报里放行、在裁决里作废。
"""

from __future__ import annotations

import re

# 小到这个程度的整数是条目序号与档位数（「第 3 档」「5 档买盘」），模型正当地会
# 写出来，而它们本来就不在事实里。
ORDINAL_CEILING = 20

# 千分位是 token 的一部分，不是三个数：把 "12,198,154.38" 切成 12 / 198 / 154.38
# 会让每一个带分隔符的数字都看起来是编的。
NUMBER_PATTERN = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")

# 中文财经行文会就地换算量级。带上后缀，前面那个数就能按对应量级去和事实比，
# 于是 "0.12亿" 对的是 12198154.38，而不是被判成凭空捏造。
UNIT_SCALES: dict[str, float] = {"万": 1e4, "亿": 1e8, "%": 0.01, "％": 0.01}


def fact_numbers(facts_text: str) -> tuple[set[str], set[float]]:
    """事实里出现过的数字，两种形态一起给。

    - **token**：去掉千分位后的原样字符串，用于「原文里有没有这么写过」；
    - **value**：能转成 float 的那些，用于按精度或量级折算后比对。

    两者要一起返回而不是各算各的：调用方先查 token 命中（便宜且最准），未命中
    才落到数值比对，两步之间用的必须是同一次扫描的结果。
    """
    tokens = {token.replace(",", "") for token in NUMBER_PATTERN.findall(facts_text)}
    values: set[float] = set()
    for token in tokens:
        try:
            values.add(float(token))
        except ValueError:
            continue
    return tokens, values
