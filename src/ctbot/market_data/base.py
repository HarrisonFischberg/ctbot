"""Market data provider interface."""

from __future__ import annotations

from typing import Protocol

from ctbot.models import Bar, Quote


class MarketDataProvider(Protocol):
    def daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        """Return daily bars, oldest first."""
        ...

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        ...

    def is_market_open(self) -> bool:
        ...
