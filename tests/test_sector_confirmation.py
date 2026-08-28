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
            {"name": "电子", "level": "SW1", "score": 80, "ret_1d": .01, "ret_5d": .03},
            {"name": "通信", "level": "SW1", "score": 78, "ret_1d": .01, "ret_5d": .02},
        ],
        "sector_clusters": {"clusters": [{"name": "科技硬件", "sectors": [{"name": "电子"}, {"name": "通信"}]}]},
    }
    (briefs / "morning_latest.json").write_text(json.dumps(morning), encoding="utf-8")
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: datetime(2026, 8, 28).date(),
    )
    path = state / "intraday_sector_20260828.jsonl"
    state.mkdir(exist_ok=True)
    append(SectorSnapshot(datetime(2026, 8, 28, 9, 35), [
        _read("电子", "SW1", .4, .63, .09), _read("通信", "SW1", .3, .61, .07)
    ]), path)
    append(SectorSnapshot(datetime(2026, 8, 28, 9, 40), [
        _read("电子", "SW1", .5, .65, .10), _read("通信", "SW1", .4, .62, .08)
    ]), path)
    append(SectorSnapshot(datetime(2026, 8, 28, 9, 45), [
        _read("电子", "SW1", .8, .70, .11), _read("通信", "SW1", .7, .68, .09)
    ]), path)

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
    (briefs / "morning_latest.json").write_text(json.dumps({
        "trade_date": "2026-08-28", "market": {},
        "sectors": [{"name": "医药生物", "level": "SW1", "score": 70}],
    }), encoding="utf-8")
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: datetime(2026, 8, 28).date(),
    )
    append(SectorSnapshot(datetime(2026, 8, 28, 10, 0), [
        _read("医药生物", "SW1", -.2, .40, .08)
    ]), state / "intraday_sector_20260828.jsonl")

    result = build_sector_confirmation(briefs, store_root=state)

    assert result["results"][0]["status"] == "已否决"


def test_lunch_break_snapshot_cannot_upgrade_confirmation(tmp_path, monkeypatch) -> None:
    briefs = tmp_path / "briefs"
    state = tmp_path / "state"
    briefs.mkdir()
    state.mkdir(exist_ok=True)
    (briefs / "morning_latest.json").write_text(json.dumps({
        "trade_date": "2026-08-28", "market": {},
        "sectors": [{"name": "有色金属", "level": "SW1", "score": 70}],
    }), encoding="utf-8")
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: datetime(2026, 8, 28).date(),
    )
    append(SectorSnapshot(datetime(2026, 8, 28, 11, 50), [
        _read("有色金属", "SW1", 1.0, .75, .1)
    ]), state / "intraday_sector_20260828.jsonl")

    result = build_sector_confirmation(briefs, store_root=state)

    assert result["data_status"].startswith("午间休市")
    assert result["results"][0]["status"] == "等待数据"


def test_industry_layer_prevents_concept_overlap_from_double_counting(tmp_path, monkeypatch) -> None:
    briefs = tmp_path / "briefs"
    state = tmp_path / "state"
    briefs.mkdir()
    state.mkdir(exist_ok=True)
    (briefs / "morning_latest.json").write_text(json.dumps({
        "trade_date": "2026-08-28", "market": {},
        "sectors": [
            {"name": "电子", "level": "SW1", "score": 80},
            {"name": "芯片概念", "level": "CONCEPT", "score": 79},
        ],
        "sector_clusters": {"clusters": [{
            "name": "科技硬件", "sectors": [{"name": "电子"}, {"name": "芯片概念"}]
        }]},
    }), encoding="utf-8")
    monkeypatch.setattr(
        "ashare_decision_support.research.daily_sector.beijing_today",
        lambda: datetime(2026, 8, 28).date(),
    )
    append(SectorSnapshot(datetime(2026, 8, 28, 9, 45), [
        _read("电子", "SW1", .5, .65, .10),
        _read("芯片概念", "CONCEPT", 2.0, .90, .30),
    ]), state / "intraday_sector_20260828.jsonl")

    row = build_sector_confirmation(briefs, store_root=state)["results"][0]

    assert row["matched_members"] == ["电子", "芯片概念"]
    assert row["evidence_members"] == ["电子"]
    assert row["overlap_ignored"] == ["芯片概念"]
    assert row["excess_pct"] == .5
    assert row["status"] == "部分确认"
    assert row["checks"]["attention_not_falling"] is False
