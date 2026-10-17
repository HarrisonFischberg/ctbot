"""US equity regular-session clock (9:30-16:00 America/New_York) with the NYSE calendar.

Full-day holidays and 13:00 early closes are computed from the exchange's rules. Unscheduled
closures (a national day of mourning, a storm) can't be: add them to UNSCHEDULED_CLOSURES when
the exchange announces one, or the session is wrongly reported open that day.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

UNSCHEDULED_CLOSURES = frozenset({
    date(2018, 12, 5),  # President G. H. W. Bush, day of mourning
    date(2025, 1, 9),  # President Carter, day of mourning
})


def trading_date(ts: datetime) -> date:
    """Calendar date in New York for a tz-aware timestamp."""
    return ts.astimezone(ET).date()


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Mon=0) of a month."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _observed(d: date) -> date:
    """A Saturday holiday is observed on Friday, a Sunday one on Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@cache
def holidays(year: int) -> frozenset[date]:
    """Weekdays the NYSE is closed all day in a calendar year."""
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    days = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _nth_weekday(year, 6, 0, 1) - timedelta(days=7),  # Memorial Day: last Monday of May
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        thanksgiving,
        _observed(date(year, 12, 25)),
    }
    if date(year, 1, 1).weekday() != 5:  # a Saturday New Year's Day isn't moved to Dec 31
        days.add(_observed(date(year, 1, 1)))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    days |= {d for d in UNSCHEDULED_CLOSURES if d.year == year}
    return frozenset(days)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


@cache
def early_closes(year: int) -> frozenset[date]:
    """Days the NYSE closes at 13:00 ET: July 3, the day after Thanksgiving and Christmas Eve,
    each only when it is a trading day."""
    days = {date(year, 7, 3), _nth_weekday(year, 11, 3, 4) + timedelta(days=1), date(year, 12, 24)}
    return frozenset(d for d in days if is_trading_day(d))


def close_time(d: date) -> time:
    return EARLY_CLOSE if d in early_closes(d.year) else CLOSE


def session_open(now: datetime) -> bool:
    et = now.astimezone(ET)
    d = et.date()
    return is_trading_day(d) and OPEN <= et.time() < close_time(d)


def minutes_to_close(now: datetime) -> float:
    et = now.astimezone(ET)
    close = datetime.combine(et.date(), close_time(et.date()), tzinfo=ET)
    return (close - et).total_seconds() / 60


def next_business_day(d: date) -> date:
    """The next trading day after `d`, skipping weekends and exchange holidays."""
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def last_close(now: datetime) -> datetime:
    """When the most recent session ended: today's close once it has passed, else the previous
    trading day's."""
    et = now.astimezone(ET)
    d = et.date()
    if not (is_trading_day(d) and et.time() >= close_time(d)):
        d -= timedelta(days=1)
        while not is_trading_day(d):
            d -= timedelta(days=1)
    return datetime.combine(d, close_time(d), tzinfo=ET)


def next_open(now: datetime) -> datetime:
    """When the next session opens: today's 9:30 if it's still ahead, else the next trading day's."""
    et = now.astimezone(ET)
    d = et.date()
    if not (is_trading_day(d) and et.time() < OPEN):
        d = next_business_day(d)
    return datetime.combine(d, OPEN, tzinfo=ET)
