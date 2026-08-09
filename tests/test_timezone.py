from datetime import datetime

from ashare_decision_support.schedule.timezone import BEIJING_TZ, beijing_now, beijing_today


def test_beijing_clock_is_explicit_and_date_matches_the_clock() -> None:
    now = beijing_now()

    assert now.tzinfo == BEIJING_TZ
    assert beijing_today() == now.date()
    assert now.utcoffset() == datetime(2026, 1, 1, tzinfo=BEIJING_TZ).utcoffset()
