"""Process-level isolation between paper and live trading.

install_paper_guard() is called first thing in the paper entrypoint. After it runs,
any attempt to import ctbot.live (directly or transitively) raises ImportError in
that process, and the CTBOT_PAPER_PROCESS marker is set so the live adapter refuses
to construct even if loaded by some other path.
"""

from __future__ import annotations

import importlib.abc
import os
import sys

PAPER_MARKER_ENV = "CTBOT_PAPER_PROCESS"
BLOCKED_PREFIXES = ("ctbot.live",)


class LiveImportBlocked(ImportError):
    pass


class _BlockLiveFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if any(fullname == p or fullname.startswith(p + ".") for p in BLOCKED_PREFIXES):
            raise LiveImportBlocked(f"{fullname} cannot be imported in a paper-trading process")
        return None


def install_paper_guard() -> None:
    already_loaded = [m for m in sys.modules if m.startswith(BLOCKED_PREFIXES)]
    if already_loaded:
        raise RuntimeError(f"Live modules already loaded before paper guard: {already_loaded}")
    if not any(isinstance(f, _BlockLiveFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _BlockLiveFinder())
    os.environ[PAPER_MARKER_ENV] = "1"
    os.environ["CTBOT_MODE"] = "paper"
    os.environ["LIVE_TRADING_ENABLED"] = "false"


def in_paper_process() -> bool:
    return os.environ.get(PAPER_MARKER_ENV) == "1" or any(
        isinstance(f, _BlockLiveFinder) for f in sys.meta_path
    )
