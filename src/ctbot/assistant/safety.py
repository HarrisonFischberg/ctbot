"""Keep secrets out of everything the assistant sends: the model, the browser and the audit log."""

from __future__ import annotations

import os
import re
from typing import Any

# Anthropic-style keys and long opaque tokens pasted into a chat, whatever env holds.
_PATTERNS = (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),)
REDACTED = "[redacted]"


def _secret_values() -> list[str]:
    """Values of every key the Settings tab can save, as this process holds them. Read only to
    scrub them out; never put into a prompt. The nickname is a label, not a secret."""
    from ctbot import secret_store

    names = [k for k in secret_store.KEYS if k != "ROBINHOOD_ACCOUNT_NICKNAME"]
    names += ["QUIVER_API_KEY", "UNUSUAL_WHALES_API_KEY", "LIVE_TRADING_CONFIRM"]
    vals = {os.environ.get(k, "").strip() for k in names}
    return sorted((v for v in vals if len(v) >= 6), key=len, reverse=True)


def redact(obj: Any) -> Any:
    """`obj` (str, list, dict) with any secret value or key-shaped string replaced."""
    secrets = _secret_values()

    def text(s: str) -> str:
        for v in secrets:
            if v in s:
                s = s.replace(v, REDACTED)
        for p in _PATTERNS:
            s = p.sub(REDACTED, s)
        return s

    def walk(o: Any) -> Any:
        if isinstance(o, str):
            return text(o)
        if isinstance(o, dict):
            return {walk(k) if isinstance(k, str) else k: walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        return o

    return walk(obj)
