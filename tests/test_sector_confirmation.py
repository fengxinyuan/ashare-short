import json
from datetime import datetime

from ashare_decision_support.research.sector_confirmation import build_sector_confirmation
from ashare_decision_support.sectors.intraday import SectorRead, SectorSnapshot, append


def _read(name: str, level: str, excess: float, breadth: float, share: float) -> SectorRead:
    return SectorRead(name, level, excess, excess, breadth, 100.0, share, 30, 30)


def test_intraday_snapshot_confirms_cluster_with_two_industries(tmp_path, monkeypatch) -> None:
    briefs = tmp_path / "briefs"
    state = tmp_path / "state"
    briefs.mkdir()
    morning = {
        "trade_date": "2026-08-28",
        "market": {"score": 70, "direction": "偏多", "breadth": 0.6},
        "sectors": [
            {"name": "电子", "level": "THS_INDUSTRY", "score": 80, "ret_1d": 0.01, "ret_5d": 0.03},
            {"name": "通信", "level": "THS_INDUSTRY", "score": 78, "ret_1d": 0.01, "ret_5d": 0.02},
        ],
        "sector_clusters": {
            "clusters": [{"name": "科技硬件", "sectors": [{"name": "电子"}, {"name": "通信"}]}]
        },
    }
    (briefs / "morning_latest.json").write_text(json.dumps(morning), encoding="utf-8")
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: datetime(2026, 8, 28).date(),
    )
    path = state / "intraday_sector_20260828.jsonl"
    state.mkdir(exist_ok=True)
    append(
        SectorSnapshot(
            datetime(2026, 8, 28, 9, 35),
            [
                _read("电子", "THS_INDUSTRY", 0.4, 0.63, 0.09),
                _read("通信", "THS_INDUSTRY", 0.3, 0.61, 0.07),
            ],
        ),
        path,
    )
    append(
        SectorSnapshot(
            datetime(2026, 8, 28, 9, 40),
            [
                _read("电子", "THS_INDUSTRY", 0.5, 0.65, 0.10),
                _read("通信", "THS_INDUSTRY", 0.4, 0.62, 0.08),
            ],
        ),
        path,
    )
    append(
        SectorSnapshot(
            datetime(2026, 8, 28, 9, 45),
            [
                _read("电子", "THS_INDUSTRY", 0.8, 0.70, 0.11),
                _read("通信", "THS_INDUSTRY", 0.7, 0.68, 0.09),
            ],
        ),
        path,
    )

    result = build_sector_confirmation(briefs, store_root=state)

    assert result["data_status"] == "可确认"
    assert result["results"][0]["status"] == "已确认"
    assert result["results"][0]["independent_industries"] == 2
    assert result["results"][0]["checks"]["attention_not_falling"] is True


def test_intraday_snapshot_falsifies_weak_breadth(tmp_path, monkeypatch) -> None:
    briefs = tmp_path / "briefs"
    state = tmp_path / "state"
    briefs.mkdir()
    state.mkdir(exist_ok=True)
    (briefs / "morning_latest.json").write_text(
        json.dumps(
            {
                "trade_date": "2026-08-28",
                "market": {},
                "sectors": [{"name": "医药生物", "level": "THS_INDUSTRY", "score": 70}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: datetime(2026, 8, 28).date(),
    )
    append(
        SectorSnapshot(
            datetime(2026, 8, 28, 10, 0), [_read("医药生物", "THS_INDUSTRY", -0.2, 0.40, 0.08)]
        ),
        state / "intraday_sector_20260828.jsonl",
    )

    result = build_sector_confirmation(briefs, store_root=state)

    assert result["results"][0]["status"] == "已否决"


def test_lunch_break_snapshot_cannot_upgrade_confirmation(tmp_path, monkeypatch) -> None:
    briefs = tmp_path / "briefs"
    state = tmp_path / "state"
    briefs.mkdir()
    state.mkdir(exist_ok=True)
    (briefs / "morning_latest.json").write_text(
        json.dumps(
            {
                "trade_date": "2026-08-28",
                "market": {},
                "sectors": [{"name": "有色金属", "level": "THS_INDUSTRY", "score": 70}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: datetime(2026, 8, 28).date(),
    )
    append(
        SectorSnapshot(
            datetime(2026, 8, 28, 11, 50), [_read("有色金属", "THS_INDUSTRY", 1.0, 0.75, 0.1)]
        ),
        state / "intraday_sector_20260828.jsonl",
    )

    result = build_sector_confirmation(briefs, store_root=state)

    assert result["data_status"].startswith("午间休市")
    assert result["results"][0]["status"] == "等待数据"


def test_industry_layer_prevents_concept_overlap_from_double_counting(
    tmp_path, monkeypatch
) -> None:
    briefs = tmp_path / "briefs"
    state = tmp_path / "state"
    briefs.mkdir()
    state.mkdir(exist_ok=True)
    (briefs / "morning_latest.json").write_text(
        json.dumps(
            {
                "trade_date": "2026-08-28",
                "market": {},
                "sectors": [
                    {"name": "电子", "level": "THS_INDUSTRY", "score": 80},
                    {"name": "芯片概念", "level": "CONCEPT", "score": 79},
                ],
                "sector_clusters": {
                    "clusters": [
                        {"name": "科技硬件", "sectors": [{"name": "电子"}, {"name": "芯片概念"}]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: datetime(2026, 8, 28).date(),
    )
    append(
        SectorSnapshot(
            datetime(2026, 8, 28, 9, 45),
            [
                _read("电子", "THS_INDUSTRY", 0.5, 0.65, 0.10),
                _read("芯片概念", "CONCEPT", 2.0, 0.90, 0.30),
            ],
        ),
        state / "intraday_sector_20260828.jsonl",
    )

    row = build_sector_confirmation(briefs, store_root=state)["results"][0]

    assert row["matched_members"] == ["电子", "芯片概念"]
    assert row["evidence_members"] == ["电子"]
    assert row["overlap_ignored"] == ["芯片概念"]
    assert row["excess_pct"] == 0.5
    assert row["status"] == "部分确认"
    assert row["checks"]["attention_not_falling"] is False
