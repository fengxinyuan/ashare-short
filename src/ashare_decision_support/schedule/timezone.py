"""中国 A 股交易时钟，统一使用北京时间。

- `beijing_now()` 返回带时区的时刻，用于对外时间戳。
- `beijing_naive_now()` 返回无时区的北京墙上时间，用于交易时段和数据时效比较。
- `beijing_today()` 返回北京日历日期；是否为交易日由 `schedule/calendar.py` 判断。

带时区输入先转换为北京时间；无时区输入按约定视为北京时间，避免宿主机时区介入。
epoch 秒与时区无关，转换为墙上时间时仍显式指定北京时间。

注入时钟的默认值同样使用这些函数。Ruff DTZ 规则检查调用，无法发现把
`datetime.now` 作为函数值传出的情况；相关默认路径由 `tests/test_timezone.py` 覆盖。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    """返回带时区的北京时间，避免依赖部署机器的系统时区。"""
    return datetime.now(BEIJING_TZ)


def beijing_naive_now() -> datetime:
    """北京**墙上时间**，不带 tzinfo。

    这是 `datetime.now()` 在本项目里的唯一正确替代：语义完全一样（一个可以直接
    和 `published_at` 相减的本地时间），区别只是它算的是北京，而不是碰巧装在哪台
    机器上。
    """
    return beijing_now().replace(tzinfo=None)


def beijing_today() -> date:
    return beijing_now().date()


def to_beijing_naive(moment: datetime) -> datetime:
    """把一个带时区的时刻折算成北京墙上时间。

    数据源的时间戳有三种形态：本来就是北京时间的裸字符串（财联社、东财）、带
    `Z` 或 `+00:00` 的 UTC（RSS、外部裁决包）、以及 epoch 毫秒（tickflow）。
    后两种要先落到同一把尺子上，否则时效折减会把一条 8 小时前的快讯算成刚发的。

    已经是 naive 的原样返回——那种时间戳按约定就是北京时间，`astimezone` 会拿
    宿主机时区去解释它，正好制造出这个模块想消灭的那个错。
    """
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(BEIJING_TZ).replace(tzinfo=None)


def beijing_from_epoch(seconds: float) -> datetime:
    """epoch 秒 → 北京墙上时间。

    裸 `datetime.fromtimestamp(s)` 按**宿主机时区**展开，和 `datetime.now()` 是
    同一个坑：行情源给的毫秒时间戳在 UTC 容器里会全部落后 8 小时，于是 K 线的
    「最后一根」变成午盘那根，盘中告警按错的时刻判。
    """
    return datetime.fromtimestamp(seconds, BEIJING_TZ).replace(tzinfo=None)
