"""Paper launcher: run one strategy with its own capital; stop sells what it opened."""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ctbot import allocations
from ctbot.config import Settings
from ctbot.models import Bar, Quote
from ctbot.paper.simulator import PaperBroker
from ctbot.pipeline import Components, run_cycle
from ctbot.risk.manager import AccountState, RiskManager
from ctbot.strategies.registry import WeightedStrategy
from ctbot.strategies.swing import TrendCore

NOW = datetime(2026, 10, 14, 19, 35, tzinfo=timezone.utc)  # Wed 15:35 ET


def test_allocations_cap_total_and_stop_flow(tmp_path):
    f = tmp_path / "a.json"
    allocations.start(f, "trend_core", 3000, 5000)
    with pytest.raises(ValueError):
        allocations.start(f, "orb_sip", 2500, 5000)  # 3000 + 2500 > 5000
    allocations.start(f, "orb_sip", 2000, 5000)
    allocations.start(f, "trend_core", 1000, 5000)  # resize
    assert allocations.running(f) == {"trend_core": 1000.0, "orb_sip": 2000.0}
    allocations.stop(f, "orb_sip")
    assert allocations.running(f) == {"trend_core": 1000.0} and allocations.stopping(f) == {"orb_sip"}
    allocations.mark_stopped(f, "orb_sip")
    assert allocations.stopping(f) == set()
    with pytest.raises(ValueError):
        allocations.stop(f, "orb_sip")


class _MD:
    def __init__(self, closes, last):
        self.closes, self.last = closes, last

    def daily_bars(self, sym, n):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [Bar(sym, t0 + timedelta(days=i), c, c, c, c, 1e9) for i, c in enumerate(self.closes[-n:])]

    def quotes(self, syms):
        return {s: Quote(s, self.last, self.last, self.last) for s in syms}


def test_launched_core_trades_alone_with_its_own_capital_and_stop_sells():
    md = _MD([400 + i for i in range(200)], 650.0)
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(md, 5000.0, Path(d) / "l.json", slippage_bps=0, clock=lambda: NOW)
        s = Settings.from_env({"PAPER_LEDGER_PATH": str(broker.ledger_path), "ENTRY_MODE": "auto", "MAX_TRADING_CAPITAL": "5000", "STARTING_CAPITAL": "5000"})
        spy_core = TrendCore(symbol="SPY", size_pct=0.4)
        spy_core.name = "spy_core"
        c = Components(s, md, [], [WeightedStrategy(TrendCore(size_pct=0.4), 1.0, standalone=True),
                                   WeightedStrategy(spy_core, 1.0, standalone=True)],
                       None, RiskManager(s.risk), broker, ["QQQ", "SPY"])
        st = AccountState(5000, 5000, 5000, 5000, 0)
        allocations.start(s.allocations_path, "trend_core", 1000, 5000)
        run_cycle(c, st, NOW)
        # the whole $1,000 (not 40% of the book), and the other core stays in shadow
        assert [(f.symbol, round(f.quantity * f.price)) for f in broker._fills] == [("QQQ", 1000)]
        assert {ws.strategy.name: ws.mode for ws in c.strategies} == {"trend_core": "trade", "spy_core": "shadow"}

        allocations.stop(s.allocations_path, "trend_core")
        st.cash = broker.cash()
        run_cycle(c, st, NOW + timedelta(minutes=5))
        assert broker.positions() == []
        assert allocations.load(s.allocations_path)["trend_core"]["status"] == "stopped"
        # the launcher stays in charge: a stopped strategy doesn't buy straight back in via strategies.toml
        assert {ws.strategy.name: ws.mode for ws in c.strategies} == {"trend_core": "shadow", "spy_core": "shadow"}


def test_launch_endpoints_validate_and_need_the_token(tmp_path):
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from ctbot.dashboard.server import make_handler

    root = Path(__file__).resolve().parents[1]
    cfg = (root / "config" / "strategies.toml").read_text()
    i = cfg.index("[vwap_revert]")  # a copy with one strategy switched off
    (tmp_path / "strategies.toml").write_text(cfg[:i] + cfg[i:].replace("enabled = true", "enabled = false", 1))
    s = Settings.from_env({"PAPER_LEDGER_PATH": str(tmp_path / "ledger.json"), "LOG_DIR": str(tmp_path / "logs"),
                           "MARKET_DATA": "csv", "MAX_TRADING_CAPITAL": "5000",
                           "STRATEGIES_FILE": str(tmp_path / "strategies.toml")})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(s, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    page = urllib.request.urlopen(base + "/").read().decode()
    token = page.split('name="ctbot-token" content="', 1)[1].split('"', 1)[0]

    def post(path, body, tok=token):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Ctbot-Token": tok})
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert post("/api/launch", {"strategy": "trend_core", "capital": 1000}, tok="nope") == 403
        assert post("/api/launch", {"strategy": "vwap_revert", "capital": 1000}) == 400  # switched off in the config
        assert post("/api/launch", {"strategy": "no_such_strategy", "capital": 1000}) == 400
        assert post("/api/launch", {"strategy": "trend_core", "capital": 9000}) == 400   # over the paper cap
        assert post("/api/launch", {"strategy": "trend_core", "capital": "lots"}) == 400
        assert post("/api/launch", {"strategy": "trend_core", "capital": 1000}) == 200
        assert allocations.running(s.allocations_path) == {"trend_core": 1000.0}
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert state["launcher"]["allocated"] == 1000.0
        assert post("/api/launch/stop", {"strategy": "trend_core"}) == 200
        assert allocations.stopping(s.allocations_path) == {"trend_core"}
        from ctbot import autopilot  # the page's save keeps a floor it doesn't send

        autopilot.save(s.autopilot_path, autopilot.AutopilotConfig(min_confidence=0.45))
        assert post("/api/autopilot", {"enabled": True, "max_loss_pct": 0.05, "giveback_pct": 0.06}) == 200
        assert autopilot.load(s.autopilot_path) == autopilot.AutopilotConfig(True, 0.05, 0.06, 0.45)
        assert post("/api/autopilot", {"enabled": True, "max_loss_pct": 0.9, "giveback_pct": 0.06}) == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_max_loss_stops_the_run_and_sells():
    md = _MD([400 + i for i in range(200)], 650.0)
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(md, 5000.0, Path(d) / "l.json", slippage_bps=0, clock=lambda: NOW)
        s = Settings.from_env({"PAPER_LEDGER_PATH": str(broker.ledger_path), "ENTRY_MODE": "auto", "MAX_TRADING_CAPITAL": "5000", "STARTING_CAPITAL": "5000"})
        c = Components(s, md, [], [WeightedStrategy(TrendCore(size_pct=0.4, disaster_stop=0.5), 1.0, standalone=True)],
                       None, RiskManager(s.risk), broker, ["QQQ"])
        st = AccountState(5000, 5000, 5000, 5000, 0)
        allocations.start(s.allocations_path, "trend_core", 1000, 5000, max_loss_pct=0.10)
        run_cycle(c, st, NOW)
        assert round(broker.positions()[0].quantity * 650) == 1000
        md.last = 580.0  # -10.8% on the $1,000, still above the 170/200-day average (~550)
        st.cash = broker.cash()
        run_cycle(c, st, NOW + timedelta(minutes=5))
        assert broker.positions() == []
        run = allocations.load(s.allocations_path)["trend_core"]
        assert run["status"] == "stopped" and run["reason"].startswith("max loss") and run["realized"] < -100


def test_rule_based_entries_skip_the_review_cap():
    from ctbot.aggregation.ensemble import EnsembleParams, combine
    from ctbot.models import Direction, Signal

    sigs = [Signal(f"m{i}", f"S{i}", Direction.LONG, 1.0, 1) for i in range(6)]
    sigs.append(Signal("core", "QLD", Direction.LONG, 1.0, 365, {"systematic": True}))
    weights = {s.strategy: 1.0 for s in sigs}
    out = combine(sigs, weights, EnsembleParams(min_agreeing_strategies=1, min_long_score=0.0), frozenset(weights))
    assert "QLD" in [c.symbol for c in out] and len(out) == 6  # 5 reviewed + the free rule-based one


def test_each_strategy_keeps_its_own_cash_and_rule_entries_retry():
    md = _MD([400 + i for i in range(200)], 650.0)
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(md, 5000.0, Path(d) / "l.json", slippage_bps=0, clock=lambda: NOW)
        broker._cash = 1000.0
        s = Settings.from_env({"PAPER_LEDGER_PATH": str(broker.ledger_path), "ENTRY_MODE": "auto", "MAX_TRADING_CAPITAL": "5000", "STARTING_CAPITAL": "5000"})
        spy_core = TrendCore(symbol="SPY", size_pct=0.4)
        spy_core.name = "spy_core"
        c = Components(s, md, [], [WeightedStrategy(TrendCore(size_pct=0.4), 1.0, standalone=True),
                                   WeightedStrategy(spy_core, 1.0, standalone=True)],
                       None, RiskManager(s.risk), broker, ["QQQ", "SPY"])
        st = AccountState(5000, 1000, 5000, 5000, 0)
        allocations.start(s.allocations_path, "trend_core", 1000, 5000)
        allocations.start(s.allocations_path, "spy_core", 1000, 5000)
        run_cycle(c, st, NOW)
        # $1,000 of cash, $2,000 promised: whoever goes first can't spend the other's share
        assert broker._fills == []
        broker._cash = 2000.0  # e.g. another position was sold
        st.cash = broker.cash()
        run_cycle(c, st, NOW + timedelta(minutes=5))  # same 15:30-15:44 window: rule entries retry
        assert sorted((f.symbol, round(f.quantity * f.price)) for f in broker._fills) == [("QQQ", 1000), ("SPY", 1000)]


def test_dashboard_shows_each_runs_idle_cash(tmp_path):
    """Cash a running strategy has not spent is held for it, not free: the dashboard says so."""
    from ctbot.dashboard.state import _launcher

    s = Settings.from_env({"MAX_TRADING_CAPITAL": "5000"})
    allocations.start(s.allocations_path, "orb", 2000, 5000)
    allocations.start(s.allocations_path, "trend_core", 1000, 5000)
    allocations.start(s.allocations_path, "rsi2_etf", 1000, 5000)
    allocations.stop(s.allocations_path, "rsi2_etf")
    positions = [{"symbol": "GE", "model": "orb", "quantity": 2.0, "avg_cost": 300.0, "market_value": 610.0, "pnl": 10.0},
                 {"symbol": "QQQ", "model": "trend_core", "quantity": 2.0, "avg_cost": 520.0, "market_value": 1030.0,
                  "pnl": -10.0}]
    L = _launcher(s, positions)
    runs = {r["strategy"]: r for r in L["runs"]}
    assert runs["orb"]["cost"] == 600.0 and runs["orb"]["waiting"] == 1400.0
    assert runs["trend_core"]["waiting"] == 0.0  # spent a little over its amount: never negative
    assert runs["rsi2_etf"]["waiting"] == 0.0  # selling: its money is not held back
    assert L["reserved"] == 1400.0 and L["allocated"] == 3000.0
