"""Shared domain types passed between modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Direction(str, Enum):
    LONG = "long"    # open or add to a long
    EXIT = "exit"    # close an existing long
    SHORT = "short"  # bearish; not executable in a cash account


# ---- Market data ------------------------------------------------------------

@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    ts: datetime = field(default_factory=_now)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid and self.ask else self.last


# ---- Alternative data -------------------------------------------------------

class EventKind(str, Enum):
    CONGRESS_TRADE = "congress_trade"          # STOCK Act disclosures
    INSIDER_TRADE = "insider_trade"            # SEC Form 4
    INSTITUTIONAL_HOLDING = "institutional"    # SEC 13F position changes
    INVESTOR_TRADE = "investor_trade"          # tracked investors / copy-trading feeds
    OPTIONS_FLOW = "options_flow"              # unusual options activity


@dataclass(frozen=True)
class AltDataEvent:
    """Normalized record from any external provider (Quiver, Unusual Whales, Robinhood MCP, ...)."""

    kind: EventKind
    symbol: str
    side: Side
    actor: str                     # politician, insider, fund, investor, or "flow"
    event_date: date               # when the trade happened
    disclosed_date: date           # when it became public (lag matters)
    source: str                    # provider name
    size_usd: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def disclosure_lag_days(self) -> int:
        return (self.disclosed_date - self.event_date).days


# ---- Strategy output --------------------------------------------------------

@dataclass(frozen=True)
class Signal:
    """One strategy's opinion on one symbol."""

    strategy: str
    symbol: str
    direction: Direction
    strength: float                # 0..1, strategy-internal conviction
    horizon_days: float            # expected holding period
    evidence: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class Candidate:
    """Ensemble of signals for one symbol, ranked for Claude review."""

    symbol: str
    direction: Direction
    score: float                   # weighted, signed-agreement score
    signals: tuple[Signal, ...]

    @property
    def strategies(self) -> tuple[str, ...]:
        return tuple(sorted({s.strategy for s in self.signals}))

    @property
    def requires_short(self) -> bool:
        return self.direction is Direction.SHORT


# ---- Decision / execution ---------------------------------------------------

@dataclass(frozen=True)
class Decision:
    """Claude's structured verdict on a candidate."""

    approve: bool
    confidence: float
    rationale: str
    suggested_notional: float
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    notional: float  # dollar amount; Robinhood supports fractional dollar orders
    limit_price: float | None = None
    client_order_id: str = field(default_factory=lambda: uuid4().hex)
    candidate_ref: str | None = None


@dataclass(frozen=True)
class Fill:
    client_order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    ts: datetime = field(default_factory=_now)
    simulated: bool = True
    tag: str | None = None  # which model(s) produced the order, or the exit reason


@dataclass
class Position:
    symbol: str
    quantity: float
    avg_cost: float
    opened_at: datetime = field(default_factory=_now)
