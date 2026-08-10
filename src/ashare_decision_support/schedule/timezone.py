"""中国 A 股交易日使用的统一时钟。

**这套系统在naive 的北京墙上时间里工作**，这是有意的决定，不是漏了 tzinfo：
全系统只有一个时区（沪深两市的交易时钟），带上 tzinfo 不增加任何信息，只会让
09:15 / 11:30 / 15:00 这类边界判断多一层转换。`pyproject.toml` 里 `DTZ` 整族被
忽略，记的就是这条。

**但「naive」必须是「naive 的北京时间」，不能是「naive 的宿主机时间」。**
这两件事只在部署机时区恰好是 Asia/Shanghai 时才相等，而这正是 `datetime.now()`
散落在四十多处时的真实状态——本机对，容器里差 8 小时。差 8 小时之后：

- `date.today()` 在 UTC 机器上，北京时间 08:00 之前返回的是**前一天**，而盘前
  跑批就在 08:30；早跑半小时，整轮就按昨天的交易日算；
- 新闻时效折减拿 `published_at`（数据源给的北京时间，无时区）减 `datetime.now()`，
  差 8 小时意味着**当天的快讯全部被判为「未来」或超龄丢弃**。

两者都不会报错，只会让盘前少一块东西。所以时钟只有这一个来源。

三个函数，各有明确用途，别混用：

- `beijing_now()`  带时区，用于对外落地的时间戳（isoformat 会带 +08:00）；
- `beijing_naive_now()`  墙上时间，用于**和数据源给的无时区时间戳比较**——
  快讯的 `published_at`、行情的 K 线时间都是这一类，跟 aware 值相减会直接
  `TypeError`；
- `beijing_today()`  交易日。

**`time.time()` 不在这条规矩里**，它返回 epoch 秒，与时区无关。缓存年龄、
令牌过期这类「过了多久」用它是对的，本文件不提供替代品。
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
