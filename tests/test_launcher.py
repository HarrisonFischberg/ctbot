"""Paper launcher: run one strategy with its own capital; stop sells what it opened."""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ctbot import allocations, auto_invest
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


def test_launch_endpoints_validate_and_need_the_token(tmp_path, monkeypatch):
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
        assert post("/api/autopilot", {"enabled": True, "max_loss_pct": 0.05, "giveback_pct": 0.06,
                                       "min_confidence": 0.2}) == 200  # the page's floor control
        assert autopilot.load(s.autopilot_path).min_confidence == 0.2
        assert json.loads(urllib.request.urlopen(base + "/api/state").read())["autopilot"]["min_confidence"] == 0.2
        assert post("/api/autopilot", {"enabled": True, "max_loss_pct": 0.05, "giveback_pct": 0.06,
                                       "min_confidence": 0.05}) == 400  # below the 10% bound
        import ctbot.dashboard.state as st  # auto-invest: picks winners, stops runs it didn't pick

        monkeypatch.setattr(st, "PERF_PATH", tmp_path / "perf.json")
        assert post("/api/launch/auto", {"amount": 4000}) == 400  # no simulated record yet
        win = {"windows": {"1Y": {"return": 0.2, "worst_drop": -0.1}}}
        st.PERF_PATH.write_text(json.dumps({"strategies": {"orb_sip": win, "rsi2_etf": win}}))
        allocations.start(s.allocations_path, "trend_core", 1000, 5000)
        assert post("/api/launch/auto", {"amount": 9000, "max_loss_pct": 0.15}) == 200  # capped at $5,000
        assert allocations.running(s.allocations_path) == {"orb_sip": 2500.0, "rsi2_etf": 2500.0}
        assert "trend_core" in allocations.stopping(s.allocations_path)
        assert post("/api/launch/auto/off", {}) == 200 and not auto_invest.path(s).exists()
        assert post("/api/paper/topup", {}) == 202  # the paper bot applies it
        assert json.loads(s.paper_ledger_path.with_name("paper_topup.json").read_text()) == {"to": 5000.0}
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


def test_auto_invest_picks_best_winners_one_per_family_and_splits_the_amount():
    w = lambda r, d, curve=(): {"windows": {"1Y": {"return": r, "worst_drop": d}}, "curve": list(curve)}
    perf = {"a": w(0.30, -0.10), "b": w(0.20, -0.40), "c": w(-0.05, -0.10), "d": w(0.10, -0.02), "e": {}}
    pick = auto_invest.pick
    assert pick(perf, ["a", "b", "c", "d", "e"], 20000, top=2) == {"d": 10000.0, "a": 10000.0}
    assert pick(perf, list(perf), 20000) == {"d": 6666.66, "a": 6666.66, "b": 6666.66}  # losers, no record out
    assert pick(perf, ["a", "b", "d"], 15) == {"a": 15.0}  # never a slice under $10
    assert pick(perf, ["c", "e"], 20000) == {}
    perf["f"] = w(0.33, -0.10)
    assert pick(perf, ["a", "f"], 1000, top=1) == {"f": 1000.0}
    assert pick(perf, ["a", "f"], 1000, keep=("a",), top=1) == {"a": 1000.0}  # a kept pick needs a 20% better rival
    ups = [[f"2026-01-0{i}", v] for i, v in enumerate([100, 103, 101, 106, 104, 110], 1)]
    twin = [[d, v * 2] for d, v in ups]
    perf.update(lib_x_etfs=w(0.3, -0.1, ups), lib_x_stocks=w(0.3, -0.1), g=w(0.3, -0.1, twin), h=w(0.29, -0.1))
    assert set(pick(perf, ["lib_x_etfs", "lib_x_stocks", "g", "h"], 1000)) == {"lib_x_etfs", "h"}  # same rule / look-alike out


def test_auto_invest_tops_up_and_repicks_weekly(tmp_path, monkeypatch):
    import json

    import ctbot.dashboard.state as st

    monkeypatch.setattr(st, "PERF_PATH", tmp_path / "perf.json")
    win = {"windows": {"1Y": {"return": 0.2, "worst_drop": -0.1}}}
    st.PERF_PATH.write_text(json.dumps({"strategies": {"orb_sip": win, "rsi2_etf": win}}))
    now = datetime.now(timezone.utc)  # run stops are stamped with the real clock
    s = Settings.from_env({"PAPER_LEDGER_PATH": str(tmp_path / "ledger.json"), "MAX_TRADING_CAPITAL": "4000"})
    assert auto_invest.run(s, now) == {}  # no plan: nothing happens
    auto_invest.save(s, 4000, 0.15)
    assert auto_invest.run(s, now) == {"orb_sip": 2000.0, "rsi2_etf": 2000.0}
    assert auto_invest.run(s, now + timedelta(hours=1)) == {}  # funded: nothing to do
    allocations.stop(s.allocations_path, "orb_sip", "max loss reached")
    assert auto_invest.run(s, now + timedelta(days=1)) == {}  # hit its max loss: not restarted this week
    assert auto_invest.run(s, now + timedelta(days=7)) == {"orb_sip": 2000.0}  # the weekly re-pick funds it again


def test_top_up_is_cash_not_a_gain(tmp_path):
    import json

    from ctbot import variants

    b = PaperBroker(None, 250.0, tmp_path / "ledger.json")
    b.deposit(19750.0)
    again = PaperBroker(None, 250.0, tmp_path / "ledger.json")
    assert again.cash() == 20000.0 and again.deposits == 19750.0
    h = tmp_path / "equity_history.jsonl"
    h.write_text("\n".join(json.dumps(r) for r in [{"ts": "2026-10-26T20:00", "equity": 250.0},
                                                       {"ts": "2026-10-27T20:00", "equity": 20010.0, "deposits": 19750.0}]))
    assert variants._daily_returns(h) == {"2026-10-27": pytest.approx(0.04)}  # $10 on $250, not +7,900%


def test_invested_strategies_trade_alongside_autopilot_on_their_own_money():
    from ctbot import autopilot

    md = _MD([400 + i for i in range(200)], 650.0)
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(md, 5000.0, Path(d) / "l.json", slippage_bps=0, clock=lambda: NOW)
        broker._cash = 1500.0
        s = Settings.from_env({"PAPER_LEDGER_PATH": str(broker.ledger_path), "ENTRY_MODE": "auto", "MAX_TRADING_CAPITAL": "5000", "STARTING_CAPITAL": "5000"})
        autopilot.save(s.autopilot_path, autopilot.AutopilotConfig(enabled=True))
        spy_core = TrendCore(symbol="SPY", size_pct=0.4)
        spy_core.name = "spy_core"
        c = Components(s, md, [], [WeightedStrategy(spy_core, 1.0, standalone=True),  # autopilot's model goes first
                                   WeightedStrategy(TrendCore(size_pct=0.4), 1.0, standalone=True)],
                       None, RiskManager(s.risk), broker, ["QQQ", "SPY"])
        st = AccountState(5000, 1500, 5000, 5000, 0)
        allocations.start(s.allocations_path, "trend_core", 1000, 5000)
        run_cycle(c, st, NOW)
        assert {ws.strategy.name: ws.mode for ws in c.strategies} == {"trend_core": "trade", "spy_core": "trade"}
        # the invested one gets its whole $1,000; autopilot's other model only the $500 not kept for it
        assert sorted((f.symbol, round(f.quantity * f.price)) for f in broker._fills) == [("QQQ", 1000), ("SPY", 500)]


def test_concurrent_edits_are_never_lost(tmp_path):
    import threading

    path = tmp_path / "allocations.json"
    allocations.start(path, "trend_core", 100.0, 250.0)
    threads = [threading.Thread(target=allocations.add_realized, args=(path, "trend_core", 1.0)) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert allocations.load(path)["trend_core"]["realized"] == 40.0
