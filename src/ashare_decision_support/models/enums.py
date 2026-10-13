from enum import StrEnum


class WyckoffBias(StrEnum):
    ACCUMULATION = "ACCUMULATION"
    MARKUP = "MARKUP"
    DISTRIBUTION = "DISTRIBUTION"
    MARKDOWN = "MARKDOWN"
    NEUTRAL = "NEUTRAL"


class EffortResult(StrEnum):
    UP_EFFORT_CONFIRMED = "UP_EFFORT_CONFIRMED"
    DOWN_EFFORT_CONFIRMED = "DOWN_EFFORT_CONFIRMED"
    ABSORPTION = "ABSORPTION"
    EXHAUSTION = "EXHAUSTION"
    COMPRESSION = "COMPRESSION"
    NEUTRAL = "NEUTRAL"


class TrendState(StrEnum):
    UPTREND = "UPTREND"
    REBOUND = "REBOUND"
    RANGE = "RANGE"
    DOWNTREND = "DOWNTREND"


class EmotionCycle(StrEnum):
    """全市场情绪周期。判据见 `strategy/regime.py`。

    这是**决定今天做不做**的那个读数，不是描述性标签。超短的同一套规则在退潮期
    和启动期的期望值可以是反号的，所以它必须先于选股生效，而不是事后用来分层。
    """

    FREEZING = "冰点"
    LAUNCH = "启动"
    MOMENTUM = "发酵"
    CLIMAX = "高潮"
    DIVERGENCE = "分歧"
    EBB = "退潮"
    # 涨停池缺失时的档位。**不等于中性**——见 `strategy/regime.py` 的仓位上限。
    UNKNOWN = "未知"


class Playbook(StrEnum):
    """已实现的日线打法；适用范围由个股趋势与市场情绪周期的交集决定。"""

    BREAKOUT = "突破"
    PULLBACK = "回踩"
    RANGE_LOW = "区间下沿"
    REVERSAL = "超跌反转"
