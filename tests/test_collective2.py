"""Collective2: read-only allowlist, provider event mapping, ranking math, watchlist wiring."""

import json
from datetime import date, timedelta

import pytest

from ctbot.models import EventKind, Side
from ctbot.providers.collective2 import (
    READ_ENDPOINTS,
    C2Client,
    C2Forbidden,
    Collective2Provider,
    EndpointNotAllowed,
)
from ctbot.research import c2_rank

TODAY = date.today()


class _NoHttp:
    def request(self, *a, **k):
        raise AssertionError("no request may be sent")


def test_write_endpoints_are_refused_before_sending():
    c = C2Client("k", http=_NoHttp())
    for method, path in (("POST", "/Strategies/CreateSubscriptionCheckout"), ("POST", "/Autotrade/StartAutotrade"),
                         ("POST", "/v2/Strategies/SetDesiredPositions"), ("POST", "/Strategies/NewStrategyOrder"),
                         ("GET", "/Autotrade/GetAutoTradeOpenPositions")):
        with pytest.raises(EndpointNotAllowed):
            c.request(method, path)
    assert all(m == "GET" or p == "/General/Hello" for m, p in READ_ENDPOINTS)


class _FakeC2:
    def __init__(self, forbidden):
        self.forbidden = forbidden

    def open_positions(self, sid):
        if self.forbidden:
            raise C2Forbidden("403")
        return [{"Quantity": 10, "OpenedDate": f"{TODAY}T14:00:00Z", "ExchangeSymbol": {"Symbol": "aapl"}},
                {"Quantity": -5, "OpenedDate": f"{TODAY}T14:00:00Z", "ExchangeSymbol": {"Symbol": "TSLA"}}]

    def closed_trades(self, sid):
        recent, old = f"{TODAY}T15:00:00Z", f"{TODAY - timedelta(days=30)}T15:00:00Z"
        return [{"OpenSide": "1", "CloseDate": recent, "ExchangeSymbol": {"Symbol": "KO"}},
                {"OpenSide": "2", "CloseDate": recent, "ExchangeSymbol": {"Symbol": "NVDA"}},  # short: ignored
                {"OpenSide": "1", "CloseDate": old, "ExchangeSymbol": {"Symbol": "PEP"}}]       # too old


def test_provider_maps_longs_and_exits_and_survives_403():
    ev = Collective2Provider("k", [7], client=_FakeC2(False)).fetch({EventKind.INVESTOR_TRADE}, TODAY - timedelta(days=60))
    assert {(e.symbol, e.side) for e in ev} == {("AAPL", Side.BUY), ("KO", Side.SELL)}
    assert all(e.actor == "c2:7" for e in ev)
    ev = Collective2Provider("k", [7], client=_FakeC2(True)).fetch({EventKind.INVESTOR_TRADE}, TODAY - timedelta(days=60))
    assert [(e.symbol, e.side) for e in ev] == [("KO", Side.SELL)]


def _pts(values, end=TODAY):
    return [{"Date": f"{end - timedelta(days=len(values) - 1 - i)}T00:00:00", "EquityWithCosts": v}
            for i, v in enumerate(values)]


def test_equity_stats_rejects_bad_or_stale_data():
    assert c2_rank.equity_stats(_pts([100, -5] + [100] * 40), TODAY) is None
    assert c2_rank.equity_stats(_pts([100] * 40, TODAY - timedelta(days=30)), TODAY) is None
    s = c2_rank.equity_stats(_pts([100 + i for i in range(400)]), TODAY)
    assert s["r12m"] > 0 and s["maxdd_12m"] == 0


def test_trade_profile_long_share_and_hold():
    t = lambda side, days: {"OpenSide": side, "OpenDate": f"{TODAY - timedelta(days=days)}T14:00:00Z",  # noqa: E731
                            "CloseDate": f"{TODAY}T14:00:00Z", "ProfitLoss": 1, "ExchangeSymbol": {"Symbol": "X"}}
    p = c2_rank.trade_profile([t("1", 2), t("1", 4), t("2", 6), t("1", 8)], TODAY)
    assert p["trades_12m"] == 4 and p["long_share"] == 0.75 and p["median_hold_days"] == 5


def test_registry_expands_c2_watchlist(tmp_path, monkeypatch):
    from ctbot.strategies.registry import build_strategies

    wl = tmp_path / "watchlist.json"
    wl.write_text(json.dumps({"strategies": [{"strategy_id": 11, "name": "A"}, {"strategy_id": 22, "name": "B"}]}))
    monkeypatch.setattr(c2_rank, "WATCHLIST", wl)
    ws = build_strategies({"copy_trading": {"enabled": True, "params": {"tracked_actors": ["c2:watchlist", "me"]}}})
    assert ws[0].strategy.tracked_actors == {"me", "c2:11", "c2:22"}
