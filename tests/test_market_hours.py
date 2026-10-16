from datetime import date, datetime, timezone

from ctbot import market_hours as mh


def test_session_open_in_new_york_time():
    assert mh.session_open(datetime(2026, 10, 13, 13, 30, tzinfo=timezone.utc))  # 9:30 EDT
    assert not mh.session_open(datetime(2026, 10, 13, 13, 29, tzinfo=timezone.utc))
    assert not mh.session_open(datetime(2026, 10, 13, 20, 0, tzinfo=timezone.utc))  # 16:00
    assert not mh.session_open(datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc))  # Sat
    assert mh.session_open(datetime(2026, 12, 1, 14, 30, tzinfo=timezone.utc))  # 9:30 EST


def test_trading_date_and_settlement_day():
    assert mh.trading_date(datetime(2026, 10, 14, 2, tzinfo=timezone.utc)) == date(2026, 10, 13)
    assert mh.next_business_day(date(2026, 10, 16)) == date(2026, 10, 19)  # Fri -> Mon
    assert mh.minutes_to_close(datetime(2026, 10, 13, 19, 45, tzinfo=timezone.utc)) == 15
