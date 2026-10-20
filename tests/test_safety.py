"""Defence in depth at the execution boundary: stale quotes, the HALT file, duplicate orders."""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctbot.config import RiskLimits, Settings
from ctbot.models import Candidate, Decision, Direction, OrderRequest, Quote, Side, Signal
from ctbot.paper.simulator import PaperBroker
from ctbot.pipeline import Components, _submit, process_manual_order
from ctbot.risk.manager import AccountState, RiskManager

NOW = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)


class _MD:
    def daily_bars(self, symbol, n):
        return []

    def quotes(self, symbols):
        return {s: Quote(s, 50.0, 50.01, 50.0, ts=NOW) for s in symbols}

    def is_market_open(self):
        return True


def _components(d):
    settings = Settings.from_env({"PAPER_LEDGER_PATH": str(Path(d) / "l.json"), "RESPECT_MARKET_HOURS": "false"})
    broker = PaperBroker(_MD(), 250.0, Path(d) / "l.json", clock=lambda: NOW)
    return Components(settings, _MD(), [], [], None, RiskManager(settings.risk), broker, ["KO"])


def test_stale_quote_refuses_entries():
    cand = Candidate("KO", Direction.LONG, 0.5, (Signal("m", "KO", Direction.LONG, 1, 1),))
    dec = Decision(True, 0.9, "", 50.0)
    st = AccountState(250, 250, 250, 250, 0)
    rm = RiskManager(RiskLimits())
    fresh = Quote("KO", 50, 50.01, 50, ts=NOW - timedelta(seconds=30))
    old = Quote("KO", 50, 50.01, 50, ts=NOW - timedelta(minutes=10))
    assert rm.check_entry(cand, dec, st, fresh, 1e9, now=NOW).allowed
    v = rm.check_entry(cand, dec, st, old, 1e9, now=NOW)
    assert not v.allowed and "stale_quote" in v.reasons


def test_halt_file_blocks_buys_not_sells():
    with tempfile.TemporaryDirectory() as d:
        c = _components(d)
        st = AccountState(250, 250, 250, 250, 0)
        c.settings.halt_path.write_text("stop")
        buy = OrderRequest("KO", Side.BUY, 50.0, limit_price=51.0)
        assert not _submit(c, st, buy, {}, NOW.date())
        status, reasons, _ = process_manual_order(
            c, st, {"symbol": "KO", "side": "buy", "ts": NOW.isoformat()}, NOW)
        assert status == "rejected" and reasons == ["halt_file_present"]
        c.settings.halt_path.unlink()
        assert _submit(c, st, buy, {}, NOW.date())
        positions = {p.symbol: p for p in c.executor.positions()}
        c.settings.halt_path.write_text("stop")
        sell = OrderRequest("KO", Side.SELL, 49.0, limit_price=49.0)
        c.recent_orders.clear()
        assert _submit(c, st, sell, positions, NOW.date())  # exits still run


def test_duplicate_order_is_refused():
    with tempfile.TemporaryDirectory() as d:
        c = _components(d)
        st = AccountState(250, 250, 250, 250, 0)
        buy = OrderRequest("KO", Side.BUY, 20.0, limit_price=51.0)
        assert _submit(c, st, buy, {}, NOW.date())
        assert not _submit(c, st, OrderRequest("KO", Side.BUY, 20.0, limit_price=51.0), {}, NOW.date())
        assert len(c.executor.positions()) == 1
