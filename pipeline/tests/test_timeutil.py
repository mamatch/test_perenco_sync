from datetime import date

from pipeline.timeutil import is_active


def test_conventional_active_open_ended():
    assert is_active(date(2020, 1, 1), None, date(2026, 9, 15))


def test_conventional_not_yet_started():
    assert not is_active(date(2026, 9, 16), None, date(2026, 9, 15))


def test_conventional_starts_today_is_active():
    assert is_active(date(2026, 9, 15), None, date(2026, 9, 15))


def test_conventional_ends_today_is_inactive():
    # date_end > as_of required; ending exactly today is no longer active.
    assert not is_active(date(2020, 1, 1), date(2026, 9, 15), date(2026, 9, 15))


def test_conventional_ends_tomorrow_is_active():
    assert is_active(date(2020, 1, 1), date(2026, 9, 16), date(2026, 9, 15))


def test_conventional_ended_yesterday_is_inactive():
    assert not is_active(date(2020, 1, 1), date(2026, 9, 14), date(2026, 9, 15))
