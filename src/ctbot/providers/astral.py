"""Form 4 insider trades from Hey Astral (Fiscal data) as INSIDER_TRADE events.

Feeds the `institutional` strategy, whose Quiver source is still a stub. Only open-market
purchases and sales count: grants, option exercises and tax withholding are not trading
decisions. One event per transaction: actor = the insider, event date = the trade date,
disclosed = the filing date, size = the dollar value.

13F holdings are left out on purpose: a megacap has thousands of small 13F filers a quarter,
which would trip the "2+ distinct actors" rule on every large stock.

At most once every `refresh_hours` the universe is fetched in batches inside one MCP session;
after a failure the next try waits `retry_minutes`, and the cached events are served meanwhile.
Events are kept in `cache_dir`/insiders.json, so a restart costs no calls.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ctbot.models import AltDataEvent, EventKind, Side

log = logging.getLogger(__name__)

TOOL = "astral_research_fiscal_observations"
SIDES = {"open_market_purchase": Side.BUY, "open_market_sale": Side.SELL}
MAX_ROWS = 10000  # the tool's per-call cap

CallMany = Callable[[Iterable[tuple[str, dict[str, Any]]]], list[dict[str, Any]]]


def to_event(row: dict[str, Any]) -> AltDataEvent | None:
    """One observation row (feature transaction_value) -> event, or None if it isn't usable."""
    side = SIDES.get(row.get("event_type") or "")
    p = row.get("payload") or {}
    try:
        traded = date.fromisoformat(p["transaction_date"])
        filed = date.fromisoformat(p.get("filing_date") or row["available_at"][:10])
        value = abs(float(p.get("transaction_value") or row.get("value_numeric") or 0.0))
    except (KeyError, TypeError, ValueError):
        return None
    if side is None or not p.get("holder_name") or not row.get("symbol"):
        return None
    return AltDataEvent(
        kind=EventKind.INSIDER_TRADE, symbol=str(row["symbol"]).upper(), side=side,
        actor=str(p["holder_name"]), event_date=traded, disclosed_date=filed, source="astral",
        size_usd=value or None,
        raw={"title": p.get("title"), "shares": p.get("shares"), "price_low": p.get("price_low"),
             "price_high": p.get("price_high"), "url": row.get("source_url")},
    )


def _to_json(ev: AltDataEvent) -> dict[str, Any]:
    return {"symbol": ev.symbol, "side": ev.side.value, "actor": ev.actor,
            "event_date": ev.event_date.isoformat(), "disclosed_date": ev.disclosed_date.isoformat(),
            "size_usd": ev.size_usd, "raw": ev.raw}


def _from_json(d: dict[str, Any]) -> AltDataEvent:
    return AltDataEvent(kind=EventKind.INSIDER_TRADE, symbol=d["symbol"], side=Side(d["side"]),
                        actor=d["actor"], event_date=date.fromisoformat(d["event_date"]),
                        disclosed_date=date.fromisoformat(d["disclosed_date"]), source="astral",
                        size_usd=d.get("size_usd"), raw=d.get("raw") or {})


class AstralInsiderProvider:
    name = "astral"
    supports = frozenset({EventKind.INSIDER_TRADE})

    def __init__(self, symbols: Iterable[str], call_many: CallMany, cache_dir: Path,
                 lookback_days: int = 60, refresh_hours: float = 6.0, retry_minutes: float = 30.0,
                 batch: int = 25, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.symbols = sorted({s.upper() for s in symbols})
        self.call_many, self.cache_dir = call_many, cache_dir
        self.lookback_days, self.refresh_hours, self.retry_minutes = lookback_days, refresh_hours, retry_minutes
        self.batch, self.clock = batch, clock
        self._next: datetime | None = None

    @property
    def _path(self) -> Path:
        return self.cache_dir / "insiders.json"

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, events: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(events, indent=1))
        tmp.replace(self._path)

    def refresh(self) -> dict[str, Any]:
        events = self._load()
        now = self.clock()
        if self._next and now < self._next:
            return events
        since = (now - timedelta(days=self.lookback_days)).date()
        calls = [(TOOL, {"symbols": self.symbols[i:i + self.batch], "families": ["insider_transactions"],
                         "feature_ids": ["transaction_value"], "event_types": sorted(SIDES),
                         "available_from": f"{since.isoformat()}T00:00:00Z", "limit": MAX_ROWS,
                         "delivery": "inline"})
                 for i in range(0, len(self.symbols), self.batch)]
        try:
            results = self.call_many(calls)
        except Exception as e:  # noqa: BLE001 - an outage serves the cache, never breaks a cycle
            log.info("astral insiders unavailable (%s); retrying in %.0f min", type(e).__name__, self.retry_minutes)
            self._next = now + timedelta(minutes=self.retry_minutes)
            return events
        self._next = now + timedelta(hours=self.refresh_hours)
        for res in results:
            rows = res.get("rows") or []
            if len(rows) >= MAX_ROWS:
                log.warning("astral insiders: a batch hit %d rows; lower `batch`", MAX_ROWS)
            for row in rows:
                ev = to_event(row)
                if ev is not None and row.get("observation_id"):
                    events[row["observation_id"]] = _to_json(ev)
        events = {k: v for k, v in events.items() if v["disclosed_date"] >= since.isoformat()}
        self._save(events)
        return events

    def fetch(self, kinds: set[EventKind], since: date) -> list[AltDataEvent]:
        if EventKind.INSIDER_TRADE not in kinds:
            return []
        return [_from_json(d) for d in self.refresh().values() if d["disclosed_date"] >= since.isoformat()]
