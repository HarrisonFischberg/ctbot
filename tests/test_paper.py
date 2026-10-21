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


class _MovableQuotes:
    def __init__(self, px):
        self.px = px

    def quotes(self, symbols):
        return {s: Quote(s, self.px, self.px, self.px) for s in symbols}


def test_margin_buys_up_to_twice_equity_then_stops():
    with tempfile.TemporaryDirectory() as d:
        b = PaperBroker(_FixedQuotes(), 10_000.0, Path(d) / "l.json", slippage_bps=0,
                        instant_settlement=True, margin=2.0)
        assert b.buying_power() == 20_000.0
        assert b.submit(OrderRequest("KO", Side.BUY, 15_000.0)).quantity == 300.0
        assert b.cash() == -5_000.0 and b.equity() == 10_000.0  # $5,000 borrowed
        assert b.buying_power() == 5_000.0
        fill = b.submit(OrderRequest("PEP", Side.BUY, 9_000.0))
        assert fill.quantity * fill.price == 5_000.0  # capped at 2x equity
        assert b.buying_power() == 0.0


def test_no_margin_is_cash_only():
    with tempfile.TemporaryDirectory() as d:
        b = PaperBroker(_FixedQuotes(), 10_000.0, Path(d) / "l.json", slippage_bps=0)
        fill = b.submit(OrderRequest("KO", Side.BUY, 15_000.0))
        assert fill.quantity * fill.price == 10_000.0 and b.cash() == 0.0


def test_margin_needs_2000_equity_and_maintenance():
    with tempfile.TemporaryDirectory() as d:
        small = PaperBroker(_FixedQuotes(), 1_500.0, Path(d) / "s.json", slippage_bps=0, margin=2.0)
        assert small.buying_power() == 1_500.0  # under $2,000: own money only

        q = _MovableQuotes(50.0)
        b = PaperBroker(q, 10_000.0, Path(d) / "l.json", slippage_bps=0, margin=2.0)
        b.submit(OrderRequest("KO", Side.BUY, 20_000.0))  # 400 shares, $10,000 borrowed
        assert not b.margin_status()[1]
        q.px = 32.0  # positions $12,800, equity $2,800: under 25% maintenance ($3,200)
        assert b.margin_status() == (0.0, True)
        assert b.submit(OrderRequest("PEP", Side.BUY, 100.0)) is None
        assert b.submit(OrderRequest("KO", Side.SELL, 3_200.0)) is not None  # sells still work
