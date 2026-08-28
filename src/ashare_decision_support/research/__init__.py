"""External-AI research packs built only from deterministic project facts."""

from ashare_decision_support.research.daily_sector import (
    load_daily_sector_analysis,
    render_daily_sector_analysis,
)
from ashare_decision_support.research.leaders import (
    LEADERS_SCHEMA,
    build_sector_leaders,
    refresh_sector_leaders,
)
from ashare_decision_support.research.monthly import (
    MONTHLY_SCHEMA,
    build_monthly_context,
)

__all__ = [
    "LEADERS_SCHEMA",
    "MONTHLY_SCHEMA",
    "build_monthly_context",
    "build_sector_leaders",
    "load_daily_sector_analysis",
    "refresh_sector_leaders",
    "render_daily_sector_analysis",
]
