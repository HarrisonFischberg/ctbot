"""Explicit records for each step of a decision, and the append-only journal that keeps them.

    MarketSnapshot -> StrategyResult(s) -> ProposedOrder -> ExecutionOutcome, plus one CycleSummary
    per cycle (with an explicit "no_trade" outcome when nothing qualified).

Every record carries a UTC timestamp, its data source and the strategy/model version, so a later
review can tell what the bot knew, from where, and why it acted. The journal is one JSON line per
record in data/journal.jsonl. Writing it never blocks trading: failures are logged and ignored.
Quantities, prices and limits in these records come from code (risk manager, broker), never from
an LLM; an LLM's contribution is only the `rationale` text and its confidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def strategy_version(strategy: Any) -> str:
    """Stable short hash of a strategy's class and simple parameters."""
    params = {k: v for k, v in sorted(vars(strategy).items())
              if not k.startswith("_") and isinstance(v, (int, float, str, bool, tuple, list, type(None)))}
    raw = json.dumps([type(strategy).__name__, params], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


@dataclass(frozen=True)
class MarketSnapshot:
    source: str
    quotes: dict[str, dict[str, Any]]          # symbol -> bid, ask, last, ts
    max_age_s: float | None = None
    ts: str = field(default_factory=_now)
    kind: str = "snapshot"


@dataclass(frozen=True)
class StrategyResult:
    strategy: str
    version: str
    mode: str                                   # trade | shadow
    signals: list[dict[str, Any]]
    ts: str = field(default_factory=_now)
    kind: str = "strategy_result"


@dataclass(frozen=True)
class ProposedOrder:
    client_order_id: str
    symbol: str
    side: str
    notional: float
    limit_price: float | None
    strategies: str | None
    ts: str = field(default_factory=_now)
    kind: str = "proposed_order"


@dataclass(frozen=True)
class ExecutionOutcome:
    client_order_id: str
    symbol: str
    status: str                                 # filled | refused | no_fill
    quantity: float | None = None
    price: float | None = None
    reason: str | None = None
    simulated: bool = True
    ts: str = field(default_factory=_now)
    kind: str = "execution_outcome"


@dataclass(frozen=True)
class CycleSummary:
    cycle_ts: str
    signals: int
    candidates: int
    orders_submitted: int
    outcome: str                                # traded | no_trade | market_closed
    reasons: dict[str, int] = field(default_factory=dict)
    ts: str = field(default_factory=_now)
    kind: str = "cycle"


class Journal:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def write(self, record: Any) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as fh:
                fh.write(json.dumps(asdict(record), sort_keys=True, default=str) + "\n")
        except (OSError, TypeError, ValueError):
            log.exception("journal write failed")

    def read(self, kind: str | None = None) -> list[dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if kind is None or r.get("kind") == kind:
                out.append(r)
        return out
