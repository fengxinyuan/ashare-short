from datetime import datetime

import pytest

from ashare_decision_support.news.dedup import parse_published


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-01 09:31:20", datetime(2026, 8, 1, 9, 31, 20)),
        ("2026-08-01 09:31", datetime(2026, 8, 1, 9, 31)),
        ("2026-08-01", datetime(2026, 8, 1)),
        ("2026/08/01 09:31:20", datetime(2026, 8, 1, 9, 31, 20)),
        ("2026/08/01 09:31", datetime(2026, 8, 1, 9, 31)),
        ("2026/08/01", datetime(2026, 8, 1)),
        ("20260801093120", datetime(2026, 8, 1, 9, 31, 20)),
        ("20260801", datetime(2026, 8, 1)),
        # strptime 历史上会接受非补零日期，慢速兼容路径保留该语义。
        ("2026-8-1", datetime(2026, 8, 1)),
    ],
)
def test_parse_published_supports_source_formats(raw: str, expected: datetime) -> None:
    assert parse_published(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not-a-date",
        "2026-02-30",
        "2026-08-01T09:31",
        "2026-08-01 09-31",
        "202608010931",
    ],
)
def test_parse_published_rejects_unknown_or_invalid_shapes(raw: str) -> None:
    assert parse_published(raw) is None


def test_parse_published_discards_fractional_seconds_and_timezone_suffix() -> None:
    expected = datetime(2026, 8, 1, 9, 31, 20)

    assert parse_published("2026-08-01T09:31:20.123+08:00") == expected
    assert parse_published("2026-08-01 09:31:20+08:00") == expected
