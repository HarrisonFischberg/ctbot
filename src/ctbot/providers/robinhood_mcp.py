"""Robinhood MCP as an alt-data source (not yet implemented).

The Robinhood MCP exposes read-only research tools usable as signal inputs,
e.g. get_politician_trades, get_equity_analyst_ratings, get_earnings_calendar.
Like market_data.robinhood_readonly, it is reachable through Claude (MCP connector),
not plain Python. Only READ_ONLY_TOOLS may ever be referenced here.
"""

from __future__ import annotations

from datetime import date

from ctbot.models import AltDataEvent, EventKind

READ_ONLY_TOOLS = frozenset(
    {"get_politician_trades", "get_equity_analyst_ratings", "get_earnings_calendar"}
)


class RobinhoodMcpProvider:
    name = "robinhood_mcp"
    supports = frozenset({EventKind.CONGRESS_TRADE})

    def fetch(self, kinds: set[EventKind], since: date) -> list[AltDataEvent]:
        raise NotImplementedError("Robinhood MCP alt-data not wired yet")
