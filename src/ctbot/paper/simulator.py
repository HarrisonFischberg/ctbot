"""Simulated broker: fills against quotes with slippage, persists a JSON ledger.

Models a cash account: sale proceeds settle T+1 (next weekday) and only settled cash
can buy, so the bot cannot trade on unsettled funds (a good-faith violation at a broker).
With margin > 1 it models a margin account instead: buys may borrow (cash goes negative) up
to margin x equity in total positions, only with at least $2,000 equity and while equity
covers the 25% maintenance requirement. No margin interest is charged.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from ctbot.market_data.base import MarketDataProvider
from ctbot.market_hours import next_business_day, trading_date
from ctbot.models import Fill, OrderRequest, Position, Side

MARGIN_MIN_EQUITY = 2_000.0   # FINRA: no borrowing below this equity
MAINTENANCE_PCT = 0.25        # FINRA baseline for long, margin-eligible stock (brokers may ask more)


def buying_power(cash: float, equity: float, market_value: float, margin: float) -> float:
    """Dollars a new buy may spend now. Cash only unless margin > 1, equity is at least
    $2,000 and the account meets maintenance; then up to margin x equity in positions."""
    if margin <= 1.0 or equity < MARGIN_MIN_EQUITY or margin_call(equity, market_value):
        return max(0.0, cash)
    return max(0.0, cash, margin * equity - market_value)


def margin_call(equity: float, market_value: float) -> bool:
    """Equity below the maintenance requirement on the positions held."""
    return market_value > 0 and equity < MAINTENANCE_PCT * market_value


class PaperBroker:
    is_live = False

    def __init__(
        self,
        market_data: MarketDataProvider,
        starting_cash: float,
        ledger_path: Path,
        slippage_bps: float = 5.0,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        instant_settlement: bool = False,
        margin: float = 1.0,
    ) -> None:
        self.md = market_data
        self.slippage_bps = slippage_bps
        self.ledger_path = ledger_path
        self.clock = clock
        self.instant_settlement = instant_settlement  # margin-style: proceeds usable at once
        self.margin = margin  # 1 = cash only; 2 = buy up to 2x equity (paper margin account)
        self._cash = starting_cash  # settled cash only
        self._unsettled: list[tuple[date, float]] = []  # (settle date, proceeds)
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._load()

    # ---- OrderExecutor -----------------------------------------------------
    def submit(self, order: OrderRequest) -> Fill | None:
        self._settle()
        quote = self.md.quotes([order.symbol]).get(order.symbol)
        if quote is None:
            return None
        slip = self.slippage_bps / 10_000
        if order.side is Side.BUY:
            price = quote.ask * (1 + slip)
            if order.limit_price is not None and price > order.limit_price * (1 + slip):
                return None
            notional = min(order.notional, self.buying_power())
            if notional <= 0:
                return None
            qty = notional / price
            self._cash -= qty * price
            pos = self._positions.get(order.symbol)
            if pos:
                total = pos.quantity + qty
                pos.avg_cost = (pos.avg_cost * pos.quantity + price * qty) / total
                pos.quantity = total
            else:
                self._positions[order.symbol] = Position(order.symbol, qty, price)
        else:
            pos = self._positions.get(order.symbol)
            if pos is None or pos.quantity <= 0:
                return None  # no simulated shorting
            price = quote.bid * (1 - slip)
            qty = min(pos.quantity, order.notional / price)
            if self.instant_settlement:
                self._cash += qty * price
            else:
                settles = next_business_day(trading_date(self.clock()))
                self._unsettled.append((settles, qty * price))
            pos.quantity -= qty
            if pos.quantity <= 1e-9:
                del self._positions[order.symbol]

        fill = Fill(order.client_order_id, order.symbol, order.side, qty, price, simulated=True,
                    tag=order.candidate_ref)
        self._fills.append(fill)
        self._save()
        return fill

    def cancel(self, client_order_id: str) -> bool:
        return False  # simulated orders fill or reject immediately

    def positions(self) -> list[Position]:
        return list(self._positions.values())

    def cash(self) -> float:
        """Settled cash: what can be spent now."""
        self._settle()
        return self._cash

    def unsettled_cash(self) -> float:
        self._settle()
        return sum(amount for _, amount in self._unsettled)

    def market_value(self) -> float:
        quotes = self.md.quotes(list(self._positions))
        return sum(
            p.quantity * quotes[p.symbol].mid for p in self._positions.values() if p.symbol in quotes
        )

    def equity(self) -> float:
        return self.cash() + self.unsettled_cash() + self.market_value()

    def buying_power(self) -> float:
        """What a buy may spend now: settled cash, plus a margin loan when margin > 1."""
        if self.margin <= 1.0:
            return max(0.0, self.cash())
        mv = self.market_value()
        return buying_power(self.cash(), self.cash() + self.unsettled_cash() + mv, mv, self.margin)

    def margin_status(self) -> tuple[float, bool]:
        """(buying power, in a maintenance call) from one price snapshot."""
        mv = self.market_value()
        equity = self.cash() + self.unsettled_cash() + mv
        return buying_power(self.cash(), equity, mv, self.margin), margin_call(equity, mv)

    def _settle(self) -> None:
        today = trading_date(self.clock())
        matured = [(d, a) for d, a in self._unsettled if d <= today]
        if not matured:
            return
        self._cash += sum(a for _, a in matured)
        self._unsettled = [(d, a) for d, a in self._unsettled if d > today]
        self._save()

    # ---- persistence -------------------------------------------------------
    def _save(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cash": self._cash,
            "unsettled": [{"settles": d.isoformat(), "amount": a} for d, a in self._unsettled],
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "avg_cost": p.avg_cost,
                    "opened_at": p.opened_at.isoformat(),
                }
                for p in self._positions.values()
            ],
            "fills": [
                {**asdict(f), "side": f.side.value, "ts": f.ts.isoformat()} for f in self._fills
            ],
        }
        self.ledger_path.write_text(json.dumps(data, indent=2))

    def _load(self) -> None:
        if not self.ledger_path.exists():
            return
        data = json.loads(self.ledger_path.read_text())
        self._cash = data["cash"]
        self._unsettled = [
            (date.fromisoformat(u["settles"]), u["amount"]) for u in data.get("unsettled", [])
        ]
        self._positions = {
            p["symbol"]: Position(
                p["symbol"], p["quantity"], p["avg_cost"], datetime.fromisoformat(p["opened_at"])
            )
            for p in data["positions"]
        }
        self._fills = [
            Fill(
                client_order_id=f["client_order_id"],
                symbol=f["symbol"],
                side=Side(f["side"]),
                quantity=f["quantity"],
                price=f["price"],
                ts=datetime.fromisoformat(f["ts"]),
                simulated=f.get("simulated", True),
                tag=f.get("tag"),
            )
            for f in data.get("fills", [])
        ]
