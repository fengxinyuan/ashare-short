from __future__ import annotations

import json
import sys
from datetime import date

import pytest

from ashare_decision_support import cli
from ashare_decision_support.state.positions import Positions, unrealised_pct


def test_cost_update_preserves_open_date_quantity_and_risk_plan(tmp_path) -> None:
    book = Positions(path=tmp_path / "positions.json")
    book.open(
        "600519",
        cost_price=100,
        shares=300,
        opened_on=date(2026, 8, 3),
        plan_stop=95,
        plan_target=110,
        note="原有计划",
    )

    book.open("sh600519", cost_price=101, today=date(2026, 9, 12))

    row = Positions(path=book.path).get("600519")
    assert row.cost_price == 101
    assert row.opened_on == "2026-08-03"
    assert row.shares == 300
    assert (row.plan_stop, row.plan_target, row.note) == (95, 110, "原有计划")


def test_open_date_and_quantity_can_be_changed_explicitly(tmp_path) -> None:
    book = Positions(path=tmp_path / "positions.json")
    book.open("600519", cost_price=100, shares=300, opened_on=date(2026, 8, 3))

    book.open("600519", cost_price=101, shares=500, opened_on=date(2026, 8, 10))

    assert book.get("600519").opened_on == "2026-08-10"
    assert book.get("600519").shares == 500


@pytest.mark.parametrize(
    "changes",
    [
        {"cost_price": 0},
        {"cost_price": -1},
        {"cost_price": float("nan")},
        {"cost_price": float("inf")},
        {"cost_price": True},
        {"cost_price": 10**1000},
        {"shares": 0},
        {"shares": -100},
        {"shares": 100.5},
        {"shares": True},
        {"plan_stop": -1},
        {"plan_target": "nan"},
        {"opened_on": "bad-date"},
    ],
)
def test_invalid_update_keeps_both_memory_and_disk_unchanged(tmp_path, changes) -> None:
    book = Positions(path=tmp_path / "positions.json")
    book.open("600519", cost_price=100, shares=100, plan_stop=95)
    before = book.get("600519").to_dict()
    before_bytes = book.path.read_bytes()

    with pytest.raises(ValueError):
        book.open("600519", **{"cost_price": 101, **changes})

    assert book.get("600519").to_dict() == before
    assert book.path.read_bytes() == before_bytes


@pytest.mark.parametrize("symbol", ["", "NOTACODE", "123.SH", "600519.US"])
def test_invalid_symbol_cannot_create_a_position(tmp_path, symbol) -> None:
    book = Positions(path=tmp_path / "positions.json")

    with pytest.raises(ValueError, match="代码"):
        book.open(symbol, cost_price=100)

    assert book.entries == []
    assert not book.path.exists()


@pytest.mark.parametrize("action", ["open", "close", "clear"])
def test_write_failure_does_not_change_in_memory_holdings(tmp_path, monkeypatch, action) -> None:
    book = Positions(path=tmp_path / "positions.json")
    book.open("600519", cost_price=100)
    before = book.path.read_bytes()

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as broken_disk:
        broken_disk.setattr("ashare_decision_support.state.positions.write_json", fail_write)
        with pytest.raises(OSError, match="disk full"):
            if action == "open":
                book.open("600519", cost_price=101)
            elif action == "close":
                book.close("600519")
            else:
                book.clear()

    assert book.get("600519").cost_price == 100
    assert book.path.read_bytes() == before
    book.open("600519", cost_price=102)
    assert Positions(book.path).get("600519").cost_price == 102


def test_hand_edited_numeric_fields_are_normalized_without_losing_other_positions(tmp_path) -> None:
    path = tmp_path / "positions.json"
    path.write_text(
        json.dumps(
            [
                {"symbol": "600519", "cost_price": "100", "shares": "300", "plan_stop": "95"},
                {"symbol": "000001", "cost_price": "nan", "shares": -1, "plan_target": "oops"},
                {"symbol": "not-a-stock", "cost_price": 10},
                None,
            ]
        ),
        encoding="utf-8",
    )

    book = Positions(path=path)

    assert book.symbols() == ["600519.SH", "000001.SZ"]
    assert book.get("600519").unrealised_pct(110) == 0.1
    assert book.get("600519").shares == 300
    assert book.get("600519").plan_stop == 95
    assert book.get("000001").cost_price is None
    assert book.get("000001").shares is None
    assert book.get("000001").plan_target is None


@pytest.mark.parametrize("cost, price", [(0, 10), (-1, 10), ("nan", 10), (10, True), (10, "bad")])
def test_invalid_prices_do_not_generate_profit_readings(cost, price) -> None:
    assert unrealised_pct(cost, price) is None


def test_cli_reports_invalid_cost_without_a_traceback(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ASHARE_DS_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "load_env_files", lambda: None)
    monkeypatch.setattr(sys, "argv", ["ashare-ds", "position", "open", "600519", "--cost", "nan"])

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 2
    assert "成本价必须是大于 0 的有限数字" in capsys.readouterr().err
    assert not (tmp_path / "positions.json").exists()


def test_stale_books_merge_new_positions_and_preserve_updated_fields(tmp_path) -> None:
    path = tmp_path / "positions.json"
    first, second = Positions(path), Positions(path)
    first.open("600519", 100, shares=100, plan_stop=95)
    second.open("000001", 12)
    second.open("600519", 101, shares=300, plan_stop=96)

    first.open("600519", 102)

    reloaded = Positions(path)
    assert reloaded.symbols() == ["600519.SH", "000001.SZ"]
    assert reloaded.get("600519").shares == 300
    assert reloaded.get("600519").plan_stop == 96
    assert reloaded.get("600519").cost_price == 102


def test_stale_close_preserves_new_positions_and_does_not_restore_closed_ones(tmp_path) -> None:
    path = tmp_path / "positions.json"
    first = Positions(path)
    first.open("600519", 100)
    first.open("000001", 12)
    second = Positions(path)
    second.close("600519")
    second.open("300750", 200)

    assert first.close("000001") is True
    assert first.symbols() == ["300750.SZ"]
    assert Positions(path).symbols() == ["300750.SZ"]
    assert second.close("000001") is False
    assert second.symbols() == ["300750.SZ"]


def test_stale_clear_counts_the_current_positions(tmp_path) -> None:
    path = tmp_path / "positions.json"
    first, second = Positions(path), Positions(path)
    second.open("600519", 100)
    second.open("000001", 12)

    assert first.clear() == 2
    assert Positions(path).symbols() == []


def test_cli_close_normalizes_and_deduplicates_results(monkeypatch, capsys) -> None:
    Positions().open("600519", 100)
    monkeypatch.setattr(cli, "load_env_files", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ashare-ds",
            "position",
            "close",
            "600519",
            "sh600519",
            "000001",
            "sz000001",
        ],
    )

    cli.main()

    result = json.loads(capsys.readouterr().out)
    assert result == {"closed": ["600519.SH"], "not_found": ["000001.SZ"], "total": 0}
