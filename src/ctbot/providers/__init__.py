"""External alternative-data providers, normalized to AltDataEvent.

Every provider is read-only. Adding a new vendor = one class implementing
AltDataProvider plus one entry in registry.PROVIDER_FACTORIES.
"""

from ctbot.providers.base import AltDataProvider

__all__ = ["AltDataProvider"]
