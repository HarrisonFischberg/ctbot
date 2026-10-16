"""TradingFlow: allowlisted client, daily-bar failover, trade-level options flow. No network."""

import json
import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from ctbot.market_data.failover import DailyBarFailover, TradingFlowDailyBars, parse_daily_candles
from ctbot.market_data.robinhood_mcp_client import ToolNotAllowed
from ctbot.market_data.robinhood_readonly import RobinhoodReadOnlyMarketData
from ctbot.market_data.tradingflow_client import READ_ONLY_TOOLS, TradingFlowClient
from ctbot.models import Bar, EventKind, Side
from ctbot.net import SourceUnavailable
from ctbot.providers.registry import build_providers
from ctbot.providers.tradingflow_options import TradingFlowOptionsFlowProvider, parse_rank

NOW = datetime(2026, 10, 13, 19, 0, tzinfo=timezone.utc)  # Tue 15:00 ET, in session
FAKE_KEY = "tf_test_key_not_real_0123456789"


# ---- client --------------------------------------------------------------------

def test_allowlist_is_read_only_and_refused_tools_never_reach_the_server(monkeypatch):
    assert not any(t.startswith(("place_", "cancel_", "review_", "exercise")) for t in READ_ONLY_TOOLS)
    assert "execute_tf_query" not in READ_ONLY_TOOLS  # no raw SQL
    client = TradingFlowClient(FAKE_KEY)

    async def boom(*a):
        raise AssertionError("network used")

    monkeypatch.setattr(client, "_call_many", boom)
    with pytest.raises(ToolNotAllowed):
        client.call("execute_tf_query", {"sql": "select 1"})


def test_no_key_means_unavailable_without_network(monkeypatch):
    monkeypatch.delenv("TRADINGFLOW_API_KEY", raising=False)
    client = TradingFlowClient()
    assert not client.configured
    with pytest.raises(SourceUnavailable, match="not set"):
        client.call("symbol_daily_candles", {"symbol": "SPY"})
    monkeypatch.setenv("TRADINGFLOW_API_KEY", FAKE_KEY)  # read at call time: no restart needed
    assert client.configured


def test_breaker_pauses_after_failures_and_never_leaks_the_key(caplog):
    t = [0.0]
    client = TradingFlowClient(FAKE_KEY, failure_threshold=2, cooldown_s=60, clock=lambda: t[0])
    calls = []

    async def failing(batch, key):
        calls.append(batch)
        raise ExceptionGroup("tg", [RuntimeError(f"auth rejected for {key}")])

    client._call_many = failing
    with caplog.at_level(logging.DEBUG):
        for _ in range(2):
            with pytest.raises(SourceUnavailable) as e:
                client.call("symbol_daily_candles", {"symbol": "SPY"})
            assert FAKE_KEY not in str(e.value) and "REDACTED" in str(e.value)
        with pytest.raises(SourceUnavailable, match="cooling down"):
            client.call("symbol_daily_candles", {"symbol": "SPY"})
    assert len(calls) == 2
    assert FAKE_KEY not in caplog.text
    t[0] = 61.0
    with pytest.raises(SourceUnavailable):
        client.call("symbol_daily_candles", {"symbol": "SPY"})
    assert len(calls) == 3


# ---- daily bars + failover -----------------------------------------------------

def _candles(rows):
    return {"symbol": "X", "bars": [
        {"date": d, "open": c, "high": c, "low": c, "close": c, "preClose": p, "volume": 100}
        for d, c, p in rows]}


def test_daily_candles_cut_at_an_unexplained_jump():
    bars = parse_daily_candles("x", _candles([
        ("2026-10-06", 400.0, 398.0),
        ("2026-10-07", 402.0, 400.0),
        ("2026-10-08", 205.0, 201.0),   # 2:1 split, unadjusted: preClose 201 vs prior bar 402
        ("2026-10-09", 210.0, 200.0),   # 2.4% mismatch: vendor noise, kept
        ("2026-10-12", 0.0, 210.0),     # bad row
    ]))
    assert [b.ts.date().isoformat() for b in bars] == ["2026-10-08", "2026-10-09"]
    assert bars[0].symbol == "X" and bars[0].ts.tzinfo is timezone.utc


def test_tradingflow_bars_batch_the_universe_and_exclude_today():
    batches = []

    def call_many(calls):
        batches.append(calls)
        return [_candles([("2026-10-09", 10.0, 9.9), ("2026-10-12", 10.1, 10.0),
                          ("2026-10-13", 10.2, 10.1)]) for _ in calls]

    tf = TradingFlowDailyBars(call_many, clock=lambda: NOW)
    tf.warm(["spy", "QQQ"])
    assert [b.ts.date().isoformat() for b in tf.daily_bars("SPY", 5)] == ["2026-10-09", "2026-10-12"]
    assert tf.daily_bars("QQQ", 1)[0].close == 10.1
    assert len(batches) == 1 and sorted(a["symbol"] for _, a in batches[0]) == ["QQQ", "SPY"]


class _Primary:
    def __init__(self, fail=False, bars=None):
        self.fail, self.bars, self.calls, self.warmed = fail, bars or [], 0, set()

    def daily_bars(self, symbol, lookback_days):
        self.calls += 1
        if self.fail:
            raise RuntimeError("robinhood down")
        return self.bars

    def warm(self, symbols):
        self.warmed |= set(symbols)

    def quotes(self, symbols):
        return {"SPY": "live quote"}


class _Backup:
    def __init__(self, fail=False):
        self.fail, self.warmed = fail, set()

    def daily_bars(self, symbol, lookback_days):
        if self.fail:
            raise SourceUnavailable("tradingflow: down")
        return [Bar(symbol, NOW, 1, 1, 1, 1, 1)]

    def warm(self, symbols):
        self.warmed |= set(symbols)


def test_failover_uses_backup_and_pauses_primary_then_retries():
    t = [0.0]
    primary = _Primary(fail=True)
    md = DailyBarFailover(primary, _Backup(), retry_primary_s=300, clock=lambda: t[0])
    md.warm(["SPY"])
    assert primary.warmed == md._backup.warmed == {"SPY"}
    assert md.daily_bars("SPY", 5)[0].symbol == "SPY"
    assert md.daily_bars("QQQ", 5)[0].symbol == "QQQ"
    assert primary.calls == 1  # paused after one failure, not one per symbol
    t[0] = 301.0
    md.daily_bars("SPY", 5)
    assert primary.calls == 2
    assert md.quotes(["SPY"]) == {"SPY": "live quote"}  # quotes never fail over


def test_failover_without_a_working_backup_behaves_like_no_backup():
    md = DailyBarFailover(_Primary(fail=True), _Backup(fail=True))
    with pytest.raises(RuntimeError, match="robinhood down"):
        md.daily_bars("SPY", 5)
    healthy = [Bar("SPY", NOW, 2, 2, 2, 2, 2)]
    assert DailyBarFailover(_Primary(bars=healthy), _Backup()).daily_bars("SPY", 5) == healthy
    assert DailyBarFailover(_Primary(bars=[]), _Backup(fail=True)).daily_bars("SPY", 5) == []
    assert DailyBarFailover(_Primary(bars=[]), _Backup()).daily_bars("SPY", 5)[0].close == 1


def test_today_bars_skip_the_first_five_minutes_robinhood_rejects():
    def never(calls):
        raise AssertionError("Robinhood called with an empty window")

    open_plus_1 = datetime(2026, 10, 13, 13, 31, tzinfo=timezone.utc)  # 09:31 ET
    assert RobinhoodReadOnlyMarketData(never, clock=lambda: open_plus_1).today_bars(["SPY"]) == {}


# ---- options flow ----------------------------------------------------------------

def _row(sym="NVDA", pc="CALL", expiry="2026-10-23", vol=5000, oi=1000, bull=900_000, bear=100_000):
    return {"optionSymbol": f"{sym}261023C00230000", "symbol": sym, "putCall": pc, "strike": 230,
            "expirationDate": expiry, "dailyVolume": vol, "oi": oi, "bullishPremium": bull,
            "bearishPremium": bear, "neutralPremium": 50_000, "oiChange": 800, "iv": 0.5,
            "delta": 0.4}


def test_parse_rank_takes_direction_from_trade_side():
    day = date(2026, 10, 13)
    events = parse_rank({"rows": [
        _row(),                                               # calls bought: bullish
        _row(sym="AMD", pc="PUT", bull=50_000, bear=700_000),  # puts bought: bearish
        _row(expiry="2026-10-13"),                            # 0DTE: dropped
        _row(vol=500, oi=4000),                               # not unusual: dropped
        _row(bull=300_000, bear=300_000),                     # no net direction: dropped
        {"symbol": "BAD"},                                    # malformed: dropped
    ]}, day)
    assert [(e.symbol, e.side, e.size_usd) for e in events] == [
        ("NVDA", Side.BUY, 800_000.0), ("AMD", Side.SELL, 650_000.0)]
    assert events[0].kind is EventKind.OPTIONS_FLOW and events[0].raw["expiry"] == "2026-10-23"


def _flow_server(holidays=()):
    log = []

    def call_many(calls):
        log.append(calls)
        out = []
        for tool, args in calls:
            assert tool == "contract_rank_snapshot" and "SPY" not in args["symbols"]
            d = args["tradingDate"]
            out.append({"effectiveDate": "2026-10-09" if d in holidays else d,
                        "rows": [_row(pc=args["putCall"])]})
        return out

    return call_many, log


def test_flow_provider_backfills_refreshes_today_and_settles_days(tmp_path):
    t = [NOW]
    call_many, log = _flow_server(holidays={"2026-10-12"})
    p = TradingFlowOptionsFlowProvider(["NVDA", "SPY"], call_many, tmp_path, backfill_days=3,
                                       refresh_s=1800, clock=lambda: t[0])
    events = p.fetch({EventKind.OPTIONS_FLOW}, date(2026, 10, 1))
    assert sorted({c[1]["tradingDate"] for c in log[0]}) == ["2026-10-09", "2026-10-12", "2026-10-13"]
    assert len(log[0]) == 6 and {e.event_date.isoformat() for e in events} == {"2026-10-09", "2026-10-13"}
    assert json.loads((tmp_path / "2026-10-12.json").read_text()) == {"final": True, "events": []}
    assert not json.loads((tmp_path / "2026-10-13.json").read_text())["final"]

    p.fetch({EventKind.OPTIONS_FLOW}, date(2026, 10, 1))
    assert len(log) == 1  # past days settled, today read recently
    t[0] = NOW + timedelta(minutes=31)
    p.fetch({EventKind.OPTIONS_FLOW}, date(2026, 10, 1))
    assert [c[1]["tradingDate"] for c in log[1]] == ["2026-10-13", "2026-10-13"]
    t[0] = NOW + timedelta(hours=1, minutes=25)  # 16:25 ET: after the close
    p.fetch({EventKind.OPTIONS_FLOW}, date(2026, 10, 1))
    assert json.loads((tmp_path / "2026-10-13.json").read_text())["final"]
    t[0] += timedelta(hours=3)
    p.fetch({EventKind.OPTIONS_FLOW}, date(2026, 10, 1))
    assert len(log) == 3
    assert {e.event_date for e in p.fetch({EventKind.OPTIONS_FLOW}, date(2026, 10, 13))} == {date(2026, 10, 13)}


def test_flow_provider_keeps_cached_days_when_tradingflow_is_down(tmp_path):
    call_many, _ = _flow_server()
    TradingFlowOptionsFlowProvider(["NVDA"], call_many, tmp_path, backfill_days=1,
                                   clock=lambda: NOW).fetch({EventKind.OPTIONS_FLOW}, date(2026, 10, 1))

    def down(calls):
        raise SourceUnavailable("tradingflow: timeout")

    later = NOW + timedelta(hours=1)
    events = TradingFlowOptionsFlowProvider(["NVDA"], down, tmp_path, backfill_days=1,
                                            clock=lambda: later).fetch({EventKind.OPTIONS_FLOW}, date(2026, 10, 1))
    assert len(events) == 2


def test_registry_needs_the_key_and_warns_about_double_counting(tmp_path, caplog):
    universe = tmp_path / "universe.txt"
    universe.write_text("NVDA\nSPY\n")
    assert build_providers(["tradingflow_options"], {"UNIVERSE_FILE": str(universe)}) == []
    env = {"UNIVERSE_FILE": str(universe), "TRADINGFLOW_API_KEY": FAKE_KEY}
    with caplog.at_level(logging.WARNING):
        providers = build_providers(["cboe_options", "tradingflow_options"], env)
    assert [p.name for p in providers] == ["cboe_options", "tradingflow_options"]
    assert providers[1].symbols == ["NVDA"]
    assert "counted twice" in caplog.text and FAKE_KEY not in caplog.text
