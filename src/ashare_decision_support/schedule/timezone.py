"""中国 A 股交易日使用的统一时钟。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    """返回带时区的北京时间，避免依赖部署机器的系统时区。"""
    return datetime.now(BEIJING_TZ)


def beijing_today() -> date:
    return beijing_now().date()
