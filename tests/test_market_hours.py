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


def test_nyse_holidays_match_the_published_schedule():
    assert sorted(mh.holidays(2026)) == [
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
    ]
    assert sorted(mh.holidays(2027)) == [
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
        date(2027, 11, 25), date(2027, 12, 24),
    ]
    assert date(2025, 1, 9) in mh.holidays(2025)  # unscheduled: day of mourning
    assert date(2027, 12, 31) not in mh.holidays(2027)  # Sat Jan 1 2028 isn't made up


def test_closed_on_holidays_and_early_on_half_days():
    assert not mh.session_open(datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc))  # Thanksgiving
    assert mh.early_closes(2026) == {date(2026, 11, 27), date(2026, 12, 24)}
    assert mh.session_open(datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc))  # 12:59 EST
    assert not mh.session_open(datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc))  # 13:00 EST
    assert mh.minutes_to_close(datetime(2026, 11, 27, 17, 45, tzinfo=timezone.utc)) == 15


def test_next_business_day_skips_holidays():
    assert mh.next_business_day(date(2026, 11, 25)) == date(2026, 11, 27)  # Wed -> Fri
    assert mh.next_business_day(date(2026, 12, 31)) == date(2027, 1, 4)  # New Year's Fri


def test_next_open():
    et = mh.ET
    fri_close = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)
    assert mh.next_open(fri_close) == datetime(2026, 10, 19, 9, 30, tzinfo=et)
    early = datetime(2026, 10, 13, 11, 0, tzinfo=timezone.utc)  # Tue 7:00 EDT
    assert mh.next_open(early) == datetime(2026, 10, 13, 9, 30, tzinfo=et)
    during = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)
    assert mh.next_open(during) == datetime(2026, 10, 14, 9, 30, tzinfo=et)
    before_thanksgiving = datetime(2026, 11, 25, 22, 0, tzinfo=timezone.utc)
    assert mh.next_open(before_thanksgiving) == datetime(2026, 11, 27, 9, 30, tzinfo=et)
    # across the DST change: Fri Oct 30 EDT -> Mon Nov 2 9:30 EST = 14:30 UTC
    assert mh.next_open(datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)) == datetime(
        2026, 11, 2, 14, 30, tzinfo=timezone.utc)


def test_last_close():
    et = mh.ET
    assert mh.last_close(datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)) == datetime(2026, 10, 16, 16, 0, tzinfo=et)
    assert mh.last_close(datetime(2026, 10, 16, 20, 30, tzinfo=timezone.utc)) == datetime(2026, 10, 16, 16, 0, tzinfo=et)
    assert mh.last_close(datetime(2026, 10, 16, 19, 0, tzinfo=timezone.utc)) == datetime(2026, 10, 15, 16, 0, tzinfo=et)
    # Thanksgiving: the last close was Wednesday's
    assert mh.last_close(datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 25, 16, 0, tzinfo=et)
    assert mh.last_close(datetime(2026, 11, 27, 19, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 27, 13, 0, tzinfo=et)
