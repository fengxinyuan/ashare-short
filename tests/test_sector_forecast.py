import json
from datetime import date

from ashare_decision_support.research.sector_forecast import build_sector_forecast


def test_forecast_collapses_overlapping_themes_and_exposes_falsification(tmp_path, monkeypatch) -> None:
    payload = {
        "trade_date": "2026-08-28",
        "market": {"direction": "偏强", "score": 72.0, "breadth": 0.62},
        "sectors": [
            {
                "name": "芯片概念",
                "level": "concept",
                "score": 81.0,
                "ret_1d": 0.02,
                "ret_5d": 0.06,
                "breadth": 0.72,
                "reasons": ["价格与广度共振"],
            },
            {
                "name": "电子",
                "level": "industry",
                "score": 76.0,
                "ret_1d": 0.01,
                "ret_5d": 0.04,
                "breadth": 0.66,
            },
        ],
        "sector_clusters": {
            "clusters": [
                {
                    "name": "科技成长",
                    "sectors": [{"name": "芯片概念"}, {"name": "电子"}],
                }
            ],
            "crowded": [{"name": "科技成长", "seat_share": 1.0}],
        },
        "rotation_price": {
            "trade_date": "2026-08-27",
            "adjacent_label": "快速轮动",
            "persistence": 0.1,
        },
        "turnover_migration": {"total_change": 0.08, "total_trend": "回升"},
        "sector_forecast_validation": {"trusted": False, "sample_count": 3},
    }
    (tmp_path / "morning_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: date(2026, 8, 28),
    )

    result = build_sector_forecast(tmp_path)

    assert result["schema"] == "ashare-sector-forecast-v1"
    assert result["information_date"] == "2026-08-27"
    assert result["market"]["state"] == "偏强但快速轮动"
    assert result["forecasts"][0]["name"] == "科技成长"
    assert result["forecasts"][0]["members"] == ["芯片概念", "电子"]
    assert result["forecasts"][0]["independent_signal"] is False
    assert result["confidence"]["status"] == "uncalibrated"
    assert result["execution_protocol"]["confirm"]
    assert result["execution_protocol"]["invalidate"]
    assert any("不是概率" in note for note in result["notes"])
