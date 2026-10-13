from ashare_decision_support.sectors.confirmation_ledger import SectorConfirmationLedger


def test_confirmation_ledger_deduplicates_and_keeps_transitions(tmp_path) -> None:
    ledger = SectorConfirmationLedger(tmp_path / "ledger.json")
    base = {
        "target_session": "2026-08-28",
        "as_of": "2026-08-28T09:45:00",
        "results": [{"name": "科技硬件", "status": "部分确认", "evidence_members": ["电子"]}],
    }
    first = ledger.record(base)
    second = ledger.record(
        {
            **base,
            "as_of": "2026-08-28T10:05:00",
            "results": [
                {"name": "科技硬件", "status": "已确认", "evidence_members": ["电子", "通信"]}
            ],
        }
    )

    assert first["records"] == second["records"] == 1
    assert second["by_latest_status"]["已确认"] == 1
    payload = ledger._load()
    assert [row["status"] for row in payload["records"][0]["transitions"]] == ["部分确认", "已确认"]


def test_waiting_data_is_not_recorded(tmp_path) -> None:
    ledger = SectorConfirmationLedger(tmp_path / "ledger.json")
    summary = ledger.record(
        {
            "target_session": "2026-08-28",
            "as_of": None,
            "results": [{"name": "科技硬件", "status": "等待数据"}],
        }
    )
    assert summary["records"] == 0
