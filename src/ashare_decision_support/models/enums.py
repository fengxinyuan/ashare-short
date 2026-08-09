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
    """打法。一只票能不能买，取决于它所处趋势允许哪些打法、今天的情绪周期又
    允许哪些，两者取交集。交集为空就是不买——这比给它打一个低分更诚实。

    `TAIL_END` 多一维约束：**时点**。前四条是收盘后选次日，判据全在日线；它是
    当日 14:30 决策、次日了结，判据全在分时。所以它在盘前/盘后链路上永远不成立
    ——那不是「这只票不合格」，是「此刻不是它的时点」，由
    `PlaybookMatch.blocked_by_session` 表达，不进拒绝理由。
    """

    BREAKOUT = "突破"
    PULLBACK = "回踩"
    RANGE_LOW = "区间下沿"
    REVERSAL = "超跌反转"
    TAIL_END = "尾盘"
