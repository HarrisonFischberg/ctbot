"""Build enabled providers by name and fan out fetches."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from ctbot.models import AltDataEvent, EventKind
from ctbot.providers.base import AltDataProvider, ProviderNotConfigured

log = logging.getLogger(__name__)


def _json_file(env: dict[str, str]) -> AltDataProvider:
    from pathlib import Path

    from ctbot.providers.json_file import JsonFileProvider

    return JsonFileProvider(Path(env.get("EVENTS_DIR", "data/events")))


def _quiver(env: dict[str, str]) -> AltDataProvider:
    from ctbot.providers.quiver import QuiverProvider

    return QuiverProvider(env.get("QUIVER_API_KEY"))


def _unusual_whales(env: dict[str, str]) -> AltDataProvider:
    from ctbot.providers.unusual_whales import UnusualWhalesProvider

    return UnusualWhalesProvider(env.get("UNUSUAL_WHALES_API_KEY"))


def _robinhood_mcp(env: dict[str, str]) -> AltDataProvider:
    from ctbot.providers.robinhood_mcp import RobinhoodMcpProvider

    return RobinhoodMcpProvider()


def _collective2(env: dict[str, str]) -> AltDataProvider:
    from ctbot.providers.collective2 import Collective2Provider
    from ctbot.research.c2_rank import load_watchlist_ids

    return Collective2Provider(env.get("C2_API_KEY"), load_watchlist_ids())


PROVIDER_FACTORIES: dict[str, Callable[[dict[str, str]], AltDataProvider]] = {
    "json_file": _json_file,
    "quiver": _quiver,
    "unusual_whales": _unusual_whales,
    "robinhood_mcp": _robinhood_mcp,
    "collective2": _collective2,
}


def build_providers(names: list[str], env: dict[str, str]) -> list[AltDataProvider]:
    out: list[AltDataProvider] = []
    for name in names:
        factory = PROVIDER_FACTORIES.get(name)
        if factory is None:
            log.warning("unknown provider %r; skipping", name)
            continue
        try:
            out.append(factory(env))
        except ProviderNotConfigured as e:
            log.warning("provider %s disabled: %s", name, e)
    return out


def fetch_all(
    providers: list[AltDataProvider], kinds: set[EventKind], since: date
) -> list[AltDataEvent]:
    """One provider failing never blocks the others."""
    events: list[AltDataEvent] = []
    for p in providers:
        wanted = kinds & set(p.supports)
        if not wanted:
            continue
        try:
            events.extend(p.fetch(wanted, since))
        except NotImplementedError:
            log.info("provider %s not implemented; skipping", p.name)
        except Exception:
            log.exception("provider %s failed", p.name)
    return events
