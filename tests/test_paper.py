"""PaperBroker ledger persistence."""

import tempfile
from pathlib import Path

from ctbot.models import OrderRequest, Quote, Side
from ctbot.paper.simulator import PaperBroker


class _FixedQuotes:
    def quotes(self, symbols):
        return {s: Quote(s, 50.0, 50.0, 50.0) for s in symbols}


def test_ledger_reload_restores_cash_positions_and_fills():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ledger.json"
        b = PaperBroker(_FixedQuotes(), 250.0, path, slippage_bps=0)
        b.submit(OrderRequest("KO", Side.BUY, 50.0))
        b.submit(OrderRequest("KO", Side.SELL, 25.0))

        again = PaperBroker(_FixedQuotes(), 250.0, path, slippage_bps=0)
    assert again.cash() == b.cash()
    assert [(p.symbol, p.quantity) for p in again.positions()] == [("KO", 0.5)]
    assert [(f.side, f.quantity) for f in again._fills] == [(Side.BUY, 1.0), (Side.SELL, 0.5)]


def test_sale_proceeds_settle_next_business_day():
    from datetime import datetime, timezone

    now = [datetime(2026, 10, 16, 15, tzinfo=timezone.utc)]  # Friday
    with tempfile.TemporaryDirectory() as d:
        b = PaperBroker(_FixedQuotes(), 100.0, Path(d) / "l.json", slippage_bps=0,
                        clock=lambda: now[0])
        b.submit(OrderRequest("KO", Side.BUY, 100.0))
        b.submit(OrderRequest("KO", Side.SELL, 100.0))
        assert (b.cash(), b.unsettled_cash(), b.equity()) == (0.0, 100.0, 100.0)
        assert b.submit(OrderRequest("KO", Side.BUY, 10.0)) is None  # no free-riding
        now[0] = datetime(2026, 10, 19, 14, tzinfo=timezone.utc)  # Monday
        assert (b.cash(), b.unsettled_cash()) == (100.0, 0.0)


def test_instant_settlement_frees_proceeds_immediately():
    with tempfile.TemporaryDirectory() as d:
        b = PaperBroker(_FixedQuotes(), 100.0, Path(d) / "l.json", slippage_bps=0, instant_settlement=True)
        b.submit(OrderRequest("KO", Side.BUY, 100.0))
        b.submit(OrderRequest("KO", Side.SELL, 100.0))
        assert (b.cash(), b.unsettled_cash()) == (100.0, 0.0)
