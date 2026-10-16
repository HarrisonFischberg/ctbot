"""Read-only Robinhood market data: allowlist, parsing, caching, token file. No network."""

import asyncio
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ctbot.market_data.robinhood_mcp_client import (
    READ_ONLY_TOOLS,
    ReadOnlyRobinhoodClient,
    ToolNotAllowed,
    _FileTokenStorage,
)
from ctbot.market_data.robinhood_readonly import RobinhoodReadOnlyMarketData, parse_bars

NOW = datetime(2026, 10, 13, 19, 58, tzinfo=timezone.utc)  # Tue 15:58 ET, in session

QUOTES = {"data": {"results": [
    {"quote": {"symbol": "SPY", "last_trade_price": "778.04",
               "venue_last_trade_time": "2026-10-13T19:57:59.999413974Z",
               "bid_price": "778.00", "venue_bid_time": "2026-10-13T19:57:58.937Z",
               "ask_price": "778.06", "has_traded": True, "state": "active"}},
    {"quote": {"symbol": "KO", "last_trade_price": "90.10", "bid_price": "90.09",
               "venue_bid_time": "2026-10-13T19:30:00Z", "ask_price": "90.11",  # stale
               "has_traded": True, "state": "active"}},
]}}

DAILY = {"data": {"results": [{"symbol": "SPY", "bars": [
    {"begins_at": "2026-10-09T00:00:00Z", "open_price": "776.24", "close_price": "778.57",
     "high_price": "779.42", "low_price": "775.14", "volume": 22624488},
    {"begins_at": "2026-10-12T00:00:00Z", "open_price": "778.78", "close_price": "778.10",
     "high_price": "779.39", "low_price": "777.05", "volume": 18685018},
    {"begins_at": "2026-10-13T00:00:00Z", "open_price": "778.1", "close_price": "778.1",
     "high_price": "778.1", "low_price": "778.1", "volume": 0, "interpolated": True},
]}]}}


INTRADAY = {"data": {"results": [{"symbol": "SPY", "bars": [
    {"begins_at": "2026-10-12T13:30:00Z", "open_price": "1", "close_price": "1", "high_price": "1",
     "low_price": "1", "volume": 1},  # already has a real daily bar: ignored
    {"begins_at": "2026-10-13T13:30:00Z", "open_price": "778.10", "close_price": "778.50",
     "high_price": "779.00", "low_price": "777.90", "volume": 1000},
    {"begins_at": "2026-10-13T19:55:00Z", "open_price": "778.40", "close_price": "778.01",
     "high_price": "778.60", "low_price": "777.70", "volume": 2000},
]}]}}


class _Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, calls):
        self.calls.extend(calls)
        out = []
        for tool, args in calls:
            if tool == "get_equity_quotes":
                out.append(QUOTES)
            else:
                out.append(INTRADAY if args.get("interval") == "5minute" else DAILY)
        return out


def test_allowlist_has_no_order_tools_and_client_refuses_them():
    assert not any(t.startswith(("place_", "cancel_", "review_", "exercise_")) for t in READ_ONLY_TOOLS)
    client = ReadOnlyRobinhoodClient(token_path=Path("/nonexistent"))
    for tool in ("review_equity_order", "cancel_equity_order", "update_watchlist"):
        with pytest.raises(ToolNotAllowed):
            client.call(tool, {})


def test_quotes_drop_stale_and_parse_nanosecond_times():
    fake = _Fake()
    md = RobinhoodReadOnlyMarketData(fake, max_quote_age_s=120, clock=lambda: NOW)
    q = md.quotes(["spy", "KO"])
    assert set(q) == {"SPY"}  # KO's bid is 28 minutes old
    assert (q["SPY"].bid, q["SPY"].ask, q["SPY"].last) == (778.0, 778.06, 778.04)
    md.quotes(["SPY"])  # within TTL: served from cache
    assert len(fake.calls) == 1


def test_daily_bars_drop_interpolated_and_rebuild_missing_day_from_intraday():
    later = datetime(2026, 10, 14, 15, tzinfo=timezone.utc)  # next morning; Oct 13 still interpolated
    fake = _Fake()
    md = RobinhoodReadOnlyMarketData(fake, clock=lambda: later)
    bars = md.daily_bars("SPY", 10)
    assert [b.ts.date().isoformat() for b in bars] == ["2026-10-09", "2026-10-12", "2026-10-13"]
    rebuilt = bars[-1]
    assert (rebuilt.open, rebuilt.high, rebuilt.low, rebuilt.close, rebuilt.volume) == (778.10, 779.0, 777.7, 778.01, 3000)
    md.daily_bars("SPY", 1)
    assert len(fake.calls) == 2  # one daily + one intraday call, then cached for the day


def test_daily_bars_exclude_today_during_session():
    fake = _Fake()
    md = RobinhoodReadOnlyMarketData(fake, clock=lambda: NOW)  # Oct 13 in session
    assert [b.ts.date().isoformat() for b in md.daily_bars("SPY", 10)] == ["2026-10-09", "2026-10-12"]
    assert parse_bars(DAILY)["SPY"][-1].close == 778.10


def test_token_file_is_private():
    from mcp.shared.auth import OAuthToken

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "sub" / "tok.json"
        storage = _FileTokenStorage(path)
        asyncio.run(storage.set_tokens(OAuthToken(access_token="x", refresh_token="y")))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert asyncio.run(storage.get_tokens()).refresh_token == "y"


def test_oauth_provider_builds_without_network():
    ReadOnlyRobinhoodClient(token_path=Path("/nonexistent"))._auth()


def test_callback_passes_iss_through():
    import threading
    import time
    import urllib.request

    from ctbot.market_data import robinhood_mcp_client as m

    out = {}
    t = threading.Thread(target=lambda: out.update(r=m._wait_for_callback(timeout_s=10)))
    t.start()
    for _ in range(50):  # wait for the server to bind
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{m.CALLBACK_PORT}/callback?code=c1&state=s1"
                "&iss=https%3A%2F%2Fexample.test", timeout=2).read()
            break
        except OSError:
            time.sleep(0.1)
    t.join(5)
    assert out["r"] == ("c1", "s1", "https://example.test")


def test_warm_batches_daily_fetch_for_universe():
    fake = _Fake()
    md = RobinhoodReadOnlyMarketData(fake, clock=lambda: NOW)
    md.warm(["SPY", "KO", "MSFT"])
    md.daily_bars("SPY", 5)
    day_calls = [a for t, a in fake.calls if t == "get_equity_historicals" and a.get("interval") == "day"]
    assert len(day_calls) == 1 and set(day_calls[0]["symbols"]) == {"SPY", "KO", "MSFT"}
