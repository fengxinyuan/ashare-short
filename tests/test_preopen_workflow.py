"""真实盘前研报组包：冷启动、缓存缺失、时点校验和可证伪假设。"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from ashare_decision_support import cli
from ashare_decision_support.mcp_server import handle
from ashare_decision_support.research.hypotheses import build_sector_hypotheses
from ashare_decision_support.research.preopen import build_preopen_research, render_preopen_research
from ashare_decision_support.research.sector_forecast import execution_protocol

MOMENT = datetime(2026, 9, 11, 8, 30)


def _morning(stamp="2026-09-11"):
    return {
        "trade_date": stamp,
        "market": {"direction": "偏强", "score": 72},
        "sectors": [
            {
                "name": name,
                "level": "THS_INDUSTRY",
                "score": score,
                "ret_1d": 0.01,
                "ret_5d": 0.03,
                "breadth": 0.7,
            }
            for name, score in [
                ("电子", 82),
                ("芯片概念", 81),
                ("有色金属", 75),
                ("医药生物", 70),
                ("银行", 65),
            ]
        ],
    }


@pytest.mark.parametrize(
    "morning", [None, "{broken-json", "[]", '{"trade_date":"2026-09-11","sectors":[]}']
)
def test_cold_or_broken_cache_returns_a_degraded_research_pack(tmp_path, morning):
    if morning is not None:
        (tmp_path / "morning_latest.json").write_text(morning, encoding="utf-8")
    payload = build_preopen_research(output_dir=tmp_path, moment=MOMENT)
    assert payload["facts"]["data_status"] == "降级"
    assert payload["hypotheses"]["items"] == []
    assert payload["facts"]["notes"]
    assert "# A股盘前情景报告" in render_preopen_research(payload)


def test_current_morning_works_without_an_optional_strategy_scan(tmp_path):
    (tmp_path / "morning_latest.json").write_text(json.dumps(_morning()), encoding="utf-8")
    payload = build_preopen_research(output_dir=tmp_path, moment=MOMENT)
    assert payload["hypotheses"]["target_session"] == "2026-09-11"
    assert len(payload["hypotheses"]["items"]) == 3
    assert "参数化策略扫描" not in payload["facts"]["facts"]
    assert any("参数化策略扫描" in note for note in payload["facts"]["notes"])
    rendered = render_preopen_research(payload)
    assert rendered.count("观察 科技硬件") == 1
    for item in payload["hypotheses"]["items"]:
        assert all(condition in rendered for condition in item["confirm"] + item["invalidate"])
    assert "失效看冲高回落" not in rendered


@pytest.mark.parametrize("stamp", ["2026-09-10", "2026-09-14", "bad-date", None])
def test_historical_future_or_undated_morning_is_not_a_current_hypothesis(tmp_path, stamp):
    (tmp_path / "morning_latest.json").write_text(json.dumps(_morning(stamp)), encoding="utf-8")
    payload = build_preopen_research(output_dir=tmp_path, moment=MOMENT)
    assert payload["hypotheses"]["items"] == []
    assert any("日期" in note for note in payload["hypotheses"]["notes"])
    assert payload["facts"]["facts"]["盘前主链路"]["sectors"]
    rendered = render_preopen_research(payload)
    assert "- 观察 " not in rendered
    assert payload["hypotheses"]["notes"][0] in rendered


def test_hypothesis_session_is_the_beijing_date_of_an_aware_input(tmp_path):
    (tmp_path / "morning_latest.json").write_text(json.dumps(_morning()), encoding="utf-8")
    payload = build_preopen_research(
        output_dir=tmp_path,
        moment=datetime(2026, 9, 10, 23, 30, tzinfo=UTC),
    )
    assert payload["hypotheses"]["target_session"] == "2026-09-11"
    assert payload["as_of"] == "2026-09-11T07:30:00+08:00"


@pytest.mark.parametrize("moment", [datetime(2026, 9, 13, 20, 0), datetime(2026, 9, 14, 8, 30)])
def test_a_weekend_plan_is_the_current_hypothesis_for_monday(tmp_path, moment):
    """周日跑的 `morning` 是给周一的：周日晚上和周一盘前的研究都该拿它立假设。

    比的是两边对应的**交易日**，不是日历日：周日的计划和周一的研究都对着周一；
    上周五的计划则不是（上面那个用例）。
    """
    (tmp_path / "morning_latest.json").write_text(
        json.dumps(_morning("2026-09-13")), encoding="utf-8"
    )
    payload = build_preopen_research(output_dir=tmp_path, moment=moment)
    assert payload["hypotheses"]["target_session"] == "2026-09-14"
    assert len(payload["hypotheses"]["items"]) == 3


def test_hypotheses_keep_opposing_evidence_and_do_not_rescore_from_overseas():
    morning = _morning()
    before = deepcopy(morning)
    overseas = {
        "sector_links": {
            "links": [
                {
                    "instrument_name": "费半",
                    "change_pct": -2,
                    "direction": "bearish",
                    "targets": ["半导体"],
                },
                {
                    "instrument_name": "韩股",
                    "change_pct": 3,
                    "direction": "bullish",
                    "targets": ["存储芯片"],
                },
                {
                    "instrument_name": "粮食",
                    "change_pct": 4,
                    "direction": "bullish",
                    "targets": ["农业", "农业"],
                },
            ]
        },
        "sector_base_rates": {
            "items": [
                {"sector": "电子", "trusted": False, "samples": 100, "headline": "未达可信门槛"},
                {"sector": "半导体", "trusted": True, "samples": 30, "headline": "30 个历史样本"},
                {"sector": "存储芯片", "trusted": True, "samples": 40, "headline": "40 个历史样本"},
            ]
        },
    }
    payload = build_sector_hypotheses(morning, overseas)
    plain = build_sector_hypotheses(morning, None)
    assert [(row["name"], row["score"]) for row in payload["items"]] == [
        (row["name"], row["score"]) for row in plain["items"]
    ]
    tech = next(row for row in payload["items"] if row["name"] == "科技硬件")
    assert tech["members"] == ["电子", "芯片概念"]
    assert tech["overseas"]["conflicted"] is True
    assert tech["base_rates"] == ["40 个历史样本", "30 个历史样本"]
    assert {row["kind"] for row in tech["evidence"]} == {"已发生", "历史统计", "推演"}
    assert tech["invalidate"] == execution_protocol()["invalidate"]
    assert tech["confirm"] == execution_protocol()["confirm"]
    assert [row["sector"] for row in payload["overseas_only"]] == ["农业"]
    assert morning == before


def test_real_cli_and_mcp_preopen_entries_work_from_empty_state(tmp_path, capsys):
    args = cli.build_parser().parse_args(
        ["preopen-research", "--no-global", "--output-dir", str(tmp_path)]
    )
    cli.COMMANDS[args.command](args)
    assert json.loads(capsys.readouterr().out)["hypotheses"]["items"] == []
    response = handle(
        {
            "jsonrpc": "2.0",
            "id": "cold-start",
            "method": "tools/call",
            "params": {
                "name": "preopen_research_context",
                "arguments": {"output_dir": str(tmp_path), "include_global": False},
            },
        }
    )
    assert response["result"]["isError"] is False
    assert response["result"]["structuredContent"]["facts"]["data_status"] == "降级"
