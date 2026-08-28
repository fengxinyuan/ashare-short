"""盘中时钟：交易时段内「现在是第几分钟」。

沪深两市一天开两段（09:30–11:30、13:00–15:00），中间休市 90 分钟。任何按「开盘
以来第几分钟」推导的读数——分时均价的分母、量能的时间进度、集合竞价之后的
成交节奏——都必须把午休那段挖掉，否则 13:05 会被算成开盘后 215 分钟，而它实际
上只走了 125 分钟的交易时间。量比会因此系统性偏低约四成。

三个适配器此前各写了一份逐字相同的实现。放在这里是因为这是**市场规则**，
不是某个数据源的解析细节。
"""

from __future__ import annotations

from datetime import datetime, time

MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)

# 上午段的分钟数。午休之后的计时从这里接着往上走。
MORNING_MINUTES = 120
# 全天交易分钟数。
SESSION_MINUTES = 240


def minutes_since_open(moment: datetime) -> int:
    """开盘以来走过的**交易**分钟数，午休不计。

    开盘前返回 0，午休期间返回 120（上午已走完），收盘后继续累加——收盘后不
    截断是有意的：调用方拿它做分母，钳到 240 会让 15:30 的读数和 15:00 的读数
    完全一样，而那两个时刻的「数据是否还在更新」并不相同。
    """
    day = moment.date()
    morning_open = datetime.combine(day, MORNING_OPEN)
    afternoon_open = datetime.combine(day, AFTERNOON_OPEN)
    if moment <= morning_open:
        return 0
    if moment < datetime.combine(day, MORNING_CLOSE):
        return int((moment - morning_open).total_seconds() // 60)
    if moment < afternoon_open:
        return MORNING_MINUTES
    return MORNING_MINUTES + int((moment - afternoon_open).total_seconds() // 60)


def is_continuous_session(moment: datetime) -> bool:
    """Whether live A-share quotes should be moving at this moment."""
    current = moment.time().replace(tzinfo=None)
    return (
        MORNING_OPEN <= current <= MORNING_CLOSE
        or AFTERNOON_OPEN <= current <= AFTERNOON_CLOSE
    )
