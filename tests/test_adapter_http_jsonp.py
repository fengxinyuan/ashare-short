"""JSONP 剥壳：同花顺板块指数与新浪美股日线共用的那一步。"""

from __future__ import annotations

import pytest

from ashare_decision_support.adapters.http import jsonp_payload


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            'quotebridge_v6_line_bk_881101_01_last({"name": "半导体", "data": "x"});',
            {"name": "半导体", "data": "x"},
        ),
        ('var _ashare_ds([{"d": "2026-08-19", "c": 1.5}]);\n', [{"d": "2026-08-19", "c": 1.5}]),
        ('cb( {"nested": {"call(x)": 1}} )', {"nested": {"call(x)": 1}}),
        ("cb(null)", None),
    ],
)
def test_jsonp_payload_returns_the_inner_json_value(text, expected) -> None:
    assert jsonp_payload(text) == expected


@pytest.mark.parametrize(
    "text", ["", "not jsonp at all", "cb({broken)", '{"plain": "json"}', "cb()"]
)
def test_jsonp_payload_rejects_text_without_a_json_shell(text) -> None:
    assert jsonp_payload(text) is None
