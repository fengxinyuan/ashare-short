"""业绩层：取数收敛、双面不对称否决。

这一层最容易出的两类错都不会报错，只会算错，所以每一类都钉一条测试：

1. **取错「预测指标」**——同一家公司归母净利润「续亏」而营业收入「略增」，
   取错就把亏损公司读成增长的。
2. **数据缺失被当成「没问题」**——业绩取不到时如果不放行，等于让缺数据冒充
   风险判断；如果放行了却不说，简报读起来像「这批票都过了业绩关」。
"""

from __future__ import annotations

from datetime import date

import pytest

from ashare_decision_support.adapters.earnings import (
    EarningsAdapter,
    EarningsSnapshot,
    previous_period,
    report_period,
)
from ashare_decision_support.screens.earnings import EarningsScreen


class FakeAk:
    """akshare 桩。按报告期返回不同结果，可指定哪些接口挂掉。"""

    def __init__(
        self,
        forecasts: dict[str, list[dict]] | None = None,
        fast: dict[str, list[dict]] | None = None,
        broken: set[str] | None = None,
    ) -> None:
        self.forecasts = forecasts or {}
        self.fast = fast or {}
        self.broken = broken or set()
        self.asked: list[str] = []

    def stock_yjyg_em(self, date):
        self.asked.append(f"yjyg:{date}")
        if "yjyg" in self.broken:
            raise ConnectionError("feed down")
        return self.forecasts.get(date, [])

    def stock_yjkb_em(self, date):
        self.asked.append(f"yjkb:{date}")
        if "yjkb" in self.broken:
            raise ConnectionError("feed down")
        return self.fast.get(date, [])


def forecast_row(code: str, metric: str, kind: str, pct: float, day: str = "2026-07-30") -> dict:
    return {
        "股票代码": code,
        "股票简称": f"票{code}",
        "预测指标": metric,
        "预告类型": kind,
        "业绩变动幅度": pct,
        "公告日期": day,
        "业绩变动原因": "原因",
    }


# 取数与收敛


def test_report_period_follows_the_disclosure_window() -> None:
    assert report_period(date(2026, 8, 3)) == "20260630"
    assert report_period(date(2026, 6, 29)) == "20260331"
    assert report_period(date(2026, 1, 15)) == "20251231"
    assert previous_period("20260331") == "20251231"


def test_net_profit_wins_over_revenue_when_they_disagree() -> None:
    """**这一条是这个源最容易出的错。**

    实测同一家公司：归母净利润「续亏」、营业收入「略增」。按行到达顺序挑，
    或者按最后一行覆盖，都会把一家持续亏损的公司读成「略增」——而那正是
    业绩否决要拦的那一类。
    """
    ak = FakeAk(
        forecasts={
            "20260630": [
                # 营收那行**排在前面**，确保测的不是「谁先到取谁」。
                forecast_row("000001", "营业收入", "略增", 25.6),
                forecast_row("000001", "扣除后营业收入", "略增", 22.5),
                forecast_row("000001", "归属于上市公司股东的净利润", "续亏", 6.98),
            ]
        }
    )
    snapshot = EarningsAdapter(ak_module=ak).fetch(date(2026, 8, 3))

    item = snapshot.get("000001.SZ")
    assert item is not None
    assert item.kind == "续亏"
    assert item.metric == "归属于上市公司股东的净利润"
    assert item.is_net_profit_basis is True


def test_falls_back_to_a_lesser_metric_and_says_so() -> None:
    """退档是允许的，但必须能被下游看见——营收口径的结论不该拿去否决。"""
    ak = FakeAk(forecasts={"20260630": [forecast_row("000001", "营业收入", "略减", -8.0)]})
    item = EarningsAdapter(ak_module=ak).fetch(date(2026, 8, 3)).get("000001.SZ")

    assert item is not None
    assert item.metric == "营业收入"
    assert item.is_net_profit_basis is False


def test_empty_period_walks_back_one_quarter() -> None:
    """季末刚过时当期一家都没披露。硬用当期会得到空快照，而空快照和
    「这批公司都没有业绩问题」在只报数量的设计里长得一模一样。"""
    ak = FakeAk(
        forecasts={
            "20260331": [forecast_row("000001", "归属于上市公司股东的净利润", "预增", 120.0)]
        }
    )
    snapshot = EarningsAdapter(ak_module=ak).fetch(date(2026, 7, 2))

    assert snapshot.ok
    assert snapshot.period == "20260331"
    assert any("改用上一期" in note for note in snapshot.notes)


def test_a_dead_feed_is_reported_not_raised() -> None:
    snapshot = EarningsAdapter(ak_module=FakeAk(broken={"yjyg"})).fetch(date(2026, 8, 3))

    assert snapshot.ok is False
    assert not snapshot.forecasts
    assert any("没有业绩风险否决" in note for note in snapshot.notes)


def test_a_fast_report_overrides_the_forecast_range() -> None:
    """快报是算出来的实际数，预告是区间估计。两边都有时以快报为准。"""
    ak = FakeAk(
        forecasts={
            "20260630": [forecast_row("000001", "归属于上市公司股东的净利润", "预增", 120.0)]
        },
        fast={
            "20260630": [
                {
                    "股票代码": "000001",
                    "股票简称": "票000001",
                    "净利润-净利润": 8.1e7,
                    "净利润-同比增长": 4.4,
                    "公告日期": "2026-08-01",
                }
            ]
        },
    )
    snapshot = EarningsAdapter(ak_module=ak).fetch(date(2026, 8, 3))
    item = snapshot.get("000001.SZ")

    assert item is not None
    assert item.source == "快报"
    # 预告说「预增」（≥50%），快报实际只有 +4.4% —— 标签必须跟着实际数走，
    # 沿用预告的标签会让最硬的那个数被最软的那个标签盖住。
    assert item.kind == "略增"
    assert any("以快报实际数覆盖" in note for note in snapshot.notes)


def test_a_loss_making_fast_report_is_not_read_as_growth() -> None:
    """净利润为负时同比「增长」说的是亏得比去年少，那是减亏不是预增。"""
    ak = FakeAk(
        forecasts={
            "20260630": [forecast_row("000002", "归属于上市公司股东的净利润", "预减", -10.0)]
        },
        fast={
            "20260630": [
                {
                    "股票代码": "000002",
                    "股票简称": "票000002",
                    "净利润-净利润": -5.0e7,
                    "净利润-同比增长": 30.0,
                    "公告日期": "2026-08-01",
                }
            ]
        },
    )
    item = EarningsAdapter(ak_module=ak).fetch(date(2026, 8, 3)).get("000002.SZ")

    assert item is not None
    assert item.kind == "减亏"
    # 方向为正（在改善）与水平为负（还在亏）同时成立，两个字段各答一问。
    assert item.direction == "正面"
    assert item.is_loss is True
    assert item.is_profitable_growth is False


def test_a_narrowing_loss_is_neither_a_veto_nor_a_catalyst() -> None:
    """「减亏 +300%」是这一层最容易标反的一类。

    只看方向 → 标成业绩催化，而公司还在亏钱；
    只看水平 → 和「增亏」一起否决，而它其实在改善。
    正确的位置在两者之间，所以它必须走一条自己的分支。
    """
    snapshot = snapshot_of(
        forecast_row("000003", "归属于上市公司股东的净利润", "减亏", 300.0, day="2026-08-01")
    )
    kept, verdicts, stats = EarningsScreen(snapshot).filter(["000003.SZ"])
    verdict = verdicts["000003.SZ"]

    assert kept == ["000003.SZ"]
    assert not verdict.is_blocked
    assert not verdict.catalysts
    assert verdict.is_strong_catalyst is False
    assert stats.catalysts == 0
    assert any("仍处亏损" in text for text in verdict.warnings)
    # 类型分布那一行提到「减亏」是对的（它确实披露了）；不能收它的是**催化榜**。
    lines = snapshot.describe()
    assert any("减亏 1" in line for line in lines)
    assert not any("增幅翻倍以上" in line for line in lines)


# 双面不对称


def snapshot_of(*rows: dict) -> EarningsSnapshot:
    ak = FakeAk(forecasts={"20260630": list(rows)})
    return EarningsAdapter(ak_module=ak).fetch(date(2026, 8, 3))


def test_deteriorating_losses_are_removed() -> None:
    snapshot = snapshot_of(
        forecast_row("000001", "归属于上市公司股东的净利润", "首亏", -180.0),
        forecast_row("000002", "归属于上市公司股东的净利润", "增亏", -60.0),
        forecast_row("000003", "归属于上市公司股东的净利润", "预增", 300.0),
    )
    kept, verdicts, stats = EarningsScreen(snapshot).filter(["000001.SZ", "000002.SZ", "000003.SZ"])

    assert kept == ["000003.SZ"]
    assert stats.blocked == 2
    assert verdicts["000001.SZ"].is_blocked


def test_a_severe_decline_blocks_but_a_mild_one_only_warns() -> None:
    """单纯的「预减」可能只是同比 -5%，那是波动不是业绩杀。"""
    snapshot = snapshot_of(
        forecast_row("000001", "归属于上市公司股东的净利润", "预减", -72.0),
        forecast_row("000002", "归属于上市公司股东的净利润", "预减", -8.0),
    )
    kept, verdicts, _ = EarningsScreen(snapshot).filter(["000001.SZ", "000002.SZ"])

    assert kept == ["000002.SZ"]
    assert verdicts["000002.SZ"].warnings
    assert not verdicts["000002.SZ"].is_blocked


def test_a_revenue_basis_reading_never_vetoes() -> None:
    """一家公司可以营收微降而净利润翻倍。营收口径的「略减」不是业绩杀。"""
    snapshot = snapshot_of(forecast_row("000001", "营业收入", "略减", -80.0))
    kept, verdicts, _ = EarningsScreen(snapshot).filter(["000001.SZ"])

    assert kept == ["000001.SZ"]
    assert not verdicts["000001.SZ"].is_blocked
    assert any("不作否决依据" in text for text in verdicts["000001.SZ"].warnings)


def test_persistent_loss_handling_is_switchable() -> None:
    """「早就在亏」和「刚开始亏」对市场是两件事，取值尚未被台账验证，所以留开关。"""
    snapshot = snapshot_of(forecast_row("000001", "归属于上市公司股东的净利润", "续亏", 6.9))

    strict, _, _ = EarningsScreen(snapshot).filter(["000001.SZ"])
    relaxed, verdicts, _ = EarningsScreen(snapshot, block_persistent_loss=False).filter(
        ["000001.SZ"]
    )

    assert strict == []
    assert relaxed == ["000001.SZ"]
    assert verdicts["000001.SZ"].warnings


def test_a_catalyst_is_annotated_and_never_scored() -> None:
    """正面那一侧只标注。加分的错误买不回来，标注的不会。"""
    snapshot = snapshot_of(
        forecast_row("000001", "归属于上市公司股东的净利润", "预增", 1197.0, day="2026-08-01")
    )
    kept, verdicts, stats = EarningsScreen(snapshot).filter(["000001.SZ"])
    verdict = verdicts["000001.SZ"]

    assert kept == ["000001.SZ"]
    assert stats.catalysts == 1
    assert verdict.is_strong_catalyst is True
    assert not verdict.is_blocked
    # 裁决对象上没有任何分数字段——这一层没有能力改排序。
    assert "score" not in verdict.to_dict()


def test_an_old_forecast_is_support_not_a_catalyst() -> None:
    """四月披露的预增和昨晚披露的预增不该在简报里长得一模一样。"""
    snapshot = snapshot_of(
        forecast_row("000001", "归属于上市公司股东的净利润", "预增", 300.0, day="2026-04-28")
    )
    _kept, verdicts, _ = EarningsScreen(snapshot).filter(["000001.SZ"])
    verdict = verdicts["000001.SZ"]

    assert verdict.is_strong_catalyst is False
    assert any("已消化" in text for text in verdict.catalysts)


@pytest.mark.parametrize("snapshot", [None, EarningsSnapshot()])
def test_missing_data_lets_everything_through_and_says_so(snapshot) -> None:
    """拿不到业绩去否决标的，等于让数据缺失冒充风险判断。"""
    symbols = ["000001.SZ", "000002.SZ"]
    kept, verdicts, stats = EarningsScreen(snapshot).filter(symbols)

    assert kept == symbols
    assert verdicts == {}
    assert stats.active is False
    assert "没有业绩风险否决" in (stats.note() or "")


def test_undisclosed_names_are_not_treated_as_bad_news() -> None:
    """未披露 ≠ 业绩差。大多数公司根本不发预告。"""
    snapshot = snapshot_of(forecast_row("000001", "归属于上市公司股东的净利润", "首亏", -180.0))
    kept, _verdicts, stats = EarningsScreen(snapshot).filter(
        ["000001.SZ", "600519.SH", "300750.SZ"]
    )

    assert kept == ["600519.SH", "300750.SZ"]
    assert stats.covered == 1
    assert "未披露不等于业绩差" in (stats.note() or "")
