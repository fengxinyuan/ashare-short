"""这份事实包里，每一块数据是什么时候的、哪几块根本没有。

`ask/context.py` 装配上下文时，缺块与陈旧一直是**散文**：「参数化策略扫描尚无落盘
结果」「盘前主链路的数据日期是 2026-08-12」。人读得懂，但这份包的主要读者是另一
个模型，而对它来说这两句话和正文里的任何一句没有区别——于是最常见的一种错法长
这样：

> 周一早上问「今天板块怎么排」，上一次跑 `morning` 是上周五。事实包照常给出一份
> 完整的板块排序，模型照常按它回答，**两边都没有说错任何一句话**，只是那份排序
> 描述的是三个交易日之前的盘面。

数字这一层已经有闸门了（`factcheck.py`：文字里的每个数都要能追溯到事实），但闸门
只管「这个数在不在事实里」，不管「事实本身是哪天的」。这个模块补的是后半句：把
时点判定做成**读数**，和其它读数一样带判据、可被程序读取。

判定只有三档，按**交易日**数，不按自然日：

| 判定 | 含义 |
|---|---|
| `当日` | 数据日期就是今天 |
| `上一交易日` | 中间只隔了一个交易日——盘前还没跑今天那一轮时的正常状态 |
| `陈旧` | 中间隔了两个或更多交易日 |

按自然日会在**每个周一早上**误报：上周五的盘前简报到周一是 3 个自然日，但它就是
上一个交易日的数据，那时它并不陈旧。节假日表缺失时（`schedule/calendar.py` 的第 2
层）周末仍然会被排除，只是长假会被算多，所以那时读数里会带一句「未经节假日校验」
——**说不准就说不准**，不假装确定。

这一层不做任何拦截：陈旧的块照样进事实包。它只保证「这是三天前的数据」这句话和
数据本身在同一个结构里，而不是在一段散文的第七条里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

STATUS_TODAY = "当日"
STATUS_PREVIOUS = "上一交易日"
STATUS_STALE = "陈旧"
STATUS_MISSING = "缺失"
STATUS_FAILED = "读取失败"
STATUS_LIVE = "本次现算"

# 隔了这么多个交易日就算陈旧。1 是「昨天收盘后跑的那份」，在盘前是正常的；
# 2 意味着中间整整跳过了一个交易日的收盘。
STALE_SESSIONS = 2

# 往回数交易日的上限。数据日期比这还早的时候，具体差几个交易日已经不影响判断了
# ——都是「陈旧」，而无上限的循环会在一个手工写坏的日期上跑很久。
MAX_LOOKBACK_DAYS = 400


@dataclass(frozen=True)
class BlockHealth:
    """一块数据的时点判定。"""

    name: str
    status: str
    trade_date: str | None = None
    sessions_behind: int | None = None
    detail: str = ""

    @property
    def degraded(self) -> bool:
        return self.status in (STATUS_STALE, STATUS_MISSING, STATUS_FAILED)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"名称": self.name, "判定": self.status}
        if self.trade_date:
            payload["数据日期"] = self.trade_date
        if self.sessions_behind is not None:
            payload["落后交易日"] = self.sessions_behind
        if self.detail:
            payload["说明"] = self.detail
        return payload

    def describe(self) -> str:
        if self.status == STATUS_MISSING:
            return f"{self.name}：缺失{f'（{self.detail}）' if self.detail else ''}"
        if self.status == STATUS_FAILED:
            return f"{self.name}：读取失败{f'（{self.detail}）' if self.detail else ''}"
        if self.status == STATUS_STALE:
            return f"{self.name}：{self.trade_date} 的数据，已落后 {self.sessions_behind} 个交易日"
        if self.trade_date:
            return f"{self.name}：{self.trade_date}（{self.status}）"
        return f"{self.name}：{self.status}"


@dataclass
class HealthLedger:
    """一次装配里每一块的状态。**记录的时候就判定**，不留到事后解析散文。"""

    today: date
    calendar: Any = None
    blocks: list[BlockHealth] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 交易日历自己造，调用方不必为了拿一个时点判定先去构造它。取不到就是 None，
        # 那时只排除周末，并在读数里说明（`_calendar_caveat`）。
        if self.calendar is None:
            self.calendar = _default_calendar()

    def stamped(self, name: str, trade_date: Any) -> BlockHealth:
        """有数据日期的块。判定按交易日数，写不出日期时只记「有这一块」。"""
        stamp = _as_date(trade_date)
        if stamp is None:
            return self._add(BlockHealth(name=name, status=STATUS_LIVE))
        behind = self.sessions_behind(stamp)
        status = STATUS_TODAY
        if behind >= STALE_SESSIONS:
            status = STATUS_STALE
        elif behind == 1:
            status = STATUS_PREVIOUS
        return self._add(
            BlockHealth(
                name=name,
                status=status,
                trade_date=str(stamp),
                sessions_behind=behind,
                detail=self._calendar_caveat(),
            )
        )

    def live(self, name: str) -> BlockHealth:
        """本次现算出来的块——没有数据日期可谈，它就是此刻。"""
        return self._add(BlockHealth(name=name, status=STATUS_LIVE))

    def missing(self, name: str, detail: str = "") -> BlockHealth:
        return self._add(BlockHealth(name=name, status=STATUS_MISSING, detail=detail))

    def failed(self, name: str, detail: str = "") -> BlockHealth:
        return self._add(BlockHealth(name=name, status=STATUS_FAILED, detail=detail))

    def sessions_behind(self, stamp: date) -> int:
        """`stamp` 之后到今天为止，过了几个交易日。

        今天当天算在内：周一早上看上周五的数据，答案是 1（周一这个交易日已经开始
        了），不是 3 个自然日。没有节假日表时只排除周末，长假会被算多——那时
        `_calendar_caveat` 会把这一点写进读数。
        """
        if stamp >= self.today:
            return 0
        span = min((self.today - stamp).days, MAX_LOOKBACK_DAYS)
        return sum(
            1
            for offset in range(1, span + 1)
            if self._is_session(stamp + timedelta(days=offset))
        )

    def to_dict(self) -> dict[str, Any]:
        degraded = [item for item in self.blocks if item.degraded]
        return {
            # 一块都没记录时不能说「正常」：那两种情况（全部新鲜 / 根本没装配任何
            # 落盘产出）在读数里必须分得开，否则一次 `output_dir=None` 的调用会
            # 产出一份自称数据正常的空包。
            "判定": ("未记录" if not self.blocks else "降级" if degraded else "正常"),
            "口径": (
                "落后交易日按交易日历数，不按自然日；"
                f"落后 {STALE_SESSIONS} 个及以上交易日判为陈旧。"
                "只覆盖落盘产出与本次现算的块；持仓、告警、台账是本地状态，随读随新"
            ),
            "块": [item.to_dict() for item in self.blocks],
            "需要注意": [item.describe() for item in degraded],
        }

    # ---- internals -------------------------------------------------------

    def _add(self, item: BlockHealth) -> BlockHealth:
        self.blocks.append(item)
        return item

    def _is_session(self, day: date) -> bool:
        if self.calendar is not None:
            try:
                return bool(self.calendar.is_trading_day(day))
            except Exception:
                return day.weekday() < 5
        return day.weekday() < 5

    def _calendar_caveat(self) -> str:
        configured = getattr(self.calendar, "configured", False) if self.calendar else False
        return "" if configured else "未经节假日校验，长假期间的落后交易日数会偏大"


def _default_calendar() -> Any:
    """节假日表。读不到（状态目录不可用）就是 None，判定退回只排除周末。"""
    try:
        from ashare_decision_support.schedule.calendar import TradingCalendar

        return TradingCalendar()
    except Exception:
        return None


def _as_date(value: Any) -> date | None:
    """`2026-08-12` / `20260812` / `2026/08/12 15:00:00` / `date` 都收，其余返回 None。

    形状这么多是因为落盘的那几份 payload 各写各的：盘前主链路写 ISO 串，板块指数
    那一路的 `trade_date` 是八位数字，而 `morning` 的板块包里还会出现带斜杠、带
    收盘时刻的写法（`research/daily_sector.py` 原来为此自己留了一份解析器）。
    认不出来时**不猜**——猜错一位就是把当日数据判成陈旧，或者反过来。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    # 日期之后的部分（空格分隔的时刻、或者 `T`）对判定没有影响，切掉再解析；
    # 斜杠与短横只是同一个日期的两种写法。
    text = text.replace("/", "-")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
