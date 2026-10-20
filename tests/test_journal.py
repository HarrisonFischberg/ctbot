"""Decision journal: each cycle ends in a summary, orders carry proposal and outcome records."""

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ctbot.config import Settings
from ctbot.contracts import Journal
from ctbot.models import Bar, Direction, Quote, Signal
from ctbot.paper.simulator import PaperBroker
from ctbot.pipeline import Components, run_cycle
from ctbot.risk.manager import AccountState, RiskManager
from ctbot.strategies.registry import WeightedStrategy

NOW = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)


class _MD:
    def daily_bars(self, symbol, n):
        return [Bar(symbol, datetime(2026, 10, 12, tzinfo=timezone.utc), 50, 50, 50, 50, 1e7)]

    def quotes(self, symbols):
        return {s: Quote(s, 50.0, 50.01, 50.0, ts=NOW) for s in symbols}

    def is_market_open(self):
        return True


class _Rule:
    name = "rule"
    event_kinds = frozenset()

    def __init__(self, signals):
        self.signals = signals

    def generate(self, ctx):
        return list(self.signals)


def _run(d, signals):
    settings = Settings.from_env({"ENTRY_MODE": "auto", "PAPER_LEDGER_PATH": str(Path(d) / "l.json")})
    broker = PaperBroker(_MD(), 250.0, Path(d) / "l.json", clock=lambda: NOW)
    journal = Journal(settings.journal_path)
    c = Components(settings, _MD(), [], [WeightedStrategy(_Rule(signals), 1.0, standalone=True)], None,
                   RiskManager(settings.risk), broker, ["KO"], journal=journal)
    run_cycle(c, AccountState(250, 250, 250, 250, 0), NOW)
    return journal


def test_no_trade_cycle_is_recorded():
    with tempfile.TemporaryDirectory() as d:
        j = _run(d, [])
        cycles = j.read("cycle")
        assert len(cycles) == 1 and cycles[0]["outcome"] == "no_trade"
        assert cycles[0]["reasons"] == {"no_candidates": 1}


def test_traded_cycle_has_result_order_and_outcome():
    sig = Signal("rule", "KO", Direction.LONG, 1.0, 5, {"systematic": True, "size_pct": 0.2})
    with tempfile.TemporaryDirectory() as d:
        j = _run(d, [sig])
        kinds = [r["kind"] for r in j.read()]
        assert kinds == ["strategy_result", "proposed_order", "execution_outcome", "snapshot", "cycle"]
        out = j.read("execution_outcome")[0]
        assert out["status"] == "filled" and out["price"] > 0
        assert j.read("strategy_result")[0]["version"]
        assert j.read("cycle")[0]["outcome"] == "traded"
