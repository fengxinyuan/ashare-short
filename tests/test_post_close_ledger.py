"""盘后执行卡的候选进台账，事后才有地方核对「明天可以做的」做对了没有。"""

from __future__ import annotations

import json

from ashare_decision_support.jobs.post_close import POST_CLOSE_SOURCE, run_post_close
from ashare_decision_support.journal import Journal
from ashare_decision_support.journal.stats import summarize
from tests.test_daily_jobs import FakeAdapter, make_bars


def pullback_bars(symbol: str) -> list[dict]:
    """稳步上涨之后最后三天缩量小幅回落，且仍在 MA20 上方：回踩打法能验过的形状。

    每天只回 0.5%：日涨 0.15 元的趋势里 MA20 落后最新价约 1.4 元，回得再深一点
    收盘就会跌到 MA20 下方，趋势判成震荡，回踩打法根本不会被验。
    """
    bars = make_bars(symbol, start_price=20.0, drift=0.15)
    for bar in bars[-3:]:
        previous = bars[bars.index(bar) - 1]
        close = round(previous["close"] * 0.995, 2)
        bar.update(
            open=round(previous["close"], 2),
            high=round(previous["close"] + 0.1, 2),
            low=round(close - 0.2, 2),
            close=close,
            volume=int(bar["volume"] * 0.6),
        )
    return bars


def test_actionable_rows_are_recorded_under_their_own_source(tmp_path) -> None:
    book = Journal(root=tmp_path / "journal")
    bars = {
        "AAA.SH": pullback_bars("AAA.SH"),
        "FALL.SH": make_bars("FALL.SH", start_price=60.0, drift=-0.25),
    }

    summary = run_post_close(
        adapter=FakeAdapter(bars),
        symbols=["AAA.SH", "FALL.SH"],
        output_dir=tmp_path,
        notifiers=[],
        use_limit_pool=False,
        journal=book,
    )
    payload = json.loads((tmp_path / "post_close_latest.json").read_text(encoding="utf-8"))
    rows = book.recommendations()

    assert summary["actionable"] >= 1, summary["data_notes"]
    assert summary["journal_records"] == summary["actionable"] == len(rows)
    # 只记过了打法门槛的那几行；只能看不能做的不进台账。
    assert {row["symbol"] for row in rows} == {row["symbol"] for row in payload["actionable"]}
    assert all(row["source"] == POST_CLOSE_SOURCE for row in rows)
    # 参考价是当日收盘，锚定日就是盘后卡的交易日；核对从下一个交易日开始。
    assert all(row["reference_date"] == row["trade_date"] == payload["trade_date"] for row in rows)
    assert all(row["stop_pct"] < 0 < row["target_pcts"][0] for row in rows)
    assert all(row["rrr"] is not None for row in rows)
    # 情绪周期与仓位系数随记录保存，事后能按周期分层。
    assert all(row["market_state"]["regime"] == "未知" for row in rows)
    assert any("post_close" in note for note in summary["data_notes"])


def test_no_journal_means_nothing_is_written(tmp_path) -> None:
    summary = run_post_close(
        adapter=FakeAdapter({"AAA.SH": pullback_bars("AAA.SH")}),
        symbols=["AAA.SH"],
        output_dir=tmp_path,
        notifiers=[],
        use_limit_pool=False,
    )

    assert summary["journal_records"] == 0


def test_post_close_outcomes_stay_out_of_the_morning_selection_edge() -> None:
    """入选/对照差值只属于晨报那条链路：盘后卡的票是人手点名的，从没进过板块排序。"""
    outcome = {
        "kind": "outcome",
        "source": POST_CLOSE_SOURCE,
        "rec_kind": "recommendation",
        "trade_date": "2026-08-17",
        "sessions": 5,
        "next_close_pct": 0.02,
        "close_pct": 0.02,
        "mfe_pct": 0.03,
        "mae_pct": -0.01,
        "entered": False,
    }
    stats = summarize([outcome])

    assert stats["overall"]["samples"] == 1
    assert stats["selection_edge"]["picked_samples"] == 0
    assert stats["selection_edge"]["available"] is False
