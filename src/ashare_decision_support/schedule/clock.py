"""沪深交易时钟：累计交易分钟数排除 11:30–13:00 午休。

供日内成交量进度与分时分析共用，13:05 对应开盘后 125 个交易分钟。
"""

from __future__ import annotations

from datetime import datetime, time

MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)

# 集合竞价开始报价的时刻。从这一刻起，行情源里的成交额、涨跌幅、涨停池都是
# **今天还没走完**的数：`ask/context.py` 据此起算「盘前 → 盘中」，
# `schedule/calendar.py:session_forming` 据此判断此刻拿到的全市场读数是不是半截。
AUCTION_OPEN = time(9, 15)

# 上午段的分钟数。午休之后的计时从这里接着往上走。
MORNING_MINUTES = 120
# 全天交易分钟数。
SESSION_MINUTES = 240

# 一天按时刻分成的四个阶段。`ask/context.py` 据此决定装配哪些上下文，
# `factcheck.py` 据此判定文字里的即时动作是否越界——后者不能 import `ask/`
# （`ask/` 依赖包根，反过来成环），所以取值放在这里而不是各写一份字面量。
PHASE_PRE_OPEN = "pre_open"
PHASE_INTRADAY = "intraday"
PHASE_POST_CLOSE = "post_close"
PHASE_OFF_HOURS = "off_hours"


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
    return MORNING_OPEN <= current <= MORNING_CLOSE or AFTERNOON_OPEN <= current <= AFTERNOON_CLOSE


def within_session_hours(moment: datetime) -> bool:
    """集合竞价已开始、尚未收盘。午休也算在内：午休时的成交额同样只是上半场。

    只看钟，不看日历——周末与节假日由 `schedule/calendar.py:session_forming` 判，
    这里没有日历。
    """
    current = moment.time().replace(tzinfo=None)
    return AUCTION_OPEN <= current < AFTERNOON_CLOSE
