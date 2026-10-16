"""US equity regular-session clock (9:30-16:00 America/New_York, Mon-Fri).

No exchange holiday or half-day calendar: on those days the session is wrongly reported
open, and orders would be rejected by the broker or (in paper) fill against stale quotes.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)


def trading_date(ts: datetime) -> date:
    """Calendar date in New York for a tz-aware timestamp."""
    return ts.astimezone(ET).date()


def session_open(now: datetime) -> bool:
    et = now.astimezone(ET)
    return et.weekday() < 5 and OPEN <= et.time() < CLOSE


def minutes_to_close(now: datetime) -> float:
    et = now.astimezone(ET)
    close = datetime.combine(et.date(), CLOSE, tzinfo=ET)
    return (close - et).total_seconds() / 60


def next_business_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d
