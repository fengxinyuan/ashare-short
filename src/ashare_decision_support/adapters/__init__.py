"""Data adapters."""

from ashare_decision_support.adapters.akshare_calendar import AkshareCalendarAdapter
from ashare_decision_support.adapters.broad_index import (
    BroadIndexAdapter,
    BroadIndexRead,
    divergence_verdict,
    read_divergence,
)
from ashare_decision_support.adapters.earnings import (
    EarningsAdapter,
    EarningsForecast,
    EarningsSnapshot,
)
from ashare_decision_support.adapters.global_market import (
    GlobalMarketAdapter,
    GlobalMarketReading,
    GlobalMarketSnapshot,
)
from ashare_decision_support.adapters.sina_intraday import SinaIntradayAdapter
from ashare_decision_support.adapters.tencent_detail import TencentDetailAdapter
from ashare_decision_support.adapters.ths_taxonomy import ThsTaxonomySource
from ashare_decision_support.adapters.tickflow_adapter import TickFlowAdapter

__all__ = [
    "AkshareCalendarAdapter",
    "BroadIndexAdapter",
    "BroadIndexRead",
    "EarningsAdapter",
    "EarningsForecast",
    "EarningsSnapshot",
    "GlobalMarketAdapter",
    "GlobalMarketReading",
    "GlobalMarketSnapshot",
    "SinaIntradayAdapter",
    "TencentDetailAdapter",
    "ThsTaxonomySource",
    "TickFlowAdapter",
    "divergence_verdict",
    "read_divergence",
]
