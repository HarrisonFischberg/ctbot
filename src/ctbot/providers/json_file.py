"""Offline provider reading data/events/*.json. Used for paper trading and tests.

Each file holds a list of objects:
  {"kind": "congress_trade", "symbol": "NVDA", "side": "buy", "actor": "...",
   "event_date": "2026-09-01", "disclosed_date": "2026-09-20", "size_usd": 50000,
   "raw": {...}}
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ctbot.models import AltDataEvent, EventKind, Side


class JsonFileProvider:
    name = "json_file"
    supports = frozenset(EventKind)

    def __init__(self, root: Path = Path("data/events")) -> None:
        self.root = root

    def fetch(self, kinds: set[EventKind], since: date) -> list[AltDataEvent]:
        out: list[AltDataEvent] = []
        for path in sorted(self.root.glob("*.json")):
            for r in json.loads(path.read_text()):
                ev = AltDataEvent(
                    kind=EventKind(r["kind"]),
                    symbol=r["symbol"].upper(),
                    side=Side(r["side"]),
                    actor=r.get("actor", ""),
                    event_date=date.fromisoformat(r["event_date"]),
                    disclosed_date=date.fromisoformat(r.get("disclosed_date", r["event_date"])),
                    source=r.get("source", self.name),
                    size_usd=r.get("size_usd"),
                    raw=r.get("raw", {}),
                )
                if ev.kind in kinds and ev.disclosed_date >= since:
                    out.append(ev)
        return out
