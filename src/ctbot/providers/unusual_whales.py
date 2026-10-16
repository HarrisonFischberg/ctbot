"""Unusual Whales provider (not yet implemented).

Intended coverage: unusual options flow (sweeps, block trades, premium, sentiment)
and congressional trades. Requires a paid API key in UNUSUAL_WHALES_API_KEY.
For OPTIONS_FLOW events, put option_type, premium_usd, expiry, strike, is_sweep
and open_interest in `raw`; strategies.options_flow reads those keys.
"""

from __future__ import annotations

from datetime import date

from ctbot.models import AltDataEvent, EventKind
from ctbot.providers.base import ProviderNotConfigured


class UnusualWhalesProvider:
    name = "unusual_whales"
    supports = frozenset({EventKind.OPTIONS_FLOW, EventKind.CONGRESS_TRADE})

    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise ProviderNotConfigured("UNUSUAL_WHALES_API_KEY is not set")
        self._api_key = api_key

    def fetch(self, kinds: set[EventKind], since: date) -> list[AltDataEvent]:
        raise NotImplementedError("Unusual Whales client not implemented")
