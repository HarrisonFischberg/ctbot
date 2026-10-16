"""Backtest engine: exits, no lookahead, account rules, costs."""

from datetime import datetime, timedelta, timezone

from ctbot.models import Bar
from ctbot.research.backtest import Trade, allocate, orb, run_exit

T0 = datetime(2026, 10, 13, 13, 30, tzinfo=timezone.utc)  # 9:30 ET


def bars(prices, vol=1000):
    return [Bar("XYZ", T0 + timedelta(minutes=5 * i), o, h, l, c, vol) for i, (o, h, l, c) in enumerate(prices)]


def test_stop_beats_target_in_same_bar_and_gap_fills_at_open():
    b = bars([(100, 100, 100, 100), (100, 103, 97, 100)])
    assert run_exit(b, 1, 100, 98, 102, 2)[1:] == (98, "stop")
    gap = bars([(100, 100, 100, 100), (95, 96, 94, 95)])
    assert run_exit(gap, 1, 100, 98, 102, 2)[1:] == (95, "stop")
    flat = bars([(100, 100, 100, 100)] * 3)
    assert run_exit(flat, 1, 100, 98, 102, 2)[1:] == (100, "time")


def test_orb_enters_on_next_bar_open_not_signal_close():
    p = [(100, 101, 99, 100)] * 6 + [(100, 102, 100, 101.5)] + [(101.8, 102, 101.7, 101.9)] + [(101.9, 102, 101.8, 101.9)] * 70
    t = orb("XYZ", T0.date(), bars(p), {"rvol": {("XYZ", T0.date()): 2.0}})
    assert t and t[0].entry_px == 101.8 and t[0].entry_ts == T0 + timedelta(minutes=35)
    assert orb("XYZ", T0.date(), bars(p), {"rvol": {("XYZ", T0.date()): 1.0}}) == []  # low volume


def _t(sym, day, hour=15, overnight=False):
    e = datetime(2026, 10, day, hour, tzinfo=timezone.utc)
    x = e + (timedelta(days=1) if overnight else timedelta(minutes=30))
    return Trade("s", sym, e, 100, x, 101, "time", 1.0)


def test_allocate_enforces_day_trade_limit_and_slots():
    # Mon-Fri Oct 12-16: 5 day trades on 5 days -> only 3 allowed in 5 business days
    dts = [_t(f"S{i}", 12 + i) for i in range(5)]
    assert len(allocate(dts)) == 3
    # overnight trades are not day trades; 4 at once -> max 3 open
    ons = [_t(f"N{i}", 12, overnight=True) for i in range(4)]
    assert len(allocate(ons)) == 3


def test_costs_reduce_returns():
    t = Trade("s", "SPY", T0, 100, T0, 100, "time", 0)
    assert round(t.net_ret * 1e4, 1) == -4.0  # 2 bps each side for ETFs
