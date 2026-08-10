"""带 schema 戳的单文件 JSON 缓存：读回来、验戳、原子写回去。

和 `news/cache.py` 是两件事，别混。那边是**按交易日分片、按时长过期**的舆情
缓存（一天一个目录，`cached_at` 到点作废）；这里是**一份长期存在、按结构版本
作废**的参照数据缓存——行业分类、板块成分、指数代码表。它们不按时间过期，因为
「申万一级有哪些成分股」昨天和今天是同一个答案；它们按**戳**作废，因为一旦这份
数据的结构变了，老文件里按旧口径写的行会静默参与新口径的查询。

原来五个地方各写了一份（`adapters/ths_sector_index.py` 的代码表与日线序列各一份、
`adapters/ths_taxonomy.py`、`adapters/security_master.py`、`sectors/taxonomy.py`），
读那半份逐字相同。收在这里的不只是重复，还有三处各自的漏洞：

- **五份里只有一份是原子写的。** 其余四份直接 `path.write_text(...)`：先把文件
  截成 0 字节，再往里写。写到一半被 kill 或磁盘写满，留下的是一份残缺 JSON，
  **而原来那份好的已经没了**。对大多数缓存这只是「下次重拉」，但
  `_write_series` 那一份不是——它是整份重写（旧序列并上今天这根），而上游
  `last.js` 只回**当年**的数据。丢掉的跨年历史重拉不回来，那正是 60 日以上
  窗口要用的部分。原子写（临时文件 + `replace`）让「文件被截断过」这个状态
  根本不存在：`replace` 要么换成新的整份，要么旧的整份原封不动。
- **四份捕的是 `json.JSONDecodeError`，漏掉 `UnicodeDecodeError`。** 缓存按
  UTF-8 读，文件被截断在一个多字节字符中间时抛的是后者，而它不是前者的子类
  ——那一路会把「缓存坏了」变成一次未捕获异常，而这恰好就是上一条会造出来的
  文件。两者都在 `ValueError` 下面，所以这里捕 `ValueError`。
- **戳的键统一成 `schema`。** `security_master.py` 原来写 `schema_version`，五份
  里只有它这么写。统一之后那一份存量缓存会验不过戳、重建一次——而那正是戳
  存在的意义，不是它的代价。

**戳管的是结构，不管内容完整性。** 这两件事分开：`sectors/taxonomy.py` 里记着
一次现场事故，一份 SW1 缓存少了 469/4430 只成分股，文件本身完全正常——那是取数
时有分片失败还照写了，不是写坏的。防它的是调用方在写之前的完整性检查（「一个
分片都没失败才配带戳」），不是这里。

放在包根而不是 `adapters/` 下，理由同 `numeric.py` 与 `fanout.py`：它约束的不是
某一个数据源，是**一类落盘形状**，今天的调用方已经横跨 `adapters/` 三处与
`sectors/` 一处。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# 戳写在这个键下面。五份缓存共用一个键名，是为了让「这个文件是什么结构版本」
# 这句话在五个文件里长得一样——键名各写各的时候，加一份新缓存的人得先挑一个。
STAMP_KEY = "schema"


def read_stamped(path: Path | None, schema: Any) -> dict[str, Any] | None:
    """读回一份验过戳的缓存。任何一步不对都返回 None，含义是「没有缓存」。

    「不对」包括：路径没配、文件不在、读不动、不是合法 JSON、顶层不是对象、
    戳对不上。调用方只需要判 `is None`，**不需要分辨是哪一种**——这几种情况的
    处置完全一样：重新拉一次。
    """
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get(STAMP_KEY) != schema:
        return None
    return payload


def write_stamped(
    path: Path | None,
    payload: dict[str, Any],
    schema: Any,
    *,
    indent: int | None = None,
) -> bool:
    """盖上戳原子写回去，写成返回 True。

    **写不成只降速，不影响正确性**——调用方此时已经拿到了数据，缓存写失败的
    唯一后果是下次还得再拉一遍。所以这里吞掉 `OSError` 而不是抛给主链路。

    临时文件名带上 pid：盘中监控循环与手动跑一次 CLI 可能同时在写同一份缓存，
    共用一个 `.tmp` 会让两个进程互相截断对方写到一半的内容，而 `replace` 之后
    看起来两次都成功了。
    """
    if path is None:
        return False
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({STAMP_KEY: schema, **payload}, ensure_ascii=False, indent=indent),
            encoding="utf-8",
        )
        temporary.replace(path)
    except (OSError, TypeError, ValueError):
        # 半份临时文件不该留在缓存目录里——它不会被读到（读的是 `path`），
        # 但会让「这个目录里有什么」这个问题以后越来越难回答。
        temporary.unlink(missing_ok=True)
        return False
    return True
