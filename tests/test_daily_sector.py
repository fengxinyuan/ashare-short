from __future__ import annotations

import json
from datetime import date

import pytest

from ashare_decision_support.research.daily_sector import (
    compact_daily_sector_analysis,
    load_daily_sector_analysis,
    render_daily_sector_analysis,
)


def _payload() -> dict:
    return {
        "trade_date": "2026-08-26",
        "market": {
            "direction": "偏多",
            "score": 72.5,
            "breadth": 0.61,
            "median_ret_5d": 0.034,
            "warnings": ["宽基与等权宽度背离"],
        },
        "sectors": [
            {
                "name": "电子",
                "level": "SW1",
                "direction": "偏强",
                "score": 88.0,
                "ret_1d": 0.021,
                "ret_5d": 0.067,
                "breadth": 0.75,
                "covered": 30,
                "constituents": 40,
                "reasons": ["量价同步", "新闻覆盖充分"],
                "warnings": [],
            }
        ],
        "rotation_price": {
            "trade_date": "2026-08-25",
            "switch_label": "强者恒强",
            "adjacent_label": "高低切换",
            "rising": [{"name": "电子"}],
            "falling": [{"name": "银行"}],
        },
        "turnover_migration": {
            "total_change": -0.02,
            "total_trend": 0.11,
            "rising": [{"name": "电子"}],
            "distribution": [{"name": "银行"}],
        },
        "sector_index": {
            "gainers": [{"name": "电子"}],
            "losers": [{"name": "银行"}],
            "faded": [{"name": "通信"}],
        },
        "limit_pool": {"limit_up": 45, "limit_down": 3, "blowup_rate": 0.18, "max_streak": 6},
        "data_notes": ["板块指数序列截止到 2026-08-25"],
    }


def test_daily_sector_report_reads_only_the_morning_payload(tmp_path, monkeypatch) -> None:
    (tmp_path / "morning_latest.json").write_text(
        json.dumps(_payload(), ensure_ascii=False), encoding="utf-8"
    )
    # 「今天」必须钉住。这个用例原来不钉，于是它断言的 `is_stale is False` 只在
    # 2026-08-26 当天成立——一条**跟着日历变红**的断言，而且红的那天看起来像是
    # 代码坏了。
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: date(2026, 8, 27),
    )
    payload = load_daily_sector_analysis(tmp_path)
    rendered = render_daily_sector_analysis(payload)

    assert payload["schema"] == "ashare-sector-analysis-v1"
    assert payload["trade_date"] == "2026-08-26"
    # 昨天（周三）收盘那份，在周四盘前是**正常状态**，不是陈旧。
    assert payload["data_health"]["status"] == "上一交易日"
    assert payload["data_health"]["sessions_behind"] == 1
    assert payload["data_health"]["is_stale"] is False
    assert "历史复看" not in rendered
    assert "数据日期：2026-08-26" in rendered
    assert "## 主攻板块" in rendered
    assert "电子" in rendered and "银行" in rendered
    assert "高低切换" in rendered
    assert "放量下跌" in rendered
    assert "炸板率 +18.00%" in rendered
    assert "数据边界" in rendered


def test_compact_daily_sector_omits_large_diagnostic_matrices() -> None:
    payload = {
        **_payload(),
        "schema": "ashare-sector-analysis-v1",
        "news_coverage": {"all": {}},
    }
    payload["data_notes"] = [f"note-{index}" for index in range(25)]

    compact = compact_daily_sector_analysis(payload)

    assert compact["view"] == "summary"
    assert "news_coverage" not in compact
    assert compact["omitted"]["data_notes"] == 5
    assert len(compact["data_notes"]) == 20
    assert compact["rendered"].startswith("# 每日板块分析")


def test_daily_sector_report_marks_stale_snapshots(tmp_path, monkeypatch) -> None:
    payload = _payload()
    payload["trade_date"] = "2026-08-10"
    (tmp_path / "morning_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: date(2026, 8, 26),
    )

    loaded = load_daily_sector_analysis(tmp_path)
    rendered = render_daily_sector_analysis(loaded)

    assert loaded["data_health"]["sessions_behind"] == 12
    assert loaded["data_health"]["status"] == "陈旧"
    assert loaded["data_health"]["is_stale"] is True
    assert "数据已落后 12 个交易日" in rendered
    assert "历史复看" in rendered


def test_daily_sector_report_is_not_stale_on_monday_morning(tmp_path, monkeypatch) -> None:
    """周一读上周五那份：隔了两个自然日的周末，但只落后 1 个交易日。

    这正是旧口径每周误报一次的那一天——`DECISIONS.md` §二十四 的判据。
    """
    payload = _payload()
    payload["trade_date"] = "2026-08-21"  # 周五
    (tmp_path / "morning_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: date(2026, 8, 24),  # 周一
    )

    loaded = load_daily_sector_analysis(tmp_path)

    assert loaded["data_health"]["sessions_behind"] == 1
    assert loaded["data_health"]["is_stale"] is False
    assert "历史复看" not in render_daily_sector_analysis(loaded)


def test_daily_sector_report_accepts_slash_dates(tmp_path, monkeypatch) -> None:
    payload = _payload()
    payload["trade_date"] = "2026/08/10 15:00:00"
    (tmp_path / "morning_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: date(2026, 8, 26),
    )

    loaded = load_daily_sector_analysis(tmp_path)

    assert loaded["data_health"]["trade_date"] == "2026-08-10"
    assert loaded["data_health"]["sessions_behind"] == 12


def test_daily_sector_report_requires_a_generated_morning_result(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="先运行 `ashare-ds morning`"):
        load_daily_sector_analysis(tmp_path)


def test_daily_sector_report_rejects_non_object_payload(tmp_path) -> None:
    (tmp_path / "morning_latest.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="不是有效的板块事实包"):
        load_daily_sector_analysis(tmp_path)
