from __future__ import annotations

from datetime import date, timedelta

from ashare_decision_support.news.theme_history import ThemeOutcomeStore
from ashare_decision_support.news.theme_radar import ThemeSignal
from ashare_decision_support.sectors.base import SectorMetrics


def metrics(electronics: float, market: float) -> dict[str, SectorMetrics]:
    return {
        "电子": SectorMetrics("电子", 2, 2, ret_1d=electronics),
        "银行": SectorMetrics("银行", 2, 2, ret_1d=market),
    }


def signal() -> ThemeSignal:
    return ThemeSignal(
        name="测试主题",
        stage="事件预警，等待价格确认",
        verified_docs=1,
        social_docs=0,
        price_confirmed=False,
        sectors=("电子",),
        event_types=("订单",),
        transmission="测试传导",
        invalidation="测试失效",
        headlines=("测试消息",),
    )


def test_theme_ledger_measures_subsequent_excess_return(tmp_path) -> None:
    store = ThemeOutcomeStore(tmp_path / "themes.json")
    start = date(2026, 8, 3)
    store.record(start, [signal()], metrics(0.0, 0.0))
    for offset in range(1, 6):
        store.record(start + timedelta(days=offset), [], metrics(0.02, 0.01))

    payload = store._load()
    row = payload["signals"][0]

    assert row["outcomes"]["1"]["excess_return"] == 0.005
    assert row["outcomes"]["5"]["excess_return"] > 0.025
    assert store.summary(payload)["signals_with_5d_outcome"] == 1
