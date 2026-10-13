"""事实包各数据块的覆盖与时点状态。

按交易日区分当日、最近交易日、上一交易日与陈旧数据。
缺日期和未来日期分别标为时点未知、未来数据；本次现算仅由 live() 显式登记。
日历不完整时保留校验限制。本模块标注状态，由调用方决定如何使用数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ashare_decision_support.schedule.calendar import default_calendar, is_session
from ashare_decision_support.signals.series import parse_bar_date

STATUS_TODAY = "当日"
STATUS_LATEST = "最近交易日"
STATUS_PREVIOUS = "上一交易日"
STATUS_STALE = "陈旧"
STATUS_UNKNOWN = "时点未知"
STATUS_FUTURE = "未来数据"
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
        return self.status in (
            STATUS_STALE,
            STATUS_MISSING,
            STATUS_FAILED,
            STATUS_UNKNOWN,
            STATUS_FUTURE,
        )

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
        if self.status in (STATUS_MISSING, STATUS_FAILED, STATUS_UNKNOWN, STATUS_FUTURE):
            return f"{self.name}：{self.status}{f'（{self.detail}）' if self.detail else ''}"
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
            self.calendar = default_calendar()

    def stamped(self, name: str, trade_date: Any) -> BlockHealth:
        """落盘块按数据日期判定；缺少日期或日期在未来都不能认作新鲜数据。"""
        stamp = parse_bar_date(trade_date)
        if stamp is None:
            return self._add(
                BlockHealth(
                    name=name, status=STATUS_UNKNOWN, detail="缺少有效数据日期，无法确认时效"
                )
            )
        if stamp > self.today:
            return self._add(
                BlockHealth(
                    name=name,
                    status=STATUS_FUTURE,
                    trade_date=str(stamp),
                    detail=f"数据日期 {stamp} 晚于分析日期 {self.today}，不可作为当日事实",
                )
            )
        behind = self.sessions_behind(stamp)
        status = STATUS_TODAY if stamp == self.today else STATUS_LATEST
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
        start = self.today - timedelta(days=span)
        return sum(
            1 for offset in range(1, span + 1) if self._is_session(start + timedelta(days=offset))
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
                "无有效日期与未来日期均标为降级；休市日的最近数据不标成当日。"
                "只覆盖落盘产出与本次现算的块；持仓、告警、台账是本地状态，随读随新"
            ),
            "块": [item.to_dict() for item in self.blocks],
            "需要注意": [item.describe() for item in degraded],
        }

    def _add(self, item: BlockHealth) -> BlockHealth:
        self.blocks.append(item)
        return item

    def _is_session(self, day: date) -> bool:
        return is_session(day, self.calendar)

    def _calendar_caveat(self) -> str:
        configured = getattr(self.calendar, "configured", False) if self.calendar else False
        return "" if configured else "未经节假日校验，长假期间的落后交易日数会偏大"
