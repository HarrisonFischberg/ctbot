"""Hey Astral: insider-trade provider, news labels, read-only client, backtest-vs-live badge."""

from __future__ import annotations

import json
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ctbot import backtest_check
from ctbot.market_data.astral_client import READ_ONLY_TOOLS, AstralClient
from ctbot.market_data.astral_news import AstralNews, summarize
from ctbot.market_data.robinhood_mcp_client import ToolNotAllowed
from ctbot.models import EventKind, Side
from ctbot.providers.astral import AstralInsiderProvider, to_event
from ctbot.providers.registry import build_providers

FIX = json.loads((Path(__file__).parent / "fixtures" / "astral_observations.json").read_text())
NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)


def test_allowlist_is_research_reads_only():
    assert READ_ONLY_TOOLS == {"astral_research_fiscal_observations", "astral_symbol_resolve"}
    client = AstralClient("https://example.invalid/mcp")
    for tool in ("astral_order_submit", "astral_strategy_deploy", "astral_broker_connect_start",
                 "astral_saved_strategy_share", "astral_account_status"):
        with pytest.raises(ToolNotAllowed):
            client.call(tool, {})  # refused before any network or sign-in


def test_client_needs_https():
    with pytest.raises(ValueError):
        AstralClient("http://example.invalid/mcp")


def test_only_open_market_trades_become_events():
    buy, sell, tax = (to_event(r) for r in FIX["insiders"]["rows"])
    assert tax is None  # tax withholding is not a trading decision
    assert (buy.kind, buy.symbol, buy.side, buy.actor) == (EventKind.INSIDER_TRADE, "TSLA", Side.BUY, "Elon Musk")
    assert (buy.event_date, buy.disclosed_date, round(buy.size_usd)) == (date(2025, 9, 12), date(2025, 9, 15), 999959042)
    assert (sell.symbol, sell.side, sell.disclosure_lag_days) == ("AAPL", Side.SELL, 2)


def test_bad_rows_are_skipped():
    assert to_event({"event_type": "open_market_purchase", "symbol": "X", "payload": {}}) is None
    assert to_event({"event_type": "open_market_purchase", "payload": {"transaction_date": "nope"}}) is None


class FakeCalls:
    def __init__(self, result=None, fail=False):
        self.calls, self.result, self.fail = [], result, fail

    def __call__(self, calls):
        calls = list(calls)
        self.calls.append(calls)
        if self.fail:
            raise ConnectionError("down")
        return [self.result for _ in calls]


def _provider(tmp_path, fake, clock):
    return AstralInsiderProvider(["aapl", "TSLA", "NVDA"], fake, tmp_path, batch=2, clock=lambda: clock[0])


def test_provider_batches_caches_and_refreshes(tmp_path):
    clock = [NOW]
    fake = FakeCalls({"rows": FIX["insiders"]["rows"][1:]})  # the AAPL sale + tax row
    p = _provider(tmp_path, fake, clock)
    evs = p.fetch({EventKind.INSIDER_TRADE}, date(2026, 10, 1))
    assert [(e.symbol, e.side) for e in evs] == [("AAPL", Side.SELL)]
    batches = [args["symbols"] for _, args in fake.calls[0]]
    assert batches == [["AAPL", "NVDA"], ["TSLA"]]
    assert fake.calls[0][0][1]["event_types"] == ["open_market_purchase", "open_market_sale"]
    assert fake.calls[0][0][1]["available_from"] == "2026-08-28T00:00:00Z"
    p.fetch({EventKind.INSIDER_TRADE}, date(2026, 10, 1))
    assert len(fake.calls) == 1  # inside refresh_hours: served from cache
    # a restart reads the file without a call
    assert AstralInsiderProvider(["AAPL"], FakeCalls(fail=True), tmp_path, clock=lambda: NOW).fetch(
        {EventKind.INSIDER_TRADE}, date(2026, 10, 1))[0].actor == "Newstead Jennifer"
    clock[0] = NOW + timedelta(hours=7)
    p.fetch({EventKind.INSIDER_TRADE}, date(2026, 10, 1))
    assert len(fake.calls) == 2
    assert p.fetch({EventKind.CONGRESS_TRADE}, date(2026, 10, 1)) == []


def test_provider_outage_serves_cache_and_backs_off(tmp_path):
    clock = [NOW]
    _provider(tmp_path, FakeCalls({"rows": FIX["insiders"]["rows"]}), clock).refresh()
    down = FakeCalls(fail=True)
    p = _provider(tmp_path, down, clock)
    assert len(p.fetch({EventKind.INSIDER_TRADE}, date(2025, 1, 1))) == 1  # the cached AAPL sale
    p.fetch({EventKind.INSIDER_TRADE}, date(2025, 1, 1))
    assert len(down.calls) == 1  # waits retry_minutes before trying again
    clock[0] = NOW + timedelta(minutes=31)
    p.fetch({EventKind.INSIDER_TRADE}, date(2025, 1, 1))
    assert len(down.calls) == 2


def test_old_events_are_pruned(tmp_path):
    clock = [NOW]
    p = _provider(tmp_path, FakeCalls({"rows": FIX["insiders"]["rows"]}), clock)
    assert {e.symbol for e in p.fetch({EventKind.INSIDER_TRADE}, date(2020, 1, 1))} == {"AAPL"}  # TSLA 2025 is past lookback


def test_registry_needs_the_url(caplog):
    assert build_providers(["astral"], {}) == []
    assert "ASTRAL_MCP_URL" in caplog.text
    (p,) = build_providers(["astral"], {"ASTRAL_MCP_URL": "https://example.invalid/mcp"})
    assert p.name == "astral" and len(p.symbols) > 10


def test_news_summary_is_labels_only():
    out = summarize(FIX["news"]["rows"])
    assert out == {"source": "astral", "articles": 3, "by_type": {"partnership": 2, "product_launch": 1},
                   "max_importance": 3.0, "latest": "2026-10-26"}
    assert "NVIDIA" not in json.dumps(out) and "must not" not in json.dumps(out)
    assert summarize([])["articles"] == 0


def test_news_caches_and_cools_down():
    t = [0.0]
    ok = FakeCalls({"rows": FIX["news"]["rows"]})
    news = AstralNews(ok, clock=lambda: NOW, mono=lambda: t[0])
    assert news.for_symbol("nvda")["articles"] == 3
    news.for_symbol("NVDA")
    assert len(ok.calls) == 1 and ok.calls[0][0][1]["symbols"] == ["NVDA"]
    down = FakeCalls(fail=True)
    news = AstralNews(down, clock=lambda: NOW, mono=lambda: t[0])
    assert news.for_symbol("AMD") is None and news.for_symbol("MSFT") is None
    assert len(down.calls) == 1  # one failure pauses Astral for every symbol


def test_backtest_verdicts():
    exp = {"avg_ret": 0.002, "source": "x"}
    assert backtest_check.verdict([0.01], None)["status"] == "none"
    assert backtest_check.verdict([0.01] * 3, exp)["status"] == "too_early"
    assert backtest_check.verdict([-0.01, -0.012] * 6, exp)["status"] == "below"
    assert backtest_check.verdict([0.03, 0.035] * 6, exp)["status"] == "above"
    assert backtest_check.verdict([0.02, -0.016] * 6, exp)["status"] == "in_line"


def test_by_model_uses_closed_round_trips():
    fills = []
    for i in range(10):
        day = f"2026-10-{i + 1:02d}"
        fills += [{"ts": f"{day}T15:30:00", "symbol": "SPY", "side": "buy", "quantity": 1, "price": 100, "tag": "intraday_momentum"},
                  {"ts": f"{day}T15:55:00", "symbol": "SPY", "side": "sell", "quantity": 1, "price": 99 + (i % 2) * 0.5}]
    out = backtest_check.by_model(fills, {"intraday_momentum": {"avg_ret": 0.001}, "rsi2_etf": {"avg_ret": 0.0017}})
    assert out["intraday_momentum"]["status"] == "below" and out["intraday_momentum"]["n"] == 10
    assert out["rsi2_etf"]["status"] == "too_early" and out["rsi2_etf"]["n"] == 0


def test_shipped_backtests_file_matches_strategies():
    root = Path(__file__).resolve().parents[1] / "config"
    exp = backtest_check.load(root / "backtests.toml")
    strategies = tomllib.loads((root / "strategies.toml").read_text())
    assert exp and set(exp) <= set(strategies)
    assert all(-0.05 < e["avg_ret"] < 0.05 and e.get("source") for e in exp.values())
    assert backtest_check.load(root / "missing.toml") == {}
