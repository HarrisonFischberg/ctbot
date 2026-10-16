"""Strategy interface and the context passed to every strategy each cycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from ctbot.market_data.base import MarketDataProvider
from ctbot.models import AltDataEvent, EventKind, Signal


@dataclass(frozen=True)
class StrategyContext:
    market_data: MarketDataProvider
    universe: tuple[str, ...]
    events: tuple[AltDataEvent, ...]
    today: date
    held_symbols: frozenset[str] = frozenset()
    now: datetime | None = None  # tz-aware cycle time; intraday strategies need it


class Strategy(Protocol):
    name: str
    # Alt-data kinds this strategy consumes; empty for price-only strategies.
    event_kinds: frozenset[EventKind]

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        ...
