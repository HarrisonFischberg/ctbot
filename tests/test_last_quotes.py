"""Dashboard prices and chart candles stop fetching while the market is closed."""

from datetime import datetime, timedelta, timezone

from ctbot.dashboard import candles as candles_mod
from ctbot.dashboard.last_quotes import SessionQuotes
from ctbot.models import Bar, Quote

UTC = timezone.utc
FRI_1555 = datetime(2026, 10, 16, 19, 55, tzinfo=UTC)  # 15:55 ET, open
FRI_1630 = datetime(2026, 10, 16, 20, 30, tzinfo=UTC)  # closed
SAT = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)
MON_0931 = datetime(2026, 10, 19, 13, 31, tzinfo=UTC)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Feed:
    def __init__(self, clock, fail=False):
        self.clock, self.fail, self.calls = clock, fail, []

    def __call__(self, symbols):
        self.calls.append(list(symbols))
        if self.fail:
            raise RuntimeError("sign-in needed")
        return {s: Quote(s, 99.0, 101.0, 100.0, self.clock()) for s in symbols}


def test_prices_are_saved_while_open_and_shown_while_closed(tmp_path):
    path, clock = tmp_path / "last_quotes.json", Clock(FRI_1555)
    feed = Feed(clock)
    q = SessionQuotes(feed, path, clock)
    assert q(["KO", "SPY"])["KO"].mid == 100.0 and len(feed.calls) == 1 and path.exists()

    for t in (FRI_1630, SAT):  # saved 5 minutes before the bell: no fetch after the close
        clock.t = t
        got = q(["KO", "SPY"])
        assert got["SPY"].mid == 100.0 and got["SPY"].ts == FRI_1555
    assert len(feed.calls) == 1

    restarted = SessionQuotes(feed, path, clock)  # a restarted dashboard reads the file
    assert restarted(["KO"])["KO"].last == 100.0 and len(feed.calls) == 1

    clock.t = MON_0931
    q(["KO"])
    assert len(feed.calls) == 2


def test_one_catch_up_fetch_when_saved_prices_predate_the_close(tmp_path):
    path, clock = tmp_path / "last_quotes.json", Clock(datetime(2026, 10, 16, 18, 0, tzinfo=UTC))  # 14:00 ET
    feed = Feed(clock)
    q = SessionQuotes(feed, path, clock)
    q(["KO"])
    clock.t = FRI_1630
    assert q(["KO", "NEW"])["NEW"].ts == FRI_1630  # KO is stale, NEW never priced
    assert feed.calls[-1] == ["KO", "NEW"]
    clock.t = SAT
    q(["KO", "NEW"])
    assert len(feed.calls) == 2


def test_failed_catch_up_shows_saved_prices_and_is_not_retried(tmp_path):
    path, clock = tmp_path / "last_quotes.json", Clock(datetime(2026, 10, 16, 18, 0, tzinfo=UTC))
    SessionQuotes(Feed(clock), path, clock)(["KO"])
    clock.t = FRI_1630
    feed = Feed(clock, fail=True)
    q = SessionQuotes(feed, path, clock)
    assert q(["KO"])["KO"].mid == 100.0
    assert q(["KO"])["KO"].mid == 100.0
    assert len(feed.calls) == 1


def test_chart_candles_fetched_after_the_close_are_kept_until_the_open(tmp_path, monkeypatch):
    mono = [0.0]
    monkeypatch.setattr(candles_mod.time, "monotonic", lambda: mono[0])
    calls = []

    class RH:
        def intraday_bars(self, symbols, start, interval="5minute"):
            calls.append(interval)
            t0 = datetime(2026, 10, 16, 13, 30, tzinfo=UTC)
            return {"KO": [Bar("KO", t0 + timedelta(minutes=5 * i), 50, 51, 49, 50.5, 100) for i in range(3)]}

    clock = Clock(FRI_1555)
    src = candles_mod.CandleSource(RH(), None, cache_root=tmp_path, clock=clock)
    src.get("KO", "1d")
    mono[0], clock.t = 120.0, FRI_1630  # fetched before the bell: one refresh after it
    src.get("KO", "1d")
    mono[0], clock.t = 1e6, SAT  # then nothing until the open
    src.get("KO", "1d")
    assert len(calls) == 2
    mono[0], clock.t = 2e6, MON_0931
    src.get("KO", "1d")
    assert len(calls) == 3
