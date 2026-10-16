"""Alternative-data provider interface."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from ctbot.models import AltDataEvent, EventKind


class ProviderNotConfigured(RuntimeError):
    """Raised when a provider is enabled without credentials or wiring."""


class AltDataProvider(Protocol):
    name: str
    supports: frozenset[EventKind]

    def fetch(self, kinds: set[EventKind], since: date) -> list[AltDataEvent]:
        """Return events of the requested kinds disclosed on or after `since`.

        Providers ignore kinds they do not support and never raise for them.
        """
        ...
