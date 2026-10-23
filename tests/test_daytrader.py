"""The day-trader skill pack inside the bot: setups, SEC 8-Ks and numbers, the time machine,
House PTRs, the trader-exit/-budget/-risk checks, close-confirmed ORB entries and price alerts."""

import gzip
import json
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx

from ctbot import thoughts, trade_log
from ctbot.analysis import setups
from ctbot.config import RiskLimits, Settings
from ctbot.market_data import sec_edgar
from ctbot.market_hours import ET
from ctbot.models import Bar, Candidate, Decision, Direction, EventKind, Position, Quote, Side, Signal
from ctbot.net import HttpSource
from ctbot.providers import house_clerk
from ctbot.research import timemachine
from ctbot.risk.manager import AccountState, RiskManager
from ctbot.strategies.base import StrategyContext

FIX = Path(__file__).parent / "fixtures" / "daytrader"
NOW = datetime(2026, 10, 19, 14, 15, tzinfo=UTC)  # Mon 10:15 ET
QUOTE = Quote("TSLA", 364.10, 364.20, 364.18, NOW)


# ---- trader-scanner ----------------------------------------------------------------------

def test_setups_tag_a_breakout_and_an_oversold_bounce():
    flat = [100.0 + (i % 3) * 0.2 for i in range(40)]
    assert setups.find(flat[:-1] + [103.0])["tags"] == ["breakout_20d"]
    falling = [100.0 * 0.98 ** i for i in range(40)]
    found = setups.find(falling + [falling[-1] * 1.01])
    assert "oversold_bounce" in found["tags"] and found["rsi14"] < 40
    flag = [50.0] * 15 + [50 + i * 0.8 for i in range(1, 11)] + [58.0, 58.3, 57.9, 58.2, 58.1]
    assert "bull_flag" in setups.find(flag)["tags"]
    assert setups.find([1.0] * 10) is None  # too short to say anything


# ---- trader-news / trader-analyst: SEC EDGAR ---------------------------------------------

SUBMISSIONS = {"filings": {"recent": {
    "form": ["8-K", "10-Q", "8-K", "8-K"],
    "filingDate": ["2026-10-16", "2026-10-15", "2026-10-01", "2026-08-01"],
    "items": ["2.02,9.01", "", "3.01,7.01", "5.02"],
}}}


def test_8k_items_and_red_flags():
    today = date(2026, 10, 19)
    s = sec_edgar.summarize_8k(sec_edgar.recent_8k(SUBMISSIONS, today, 30), today)
    assert s["filings"] == 2 and s["latest"] == "2026-10-16"
    assert s["important"] == ["2.02 earnings results", "3.01 delisting notice"]
    assert s["red_flags"] == []  # the delisting notice is 18 days old: outside RED_FLAG_DAYS
    s = sec_edgar.summarize_8k(sec_edgar.recent_8k(SUBMISSIONS, date(2026, 10, 10), 30), date(2026, 10, 10))
    assert s["red_flags"] == ["3.01 delisting notice"]


def _fact(start, end, val, form="10-Q", filed="2026-07-25"):
    return {"start": start, "end": end, "val": val, "form": form, "filed": filed, "accn": filed}


def test_latest_quarter_against_a_year_ago_with_a_derived_q4():
    rev = [_fact("2025-01-01", "2025-03-31", 80), _fact("2025-04-01", "2025-06-30", 90),
           _fact("2025-07-01", "2025-09-30", 95), _fact("2025-01-01", "2025-12-31", 400, "10-K", "2026-02-01"),
           _fact("2026-01-01", "2026-03-31", 100), _fact("2026-04-01", "2026-06-30", 108)]
    ni = [_fact(f["start"], f["end"], f["val"] / 10, f["form"], f["filed"]) for f in rev]
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": rev}},
                                   "NetIncomeLoss": {"units": {"USD": ni}}}}}
    q = sec_edgar.latest_quarter(facts)
    assert q["quarter_end"] == "2026-06-30" and q["revenue_yoy"] == 0.2 and q["net_margin"] == 0.1
    assert sec_edgar._quarters(rev)["2025-12-31"] == {
        "value": 135, "start": "2025-10-01", "derived": True, "filed": "2026-02-01"}


def test_sec_client_needs_a_real_user_agent_and_caches():
    assert not sec_edgar.valid_user_agent("") and not sec_edgar.valid_user_agent("Me you@example.com")
    assert sec_edgar.valid_user_agent("Harold h@example.org")
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if req.url.path.endswith("company_tickers.json"):
            return httpx.Response(200, json={"0": {"ticker": "XYZ", "cik_str": 42}})
        return httpx.Response(200, json=SUBMISSIONS)

    src = HttpSource("sec", client=httpx.Client(transport=httpx.MockTransport(handler)))
    sec = sec_edgar.SecEdgar("Harold h@example.org", source=src, clock=lambda: datetime(2026, 10, 10, tzinfo=UTC))
    assert sec.red_flags("XYZ") == ["3.01 delisting notice"]
    assert sec.filings("XYZ")["important"]
    assert sec.red_flags("SPY") == []  # no CIK: no data, never an error
    assert sum("CIK0000000042" in u for u in calls) == 1  # one submissions download, then the cache


def test_http_source_returns_raw_bytes():
    src = HttpSource("x", client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"PK\x03\x04zip"))))
    assert src.get_bytes("https://example.test/a.zip") == b"PK\x03\x04zip"


# ---- trader-timemachine ------------------------------------------------------------------

def _tsla_bars():
    doc = json.load(gzip.open(FIX / "TSLA-60d5m.json.gz"))
    r = doc["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    return [Bar("TSLA", datetime.fromtimestamp(t, UTC), o, h, lo, c, v or 0)
            for t, o, h, lo, c, v in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"])
            if c is not None]


def test_time_machine_reproduces_the_reel_odds():
    day = date(2026, 10, 19)
    out = timemachine.replay(_tsla_bars(), day, datetime.combine(day, time(10, 15), tzinfo=ET), None,
                             364.18, 358.32, 370.04)
    # the pack's own run on the same bars: target 29.4%, stop 25.5%, neither 45.1% (another RNG order)
    assert out["bars"] == 69 and out["seed"] == 19 and out["history"]["sessions"] == 59
    assert abs(out["target_first"] - 0.294) < 0.01 and abs(out["stop_first"] - 0.255) < 0.015
    assert out["verdict"].startswith("Close to a coin flip")
    again = timemachine.replay(_tsla_bars(), day, datetime.combine(day, time(10, 15), tzinfo=ET), None,
                               364.18, 358.32, 370.04)
    assert again == out  # same plan, same bars, same seed: the same numbers


def test_time_machine_refuses_what_it_cannot_replay():
    day = date(2026, 10, 19)
    at = datetime.combine(day, time(10, 15), tzinfo=ET)
    assert timemachine.replay(_tsla_bars(), day, at, None, 364.18, 365.0, 370.04) is None  # stop above entry
    assert timemachine.replay(_tsla_bars()[:500], day, at, None, 364.18, 358.32, 370.04) is None  # too few days
    assert timemachine.verdict(0.241, 0.505, 2.0).startswith("Close to break-even")  # 32% vs 33% needed


def test_replay_odds_only_for_same_day_plans():
    bars = _tsla_bars()

    class MD:
        def intraday_bars(self, symbols, start, end):
            return {"TSLA": [b for b in bars if start <= b.ts < end]}

    odds = timemachine.ReplayOdds(MD(), replays=500)
    plan = {"ref_price": 364.18, "stop": 358.32, "target": 370.04, "exit_at": "2026-10-19T15:50:00-04:00"}
    out = odds.for_plan("TSLA", plan, NOW)
    assert out["replays"] == 500 and out["until_et"] == "15:50"
    assert odds.for_plan("TSLA", {**plan, "exit_at": "2026-10-20T09:35:00-04:00"}, NOW) is None
    assert odds.for_plan("TSLA", {"ref_price": 1.0}, NOW) is None


# ---- trader-pelosi: House PTRs -----------------------------------------------------------

def test_ptr_rows_become_share_trade_events_only():
    rows = house_clerk.parse_ptr_text((FIX / "ptr-20035143.txt").read_text())
    assert [(r["ticker"], r["asset_type"], r["option"]) for r in rows][:2] == [("BE", "ST", False), ("BE", "OP", True)]
    rep = {"doc_id": "20035143", "name": "Nancy Pelosi", "filed": "2026-08-21", "rows": rows}
    events = house_clerk.to_events(rep)
    assert [e.symbol for e in events] == ["BE", "BE", "INTC"]  # options and the no-ticker LLC are skipped
    e = events[0]
    assert e.kind is EventKind.CONGRESS_TRADE and e.side is Side.BUY and e.size_usd == 1_000_001
    assert e.disclosure_lag_days == 28
    exercised = house_clerk.parse_ptr_text((FIX / "ptr-20033725-excerpt.txt").read_text())
    rep = {"doc_id": "20033725", "name": "Nancy Pelosi", "filed": "2026-01-23", "rows": exercised}
    assert [(e.symbol, e.side) for e in house_clerk.to_events(rep)] == [("PYPL", Side.SELL)]


def test_house_provider_reads_each_new_ptr_once():
    zipped = (FIX / "2026FD.zip").read_bytes()
    text = (FIX / "ptr-20035143.txt").read_text()
    pdfs = []

    def handler(req):
        if req.url.path.endswith("FD.zip"):
            return httpx.Response(200, content=zipped)
        pdfs.append(req.url.path)
        return httpx.Response(200, content=b"%PDF")

    src = HttpSource("house", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with tempfile.TemporaryDirectory() as d:
        clock = [datetime(2026, 10, 13, 15, tzinfo=UTC)]
        p = house_clerk.HouseClerkProvider(Path(d), src, to_text=lambda pdf: text, clock=lambda: clock[0],
                                           sleep=lambda s: None)
        events = p.fetch({EventKind.CONGRESS_TRADE}, date(2026, 9, 1))
        n = len(pdfs)
        assert n > 0 and events and all(e.source == "house_clerk" for e in events)
        clock[0] += timedelta(hours=7)
        p2 = house_clerk.HouseClerkProvider(Path(d), src, to_text=lambda pdf: text, clock=lambda: clock[0],
                                            sleep=lambda s: None)
        assert len(p2.fetch({EventKind.CONGRESS_TRADE}, date(2026, 9, 1))) == len(events)
        assert len(pdfs) == n  # parsed reports were cached on disk: nothing downloaded twice
        assert p2.fetch({EventKind.OPTIONS_FLOW}, date(2026, 9, 1)) == []


def test_house_provider_without_pdftotext_reads_nothing_and_says_so(caplog):
    src = HttpSource("house", client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=(FIX / "2026FD.zip").read_bytes()
                                   if req.url.path.endswith(".zip") else b"%PDF"))))
    with tempfile.TemporaryDirectory() as d:
        p = house_clerk.HouseClerkProvider(Path(d), src, to_text=lambda pdf: None,
                                           clock=lambda: datetime(2026, 10, 13, tzinfo=UTC), sleep=lambda s: None)
        assert p.fetch({EventKind.CONGRESS_TRADE}, date(2026, 9, 1)) == []
    assert "pdftotext not found" in caplog.text


# ---- trader-exit / -budget / -risk -------------------------------------------------------

def _state(**kw):
    base = dict(equity=10_000, cash=10_000, peak_equity=10_000, day_start_equity=10_000, open_positions=0)
    return AccountState(**{**base, **kw})


def _plan_cand(stop, target=None, ref=364.18):
    sig = Signal("orb", "TSLA", Direction.LONG, 0.8, 0.25, {
        "ref_price": ref, "stop": stop, "target": target, "exit_at": "2026-10-19T15:50:00-04:00"})
    return Candidate("TSLA", Direction.LONG, 0.8, (sig,))


def _reviewed_cand():
    return Candidate("TSLA", Direction.LONG, 0.8, (Signal("momentum", "TSLA", Direction.LONG, 0.8, 20),))


def _ok(stop_pct=None):
    return Decision(approve=True, confidence=0.9, rationale="", suggested_notional=1e9, stop_loss_pct=stop_pct)


def _check(limits, cand, state=None, decision=None, **kw):
    rm = RiskManager(RiskLimits(max_trading_capital=10_000, max_position_pct=0.5, max_sector_pct=0, **limits))
    return rm.check_entry(cand, decision or _ok(), state or _state(), QUOTE, 1e10, now=NOW, **kw)


def test_a_stop_too_tight_or_a_thin_reward_is_refused():
    assert "stop_too_tight" in _check({}, _plan_cand(364.10)).reasons  # 0.02% away
    assert _check({}, _plan_cand(358.32, 370.04)).allowed
    v = _check({"min_reward_risk": 1.5}, _plan_cand(358.32, 370.04))
    assert "reward_risk_1.00_below_1.5" in v.reasons


def test_the_stop_decides_the_size():
    # 1% of $10,000 at risk with a 5% stop: $2,000 of stock, not the $5,000 cap
    v = _check({"max_loss_per_trade_pct": 0.01}, _reviewed_cand(), decision=_ok(0.05))
    assert v.allowed and v.orders[0].notional == 2000.0
    assert _check({}, _reviewed_cand(), decision=_ok(0.05)).orders[0].notional == 5000.0


def test_open_risk_budget_entries_per_day_and_the_entry_window():
    v = _check({"max_open_risk_pct": 0.03}, _reviewed_cand(), _state(open_risk=250.0), _ok(0.05))
    assert v.orders[0].notional == 1000.0  # $50 of room left at a 5% stop
    v = _check({"max_open_risk_pct": 0.03}, _reviewed_cand(), _state(open_risk=300.0), _ok(0.05))
    assert "open_risk_budget_used" in v.reasons and "notional_too_small" not in v.reasons
    today = date(2026, 10, 19)
    v = _check({"max_entries_per_day": 2}, _reviewed_cand(), _state(entry_dates=[today, today]))
    assert "max_entries_per_day" in v.reasons
    assert "before_entry_window" in _check({"no_entries_before": "10:30"}, _reviewed_cand()).reasons
    assert _check({"no_entries_before": "09:45"}, _reviewed_cand()).allowed


def test_red_flag_8k_blocks_an_entry():
    v = _check({}, _reviewed_cand(), red_flags=["1.03 bankruptcy"])
    assert "sec_8k_red_flag" in v.reasons


def test_settings_read_the_new_limits():
    s = Settings.from_env({"MAX_LOSS_PER_TRADE_PCT": "0.01", "NO_ENTRIES_BEFORE": "09:45",
                           "REVIEW_REPLAY_ODDS": "false"})
    assert s.risk.max_loss_per_trade_pct == 0.01 and s.risk.no_entries_before == "09:45"
    assert s.risk.min_stop_distance_pct == 0.0005 and not s.review_replay_odds


def test_trade_log_reports_the_result_in_r():
    rec = trade_log.close_record("TSLA", {"stop": 358.32, "ctx": {"r0": 5.86}}, 364.18, 8, 370.04,
                                 NOW, NOW + timedelta(hours=1), "take_profit")
    assert rec["r_multiple"] == 1.0
    assert trade_log.close_record("TSLA", {"stop": 358.32}, 364.18, 8, 370.04, NOW, NOW,
                                  "take_profit")["r_multiple"] is None


# ---- trader-entry: close-confirmed opening-range breakout --------------------------------

def test_orb_sip_can_wait_for_a_close_above_the_range():
    from ctbot.strategies.orb_sip import OrbStocksInPlay

    class Scan:
        def today(self):
            return [{"symbol": "XYZ", "rvol_ratio": 0.2, "atr14": 2.0, "last": 50}]

    t0 = datetime(2026, 10, 14, 13, 30, tzinfo=UTC)
    first = Bar("XYZ", t0, 50.0, 51.0, 49.8, 50.8, 1e5)
    wick = Bar("XYZ", t0 + timedelta(minutes=5), 50.8, 51.2, 50.7, 50.9, 2e5)  # pokes above, closes under
    bars = {"XYZ": [first, wick]}
    md = SimpleNamespace(today_bars=lambda syms: bars)
    now = t0 + timedelta(minutes=11)
    ctx = StrategyContext(md, ("SPY",), (), now.date(), frozenset(), now)
    assert len(OrbStocksInPlay(scanner=Scan(), confirm="touch").generate(ctx)) == 1  # the paper's rule
    assert OrbStocksInPlay(scanner=Scan()).generate(ctx) == []  # the default waits for a close
    bars["XYZ"] = [first, Bar("XYZ", t0 + timedelta(minutes=5), 50.8, 51.3, 50.7, 51.2, 5e4)]
    assert len(OrbStocksInPlay(scanner=Scan(), confirm="close").generate(ctx)) == 1
    assert OrbStocksInPlay(scanner=Scan(), confirm="close", min_volume_x=1.0).generate(ctx) == []  # thin bar


# ---- trader-alerts: Bubs Thoughts on held positions --------------------------------------

def test_price_alerts_near_the_stop_and_at_1r_ring_once():
    with tempfile.TemporaryDirectory() as d:
        bid = {"TSLA": 359.5, "KO": 66.5}
        c = SimpleNamespace(
            settings=SimpleNamespace(thoughts_path=Path(d) / "t.jsonl"),
            executor=SimpleNamespace(positions=lambda: [Position("TSLA", 8, 364.18), Position("KO", 1, 60.0)]),
            market_data=SimpleNamespace(quotes=lambda syms: {s: Quote(s, bid[s], bid[s], bid[s]) for s in syms}))
        state = _state(brackets={"TSLA": {"stop": 358.32, "ctx": {"r0": 5.86}}, "KO": {"stop": 57.0}})
        thoughts._price_alerts(c, state, NOW)
        thoughts._price_alerts(c, state, NOW + timedelta(minutes=5))
        said = {t["key"]: t["text"] for t in thoughts.read(c.settings.thoughts_path)}
    assert set(said) == {"near_stop_TSLA", "up_1r_KO"}
    assert "$1.18 above its stop" in said["near_stop_TSLA"] and "up 2.2x what it risked" in said["up_1r_KO"]


# ---- wiring: the red-flag gate, the review's replay odds, R at the fill ------------------

def test_pipeline_skips_red_flag_names_and_gives_reviews_the_odds():
    from ctbot.paper.simulator import PaperBroker
    from ctbot.pipeline import run_cycle
    from ctbot.strategies.registry import WeightedStrategy
    from tests.test_pipeline import _MD, _components, _Engine, _state, _Strategy

    now = datetime(2026, 10, 13, 15, tzinfo=UTC)
    seen = []

    class Ctx:
        flags: list = []

        def next_earnings(self, symbol):
            return None

        def for_symbol(self, symbol):
            return {}

        def red_flags(self, symbol):
            return self.flags

        def plan_odds(self, symbol, plan):
            return {"target_first": 0.3, "plan_stop": plan["stop"]} if plan else None

    class Engine(_Engine):
        def evaluate(self, candidate, account):
            seen.append(account)
            return super().evaluate(candidate, account)

    sig = Signal("orb", "KO", Direction.LONG, 1.0, 0.25, {
        "ref_price": 50.0, "stop": 49.0, "target": 52.0, "exit_at": "2026-10-13T15:50:00-04:00"})
    strat = WeightedStrategy(_Strategy("orb", [sig]), 1.0, standalone=True)
    for flags in (["1.03 bankruptcy"], []):
        with tempfile.TemporaryDirectory() as d:
            broker = PaperBroker(_MD(), 250.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: now)
            c = _components(broker, [strat])
            c.engine, c.context = Engine(), Ctx()
            Ctx.flags = flags
            state = _state(broker)
            run_cycle(c, state, now)
            held = [p.symbol for p in broker.positions()]
        if flags:
            assert held == [] and seen == []  # refused before the paid review
        else:
            assert held == ["KO"] and seen[0]["replay_odds"] == {"target_first": 0.3, "plan_stop": 49.0}
            assert state.brackets["KO"]["ctx"]["r0"] == 1.0 and state.entry_dates == [date(2026, 10, 13)]
            assert abs(state.open_risk - broker.positions()[0].quantity * 1.0) < 1e-9  # $1 a share at the stop
