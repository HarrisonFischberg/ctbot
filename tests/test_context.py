"""Market context for reviews and the never-hold-through-earnings rule."""

from datetime import date, datetime, timezone

from ctbot.config import RiskLimits
from ctbot.market_data.context import MarketContext, report_datetime
from ctbot.models import Candidate, Decision, Direction, Position, Quote, Signal
from ctbot.risk.manager import AccountState, RiskManager

NOW = datetime(2026, 10, 14, 15, tzinfo=timezone.utc)  # Wed 11:00 ET


def _fake(calls):
    out = []
    for tool, args in calls:
        if tool == "get_earnings_calendar":
            out.append({"data": {"results": [
                {"symbol": "jpm", "report": {"date": "2026-10-15", "timing": "am"}},
                {"symbol": "JPM", "report": {"date": "2026-11-20", "timing": "am"}},
                {"symbol": "OLD", "report": {"date": "2026-10-01", "timing": "pm"}}]}})
        else:
            out.append({"data": {"results": [{"symbol": "JPM", "ratings": {
                "num_buy_ratings": 10, "num_hold_ratings": 5, "num_sell_ratings": 1, "mean_price_target": "330"}}]}})
    return out


def test_next_earnings_and_snapshot():
    ctx = MarketContext(_fake, None, clock=lambda: NOW)
    assert ctx.next_earnings("JPM") == report_datetime(date(2026, 10, 15), "am")
    assert ctx.next_earnings("OLD") is None and ctx.next_earnings("KO") is None
    snap = ctx.for_symbol("JPM")
    assert snap["next_earnings"]["days_away"] == 1 and snap["analysts"]["buy"] == 10
    assert snap["now_et"].startswith("2026-10-14 11:00 ET")


def _cand(exit_at):
    sig = Signal("overnight", "JPM", Direction.LONG, 0.6, 1.0, {"stop": 290, "target": None, "exit_at": exit_at})
    return Candidate("JPM", Direction.LONG, 0.5, (sig,))


def test_overnight_hold_refused_when_earnings_before_exit():
    rm = RiskManager(RiskLimits())
    report = report_datetime(date(2026, 10, 15), "am")
    st = AccountState(250, 250, 250, 250, 0)
    args = (Decision(True, 0.9, "", 50), st, Quote("JPM", 300, 300, 300), 1e9)
    v = rm.check_entry(_cand("2026-10-15T09:35:00-04:00"), *args, next_earnings=report, now=NOW)
    assert "earnings_before_exit" in v.reasons
    v = rm.check_entry(_cand("2026-10-14T15:50:00-04:00"), *args, next_earnings=report, now=NOW)
    assert v.allowed  # day trade closes before the report


def test_swing_refused_through_earnings():
    rm = RiskManager(RiskLimits())
    st = AccountState(250, 250, 250, 250, 1, brackets={"JPM": {"stop": 295, "target": None,
                      "exit_at": "2026-10-14T15:50:00-04:00", "model": "orb"}})
    at = datetime(2026, 10, 14, 19, 40, tzinfo=timezone.utc)
    report = report_datetime(date(2026, 10, 15), "am")
    assert not rm.maybe_swing(st, Position("JPM", 1, 300), Quote("JPM", 306, 306, 306), at, report)
    assert rm.maybe_swing(st, Position("JPM", 1, 300), Quote("JPM", 306, 306, 306), at, None)
