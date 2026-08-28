r"""把模型的回答收成能核对的东西：JSON 载荷，以及用来找回原文的锚点文本。

**放在包根**，理由同 `factcheck.py` / `fanout.py` / `numeric.py`：它约束的不是
分类、不是通读、也不是裁决，是**一件横跨三层的事**——凡是让模型交出结构化答案的
地方，都要先从一段可能带围栏、可能带前后废话的文本里，把那个 JSON 抠出来。

收口之前有三份实现，而且**已经分叉了**：

| 位置 | 剥围栏的办法 | 抠 JSON 的办法 |
|---|---|---|
| `news/llm_classifier.py:_parse_json_array` | `_FENCE` 正则，全文搜 | 首个 `[` 到末个 `]` |
| `news/daily_read.py:_parse_json_object` | `_FENCE` 正则（**逐字相同的第二份**） | 首个 `{` 到末个 `}` |
| `advice/adjudicate.py:_parse_json` | `strip("`")` + 按行切，**只在开头生效** | 贪婪 `\{.*\}` |

前两份的 `_FENCE` 是同一行正则抄了两遍；第三份是另一套写法，且**更弱**——它只在
整段回答以 ``` 开头时才剥围栏，模型多寒暄一句「好的，结果如下：」就漏过去了（那
一路靠贪婪正则兜住，所以今天不出错，但兜底和判据不是一回事）。三份并存的代价是
`factcheck.py` 开头记的那种：给其中一份补一条容错，另外两条链路在同样的输入上
照旧丢弃整份回答，而**三处都不会有测试变红**。

## 判据是三份的并集，不是其中一份

`_parse` 先拿剥完围栏的整段试一次 `json.loads`，失败了再退到「首个开括号到末个
闭括号」。这样：

- 三份原来能解析的，现在**一个不少**；
- 原来 `{...}` 开头但尾巴上挂了废话的那种（`daily_read` / `llm_classifier` 会整份
  丢弃，`adjudicate` 能救回来），现在三处都救得回来。

**取不到时返回 `None`，不返回 `{}` / `[]`。** 同 `numeric` 那一层对「拿不到」与
「空的」的区分：一份合法但没有结论的回答（`{"chains": []}`）和一份根本没解析出来
的回答，该导向不同的 note。调用方要退化成空容器，自己写 `or {}`。

## `anchor_text` 为什么也在这里

`llm_classifier` 与 `daily_read` 都要求模型**原样回抄**所依据标题的前几个字，再拿
这个前缀去原文里找回是哪一条（`ANCHOR_CHARS` 两边分别是 8 和 12，那个差异是有意的
——后者的标题更长，见各自的注释）。两边归一化用的字符类原来是同一串正则抄了两遍，
一份预编译、一份没有。

**这一份不能漂**，而且漂了是静默的：归一化同时作用在**语料**和**模型回抄的锚点**
上，两边的字符类差一个符号，匹配率会整体变化，而结果只表现为「今天模型引用不到
原文的条数多了几条」——那和模型当天发挥不好长得一模一样。

`news/dedup.py:_STRIP_PATTERN` **有意不并进来**：它多剥 `<>‘’｜~—-_*#`，还要剥掉
标题开头的来源标签，因为它算的是**去重指纹**（宁可多剥，让同一条消息的几个转发版
本撞成一个 key）；锚点匹配要的是「模型抄漏一个标点也能对上」，多剥会让两条不同的
标题撞成同一个前缀。同一条纪律写在 `sectors/panel.py`：共用的是判据，不是说法。
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
