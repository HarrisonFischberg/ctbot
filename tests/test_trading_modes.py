"""Trading tab modes: QQQ as market gauge and aggressive day trading (ctbot.trading_modes)."""

import json
import tempfile
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctbot import trading_modes
from ctbot.config import Mode
from ctbot.models import Bar, Candidate, Decision, Direction, OrderRequest, Position, Quote, Side, Signal
from ctbot.paper.simulator import PaperBroker
from ctbot.pipeline import _submit, load_modes, manage_positions, refresh_learning, run_cycle
from ctbot.risk.manager import AccountState
from ctbot.strategies.registry import WeightedStrategy
from tests.test_pipeline import TODAY, _components, _Strategy

NOW = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)      # Tue 11:00 ET
FLAT = datetime(2026, 10, 13, 19, 45, tzinfo=timezone.utc)  # Tue 15:45 ET


class _MD:
    """Every stock at $50 with $500M/day; QQQ at 90 under a flat 100 average."""

    def daily_bars(self, symbol, lookback_days):
        px = 100.0 if symbol == "QQQ" else 50.0
        return [Bar(symbol, NOW - timedelta(days=i + 1), px, px, px, px, 1e7) for i in range(lookback_days)]

    def quotes(self, symbols):
        return {s: Quote(s, 90.0, 90.0, 90.0) if s == "QQQ" else Quote(s, 50.0, 50.0, 50.0) for s in symbols}


def _setup(d, cash=250.0):
    broker = PaperBroker(_MD(), cash, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW)
    c = _components(broker, [])
    c.market_data = _MD()
    return broker, c


def _modes(c, **kw):
    trading_modes.save(c.settings.trading_modes_path, trading_modes.TradingModes(**kw))
    load_modes(c)


def _state(equity=250.0, cash=250.0, **kw):
    return AccountState(equity=equity, cash=cash, peak_equity=kw.pop("peak_equity", equity),
                        day_start_equity=kw.pop("day_start_equity", equity), open_positions=0,
                        day_start_date=TODAY, **kw)


def _cand(sym="KO"):
    return Candidate(sym, Direction.LONG, 0.5, (Signal("momentum", sym, Direction.LONG, 0.8, 5),))


DECISION = Decision(approve=True, confidence=0.9, rationale="", suggested_notional=50.0)


def test_qqq_gate_off_lets_a_buy_pass_and_on_blocks_it():
    with tempfile.TemporaryDirectory() as d:
        _, c = _setup(d)
        borrowing = _state(equity=5_000, cash=-1_000, buying_power=4_000)
        c.risk.limits = replace(c.risk.limits, max_trading_capital=1e9)

        _modes(c, qqq_gate=True)
        refresh_learning(c, borrowing, NOW)
        assert borrowing.risk_off == ["QQQ under its 50-day average"]
        v = c.risk.check_entry(_cand(), DECISION, borrowing, Quote("KO", 50, 50, 50), 1e9)
        assert v.reasons == ("pulled_back_no_borrowing",)

        _modes(c, qqq_gate=False)
        refresh_learning(c, borrowing, NOW)
        assert borrowing.risk_off == []
        assert c.risk.check_entry(_cand(), DECISION, borrowing, Quote("KO", 50, 50, 50), 1e9).allowed

        # the other pull-back triggers stay on with the gauge off
        down = _state(equity=9_600, cash=-1_000, buying_power=4_000, day_start_equity=10_000)
        refresh_learning(c, down, NOW)
        assert down.risk_off == ["down 4.0% today"]
        changes = trading_modes.changes(c.settings.trading_modes_path)
    assert [(ch["field"], ch["to"]) for ch in changes] == [("qqq_gate", False)]  # newest first, timestamped
    assert datetime.fromisoformat(changes[0]["ts"])


def _held(broker, state, sym, opened, **bracket):
    broker._positions[sym] = Position(sym, 1.0, 50.0, opened)
    state.brackets[sym] = {"stop": 45.0, "target": None, **bracket}


def test_aggressive_mode_is_in_cash_by_1545_except_the_trend_cores():
    later = (FLAT + timedelta(days=30)).isoformat()
    with tempfile.TemporaryDirectory() as d:
        broker, c = _setup(d, cash=1_000.0)
        state = _state(equity=1_250, cash=1_000)
        _held(broker, state, "AMD", FLAT - timedelta(hours=4), model="orb", exit_at=FLAT.replace(minute=50).isoformat())
        _held(broker, state, "KO", FLAT - timedelta(hours=2))  # Claude-reviewed, no plan: intraday
        _held(broker, state, "QQQ", FLAT - timedelta(hours=1), model="trend_core", exit_at=later)
        _held(broker, state, "QLD", FLAT - timedelta(days=7), model="trend_core_qld", exit_at=later)
        _held(broker, state, "SPY", FLAT - timedelta(minutes=10), model="overnight",
              exit_at=(FLAT + timedelta(days=1)).isoformat())
        cores = {s: asdict(broker._positions[s]) for s in ("QQQ", "QLD")}
        _modes(c, aggressive=True)

        manage_positions(c, state, FLAT - timedelta(minutes=1))  # 15:44: nothing sold yet
        assert len(broker.positions()) == 5

        orb = WeightedStrategy(_Strategy("orb", [Signal("orb", "MU", Direction.LONG, 1.0, 0.25)]), 1.0, standalone=True)
        c.strategies = [orb]
        run_cycle(c, state, FLAT)  # flattens, and buys nothing new after the flatten time
        held = {p.symbol for p in broker.positions()}
        assert held == {"QQQ", "QLD", "SPY"}  # cores and the planned overnight hold stay
        assert {s: asdict(broker._positions[s]) for s in ("QQQ", "QLD")} == cores
        sells = [f for f in json.loads(broker.ledger_path.read_text())["fills"] if f["side"] == "sell"]
        assert {f["symbol"] for f in sells} == {"AMD", "KO"} and {f["tag"] for f in sells} == {"eod_flatten"}

        _modes(c, aggressive=True, flatten_all=True)  # "also flatten these"
        manage_positions(c, state, FLAT + timedelta(minutes=1))
        assert broker.positions() == []


def test_kill_switches_still_stop_all_buys_in_aggressive_mode():
    orb = WeightedStrategy(_Strategy("orb", [Signal("orb", "KO", Direction.LONG, 1.0, 0.25)]), 1.0, standalone=True)
    for state, reason in [(_state(equity=240, day_start_equity=250), "daily_loss_limit"),
                          (_state(equity=200, peak_equity=250), "max_drawdown_kill")]:
        with tempfile.TemporaryDirectory() as d:
            broker, c = _setup(d)
            _modes(c, aggressive=True)
            c.strategies = [orb]
            ideas = []
            c.recorder = ideas.append
            run_cycle(c, state, NOW)
        assert broker.positions() == []
        assert [i["status"] for i in ideas] == ["rejected"] and reason in ideas[0]["risk"]["reasons"]


def test_aggressive_mode_trades_shadow_intraday_models_and_allows_round_trips():
    orb = WeightedStrategy(_Strategy("orb", [Signal("orb", "KO", Direction.LONG, 1.0, 0.25)]), 1.0,
                           standalone=True, mode="shadow")
    with tempfile.TemporaryDirectory() as d:
        broker, c = _setup(d)
        c.strategies = [orb]
        run_cycle(c, _state(), NOW)
        assert broker.positions() == []  # shadow: reviewed, never bought
        c.reviewed.clear()
        _modes(c, aggressive=True)
        state = _state()
        run_cycle(c, state, NOW)
        assert [p.symbol for p in broker.positions()] == ["KO"]
        assert any(k[1] == "KO" for k in c.reviewed)
        _submit(c, state, OrderRequest("KO", Side.SELL, 1e9), {p.symbol: p for p in broker.positions()}, TODAY)
        assert not any(k[1] == "KO" for k in c.reviewed)  # the next KO setup may be reviewed again today


def test_live_trading_settings_stay_untouched():
    with tempfile.TemporaryDirectory() as d:
        broker, c = _setup(d)
        before = c.settings
        cfg = trading_modes.validate({"qqq_gate": False, "aggressive": True, "live_trading_enabled": True,
                                      "max_daily_loss_pct": 0.9, "mode": "live"})
        assert set(asdict(cfg)) == set(asdict(trading_modes.TradingModes()))  # unknown keys never stored
        _modes(c, qqq_gate=False, aggressive=True)
        run_cycle(c, _state(), NOW)
        assert c.settings is before
        assert c.settings.mode is Mode.PAPER and not c.settings.live_trading_enabled
        assert c.settings.risk == before.risk and c.settings.max_claude_reviews_per_day == 40

        broker.is_live = True  # a live executor never reads the paper modes
        load_modes(c)
        assert c.modes == trading_modes.TradingModes()


def test_flatten_time_moves_up_on_an_early_close():
    cfg = trading_modes.TradingModes(aggressive=True)
    early = datetime(2026, 11, 27, 17, 45, tzinfo=timezone.utc)  # day after Thanksgiving, 12:45 ET; closes 13:00
    assert trading_modes.flatten_due(cfg, early)
    assert not trading_modes.flatten_due(cfg, early - timedelta(minutes=1))
    assert not trading_modes.flatten_due(trading_modes.TradingModes(), FLAT)
