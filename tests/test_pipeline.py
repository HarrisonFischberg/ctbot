"""run_cycle wiring: standalone strategies, post-fill state refresh, day-trade recording."""

import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from ctbot.config import Settings
from ctbot.models import Bar, Decision, Direction, Position, Quote, Signal
from ctbot.paper.simulator import PaperBroker
from ctbot.pipeline import Components, run_cycle
from ctbot.risk.manager import AccountState, RiskManager
from ctbot.strategies.registry import build_strategies

TODAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)  # Tue 11:00 ET


class _MD:
    def daily_bars(self, symbol, lookback_days):
        ts = datetime(2026, 10, 12, tzinfo=timezone.utc)
        return [Bar(symbol, ts, 50, 50, 50, 50, 1e7)]  # $500M/day

    def quotes(self, symbols):
        return {s: Quote(s, 50.0, 50.0, 50.0) for s in symbols}

    def is_market_open(self):
        return True


class _Strategy:
    event_kinds = frozenset()

    def __init__(self, name, signals):
        self.name = name
        self._signals = signals

    def generate(self, ctx):
        return list(self._signals)


class _Engine:
    def evaluate(self, candidate, account):
        return Decision(approve=True, confidence=0.9, rationale="", suggested_notional=50.0)


def _components(broker, strategies, entry_mode="auto"):
    return Components(
        # the broker's temp folder, so a real data/autopilot.json can't leak into these tests
        settings=Settings.from_env({"ENTRY_MODE": entry_mode, "PAPER_LEDGER_PATH": str(broker.ledger_path)}),
        market_data=_MD(),
        providers=[],
        strategies=strategies,
        engine=_Engine(),
        risk=RiskManager(Settings.from_env({}).risk),
        executor=broker,
        universe=[],
    )


def _state(broker):
    return AccountState(
        equity=250, cash=broker.cash(), peak_equity=250, day_start_equity=250,
        open_positions=len(broker.positions()),
    )


def test_registry_reads_standalone_flag():
    ws = build_strategies(
        {"copy_trading": {"enabled": True, "weight": 0.5, "standalone": True,
                          "params": {"tracked_actors": ["x"]}}}
    )
    assert ws[0].standalone and ws[0].weight == 0.5


def test_registry_rejects_non_liquidity_risk_overrides():
    import pytest

    cfg = {"copy_trading": {"enabled": True, "risk": {"max_position_pct": 1.0}}}
    with pytest.raises(ValueError, match="may only set"):
        build_strategies(cfg)


def test_real_config_relaxes_copy_trading_liquidity():
    from ctbot.strategies.registry import load_strategy_config, strategy_risk_overrides

    cfg = load_strategy_config(Path(__file__).resolve().parents[1] / "config" / "strategies.toml")
    cfg["copy_trading"]["enabled"] = True
    assert strategy_risk_overrides(build_strategies(cfg)) == {
        "copy_trading": {"min_share_price": 1.0, "min_avg_dollar_volume": 1_000_000.0}
    }


def test_standalone_entry_fills_and_updates_state():
    from ctbot.strategies.registry import WeightedStrategy

    sigs = [Signal("copy_trading", sym, Direction.LONG, 1.0, 30) for sym in ("KO", "PEP")]
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(_MD(), 250.0, Path(d) / "ledger.json", slippage_bps=0,
                             clock=lambda: NOW)
        strat = WeightedStrategy(_Strategy("copy_trading", sigs), 0.5, standalone=True)
        state = _state(broker)
        run_cycle(_components(broker, [strat]), state, NOW)
    assert {p.symbol for p in broker.positions()} == {"KO", "PEP"}
    assert state.open_positions == 2 and state.cash == broker.cash() == 150.0


def test_same_day_exit_records_day_trade():
    from ctbot.strategies.registry import WeightedStrategy

    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(_MD(), 200.0, Path(d) / "ledger.json", slippage_bps=0,
                             clock=lambda: NOW)
        broker._positions["KO"] = Position(
            "KO", 1.0, 50.0, datetime(2026, 10, 13, 15, tzinfo=timezone.utc)
        )
        exit_sig = Signal("momentum", "KO", Direction.EXIT, 1.0, 1)
        strat = WeightedStrategy(_Strategy("momentum", [exit_sig]), 1.0)
        state = _state(broker)
        run_cycle(_components(broker, [strat]), state, NOW)
    assert broker.positions() == []
    assert state.day_trade_dates == [TODAY]
    # Cash account: the $50 sale settles T+1, so settled cash stays $200 today.
    assert state.open_positions == 0 and state.cash == 200.0
    assert broker.unsettled_cash() == 50.0


class _Quotes(_MD):
    def __init__(self, px):
        self.px = px

    def quotes(self, symbols):
        return {s: Quote(s, self.px, self.px, self.px) for s in symbols}


def _held_broker(d, md, opened):
    broker = PaperBroker(md, 200.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW)
    broker._positions["KO"] = Position("KO", 1.0, 50.0, opened)
    return broker


def test_stop_loss_exits_without_any_strategy_signal():
    md = _Quotes(47.0)  # -6% vs default 5% stop off $50 avg cost
    with tempfile.TemporaryDirectory() as d:
        broker = _held_broker(d, md, datetime(2026, 10, 1, tzinfo=timezone.utc))
        c = _components(broker, [])
        c.market_data = md
        state = _state(broker)
        run_cycle(c, state, NOW)
    assert broker.positions() == [] and "KO" not in state.brackets
    assert state.day_trade_dates == []  # opened on an earlier day


def test_market_closed_skips_cycle_and_late_entries_are_blocked():
    from ctbot.strategies.registry import WeightedStrategy

    sig = Signal("copy_trading", "KO", Direction.LONG, 1.0, 30)
    strat = WeightedStrategy(_Strategy("copy_trading", [sig]), 0.5, standalone=True)
    saturday = datetime(2026, 10, 17, 15, tzinfo=timezone.utc)
    late = datetime(2026, 10, 13, 19, 50, tzinfo=timezone.utc)  # 15:50 ET
    for now in (saturday, late):
        with tempfile.TemporaryDirectory() as d:
            broker = PaperBroker(_MD(), 250.0, Path(d) / "ledger.json", clock=lambda: now)
            run_cycle(_components(broker, [strat]), _state(broker), now)
        assert broker.positions() == [], now


def test_entry_fill_sets_bracket_from_decision():
    from ctbot.strategies.registry import WeightedStrategy

    sig = Signal("copy_trading", "KO", Direction.LONG, 1.0, 30)
    strat = WeightedStrategy(_Strategy("copy_trading", [sig]), 0.5, standalone=True)
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(_MD(), 250.0, Path(d) / "ledger.json", slippage_bps=0,
                             clock=lambda: NOW)
        state = _state(broker)
        run_cycle(_components(broker, [strat]), state, NOW)
    b = dict(state.brackets["KO"])
    ctx = b.pop("ctx")  # the trade log's entry context rides on the bracket
    assert b == {"stop": 47.5, "target": None}  # 5% default stop
    assert ctx["models"] == ["copy_trading"] and ctx["confidence"] == 0.9


def test_manual_mode_records_recommendation_without_buying():
    from ctbot.strategies.registry import WeightedStrategy

    sig = Signal("copy_trading", "KO", Direction.LONG, 1.0, 30)
    strat = WeightedStrategy(_Strategy("copy_trading", [sig]), 0.5, standalone=True)
    ideas = []
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(_MD(), 250.0, Path(d) / "l.json", clock=lambda: NOW)
        c = _components(broker, [strat], entry_mode="manual")
        c.recorder = ideas.append
        run_cycle(c, _state(broker), NOW)
    assert broker.positions() == []
    assert [(i["symbol"], i["status"], i["risk"]["allowed"]) for i in ideas] == [("KO", "recommended", True)]
    assert ideas[0]["decision"]["approve"] and ideas[0]["risk"]["notional"] == 50.0


def test_dashboard_click_buys_then_sells_through_risk_checks():
    from ctbot.pipeline import process_manual_order

    def order(side, ts=NOW, **kw):
        return {"id": "x", "ts": ts.isoformat(), "side": side, "symbol": "KO", **kw}

    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(_MD(), 250.0, Path(d) / "l.json", slippage_bps=0, clock=lambda: NOW)
        c = _components(broker, [], entry_mode="manual")
        state = _state(broker)
        st, reasons, fill = process_manual_order(c, state, order("buy", notional=500.0), NOW)
        assert st == "filled" and fill["notional"] == 50.0  # capped at 20% of equity
        assert state.brackets["KO"]["stop"] == 47.5
        broker._positions["KO"].opened_at = NOW  # Position timestamps use the real clock
        st, reasons, _ = process_manual_order(c, state, order("buy", notional=20.0), NOW)
        assert st == "rejected" and "already_held" in reasons
        st, _, fill = process_manual_order(c, state, order("sell"), NOW)
        assert st == "filled" and fill["day_trade"] and broker.positions() == []

        old = NOW.replace(hour=14)  # an hour-old click
        assert process_manual_order(c, state, order("buy", ts=old), NOW)[0] == "expired"
        night = datetime(2026, 10, 14, 2, tzinfo=timezone.utc)
        assert process_manual_order(c, state, order("buy", ts=night), night)[1] == ["market_closed"]


def test_earnings_inside_signal_horizon_skips_the_paid_review():
    """A plan-less signal (momentum, 30-day horizon) with a report in 10 days would be refused by the
    hard rule after the review; the pipeline refuses it first so the review slot isn't spent."""
    from datetime import timedelta

    from ctbot.strategies.registry import WeightedStrategy

    class _Ctx:
        def next_earnings(self, symbol):
            return NOW + timedelta(days=10)

        def for_symbol(self, symbol):
            return {}

    class _Counting(_Engine):
        calls = 0

        def evaluate(self, candidate, account):
            _Counting.calls += 1
            return super().evaluate(candidate, account)

    sig = Signal("copy_trading", "KO", Direction.LONG, 1.0, 30)
    strat = WeightedStrategy(_Strategy("copy_trading", [sig]), 0.5, standalone=True)
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(_MD(), 250.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW)
        c = _components(broker, [strat])
        c.engine, c.context = _Counting(), _Ctx()
        run_cycle(c, _state(broker), NOW)
    assert broker.positions() == [] and _Counting.calls == 0


def test_next_cycle_reviews_a_new_name_instead_of_repeating_the_top_one():
    from ctbot.strategies.registry import WeightedStrategy

    seen = []

    class _No(_Engine):
        def evaluate(self, candidate, account):
            seen.append(candidate.symbol)
            return Decision(approve=False, confidence=0.2, rationale="", suggested_notional=50.0)

    sigs = [Signal("copy_trading", "KO", Direction.LONG, 1.0, 1), Signal("copy_trading", "PEP", Direction.LONG, 0.9, 1)]
    strat = WeightedStrategy(_Strategy("copy_trading", sigs), 0.5, standalone=True)
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(_MD(), 250.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW)
        c = _components(broker, [strat])
        c.engine = _No()
        c.settings = Settings.from_env({"ENTRY_MODE": "auto", "PAPER_LEDGER_PATH": str(broker.ledger_path),
                                        "MAX_CANDIDATES_PER_CYCLE": "1"})
        state = _state(broker)
        run_cycle(c, state, NOW)
        run_cycle(c, state, NOW)
    assert seen == ["KO", "PEP"]


def test_top_up_never_deploys_past_max_trading_capital():
    from ctbot.models import Candidate, OrderRequest, Side
    from ctbot.pipeline import _maybe_top_up
    from ctbot.strategies.registry import WeightedStrategy

    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(_MD(), 500.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW)
        broker.submit(OrderRequest("QQQ", Side.BUY, 100.0))  # the trend core, under its $150 target
        broker.submit(OrderRequest("KO", Side.BUY, 150.0))   # another model: $250 deployed, the cap
        c = _components(broker, [WeightedStrategy(_Strategy("core", []), 1.0)])
        state = AccountState(equity=500, cash=broker.cash(), peak_equity=500, day_start_equity=500,
                             open_positions=2, brackets={"QQQ": {"stop": 40.0, "model": "core"}})
        sig = Signal("core", "QQQ", Direction.LONG, 1.0, 30, {"rebalance": True, "size_pct": 0.6})
        _maybe_top_up(c, state, Candidate("QQQ", Direction.LONG, 1.0, (sig,)),
                      broker.positions()[0], Quote("QQQ", 50.0, 50.0, 50.0), NOW)
    assert broker.market_value() == 250.0


def test_a_fill_saves_its_bracket_at_once():
    import json

    from ctbot.strategies.registry import WeightedStrategy

    sigs = [Signal("copy_trading", "KO", Direction.LONG, 1.0, 30)]
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(_MD(), 250.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW)
        c = _components(broker, [WeightedStrategy(_Strategy("copy_trading", sigs), 0.5, standalone=True)])
        run_cycle(c, _state(broker), NOW)  # nobody calls save_account_state after the cycle here
        saved = json.loads(c.settings.paper_risk_state_path.read_text())
    assert "KO" in saved["brackets"]
