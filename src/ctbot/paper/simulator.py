"""Simulated broker: fills against quotes with slippage, persists a JSON ledger.

Models a cash account: sale proceeds settle T+1 (next weekday) and only settled cash
can buy, so the bot cannot trade on unsettled funds (a good-faith violation at a broker).
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
    ) -> None:
        self.md = market_data
        self.slippage_bps = slippage_bps
        self.ledger_path = ledger_path
        self.clock = clock
        self.instant_settlement = instant_settlement  # margin-style: proceeds usable at once
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
            notional = min(order.notional, self._cash)
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

    def equity(self) -> float:
        quotes = self.md.quotes(list(self._positions))
        mv = sum(
            p.quantity * quotes[p.symbol].mid for p in self._positions.values() if p.symbol in quotes
        )
        return self.cash() + self.unsettled_cash() + mv

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
