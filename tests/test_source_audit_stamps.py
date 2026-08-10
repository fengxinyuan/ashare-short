"""数据源审计的时间戳锚定。

这份脚本的三个度量之一是**时效**，而时效恰好是最容易被静默测错的那个：有的源
只给 `HH:MM:SS`，一个字的日期都没有（腾讯逐笔的「成交时间」就是）。把它锚到
`date.today()` 会让上一个交易日的数据看起来是今天的——错得**恰好像最新鲜**。

2026-08-10（周一）08:22 实测：上周五的逐笔被标成「今天 15:13」，一个还没发生的
时刻，lag 算出来是负的。下面的用例钉住三种情形都锚对。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime
from datetime import time as dtime

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "source_audit.py"


def _module():
    """按路径加载：`scripts/` 不是包，也刻意不做成包。

    审计脚本不依赖项目内部模块，为的是链路整体坏掉时它还能跑——把它变成可 import
    的包会让那条性质慢慢失效。

    加载前要先塞进 `sys.modules`：模块里的 dataclass 在 `exec_module` 期间就会去
    解析自己的注解，那时按模块名回查，查不到就抛 AttributeError。
    """
    spec = importlib.util.spec_from_file_location("source_audit", _PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = _module()


@pytest.mark.parametrize(
    ("label", "clock", "now", "expected"),
    [
        # 周一盘前：15:13 今天还没到，它属于上一个交易日（上周五）。
        ("周一盘前", dtime(15, 13, 19), datetime(2026, 8, 10, 8, 22), "2026-08-07"),
        # 盘中：10:23 已经过了，就是今天的。
        ("周一盘中", dtime(10, 23, 0), datetime(2026, 8, 10, 10, 25), "2026-08-10"),
        # 盘后：15:13 已经过了，仍是今天的。
        ("周一盘后", dtime(15, 13, 19), datetime(2026, 8, 10, 16, 0), "2026-08-10"),
        # 周末不是交易日，无论几点都算上周五。
        ("周六晚间", dtime(15, 13, 19), datetime(2026, 8, 8, 20, 0), "2026-08-07"),
        ("周日午间", dtime(15, 13, 19), datetime(2026, 8, 9, 12, 0), "2026-08-07"),
    ],
)
def test_bare_times_land_on_the_session_they_belong_to(label, clock, now, expected) -> None:
    assert audit.anchor_bare_time(clock, now).date().isoformat() == expected, label


def test_a_bare_time_is_never_stamped_in_the_future() -> None:
    """这是上面五条的共同判据，也是原来那个 bug 的判据。

    未来的时间戳不只是「不准」——lag 会变成负数，于是一个三天没更新的源在
    「时效」这一列上排第一。
    """
    now = datetime(2026, 8, 10, 8, 22)
    for hour in range(24):
        stamped = audit.anchor_bare_time(dtime(hour, 30), now)
        assert stamped <= now, f"{hour}:30 被标到了未来：{stamped}"
