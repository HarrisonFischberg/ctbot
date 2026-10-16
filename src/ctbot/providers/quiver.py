"""Quiver Quantitative provider (not yet implemented).

Intended coverage: congressional trades, insider (Form 4) trades, 13F institutional
holdings. Requires a paid API key in QUIVER_API_KEY. Map each record to AltDataEvent;
keep vendor-specific fields in `raw`.
"""

from __future__ import annotations

from datetime import date

from ctbot.models import AltDataEvent, EventKind
from ctbot.providers.base import ProviderNotConfigured


class QuiverProvider:
    name = "quiver"
    supports = frozenset(
        {EventKind.CONGRESS_TRADE, EventKind.INSIDER_TRADE, EventKind.INSTITUTIONAL_HOLDING}
    )

    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise ProviderNotConfigured("QUIVER_API_KEY is not set")
        self._api_key = api_key

    def fetch(self, kinds: set[EventKind], since: date) -> list[AltDataEvent]:
        raise NotImplementedError("Quiver client not implemented")
