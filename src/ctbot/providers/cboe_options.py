"""Unusual options activity from Cboe's free delayed option chains (no key, 15-minute delay).

Once per trading day, after `refresh_after` ET, it downloads each symbol's full chain from
cdn-api.cboe.com (about 1-6 MB per symbol, so index ETFs are skipped by default) and keeps
contracts whose day volume is unusual: at least `min_volume` contracts and at least
`min_vol_oi` times the open interest, expiring in `min_days_to_expiry`..`max_days_to_expiry`
days. Same-day expiries are dropped: 0DTE volume is mostly hedging and gamma trading.

The chain has no trade-side data, so direction is inferred: a contract counts only when its
last trade printed at or above the mid (likely a buyer). Calls bought -> side BUY (bullish),
puts bought -> side SELL (bearish), premium = volume x mid x 100. Each kept contract is one
OPTIONS_FLOW event for strategies.options_flow. This is an approximation of paid
sweep/block feeds, not a replacement: treat its signals as weak evidence.

Each day's events are saved to `cache_dir`/YYYY-MM-DD.json so a restart keeps the window.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from ctbot import market_hours
from ctbot.models import AltDataEvent, EventKind, Side
from ctbot.net import HttpSource, SourceUnavailable

log = logging.getLogger(__name__)

CHAIN_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
OCC = re.compile(r"^(?P<root>.+?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
# Index and sector ETF flow is dominated by hedging; their chains are also the largest.
DEFAULT_EXCLUDE = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB",
    "XLC", "SMH", "GLD", "TLT",
})


def _f(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(x) else x


def parse_chain(
    symbol: str,
    payload: dict[str, Any],
    today: date,
    min_volume: float = 500,
    min_vol_oi: float = 1.0,
    min_days_to_expiry: int = 1,
    max_days_to_expiry: int = 60,
) -> list[AltDataEvent]:
    sym = symbol.upper()
    root = sym.replace(".", "")
    out: list[AltDataEvent] = []
    for o in (payload.get("data") or {}).get("options") or []:
        m = OCC.match(str(o.get("option", "")))
        if not m or m["root"] != root:
            continue  # adjusted / non-standard deliverable contracts
        ymd = m["ymd"]
        expiry = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
        dte = (expiry - today).days
        if not min_days_to_expiry <= dte <= max_days_to_expiry:
            continue
        volume, oi = _f(o.get("volume")), _f(o.get("open_interest"))
        if volume < min_volume or volume < min_vol_oi * oi:
            continue
        bid, ask, last = _f(o.get("bid")), _f(o.get("ask")), _f(o.get("last_trade_price"))
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2
        if last < mid:
            continue  # likely sold to the bid: direction unknown, skip
        is_call = m["cp"] == "C"
        out.append(AltDataEvent(
            kind=EventKind.OPTIONS_FLOW,
            symbol=sym,
            side=Side.BUY if is_call else Side.SELL,
            actor="flow",
            event_date=today,
            disclosed_date=today,
            source="cboe_options",
            size_usd=round(volume * mid * 100, 2),
            raw={
                "contract": o["option"],
                "option_type": "call" if is_call else "put",
                "expiry": expiry.isoformat(),
                "strike": int(m["strike"]) / 1000,
                "volume": volume,
                "open_interest": oi,
                "vol_oi": round(volume / oi, 2) if oi else None,
                "iv": o.get("iv"),
                "delta": o.get("delta"),
                "side_inferred": "last trade at or above mid",
            },
        ))
    return out


def _to_json(ev: AltDataEvent) -> dict[str, Any]:
    return {"symbol": ev.symbol, "side": ev.side.value, "date": ev.event_date.isoformat(),
            "size_usd": ev.size_usd, "raw": ev.raw}


def _from_json(d: dict[str, Any]) -> AltDataEvent:
    day = date.fromisoformat(d["date"])
    return AltDataEvent(EventKind.OPTIONS_FLOW, d["symbol"], Side(d["side"]), "flow", day, day,
                        "cboe_options", d.get("size_usd"), d.get("raw") or {})


class CboeOptionsFlowProvider:
    name = "cboe_options"
    supports = frozenset({EventKind.OPTIONS_FLOW})

    def __init__(
        self,
        symbols: list[str],
        cache_dir: Path = Path("data/options_flow"),
        exclude: frozenset[str] = DEFAULT_EXCLUDE,
        refresh_after: time = time(14, 30),
        workers: int = 6,
        source: HttpSource | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        **thresholds: float,
    ) -> None:
        self.symbols = sorted({s.upper() for s in symbols} - set(exclude))
        self.cache_dir = cache_dir
        self.refresh_after = refresh_after
        self.workers = workers
        # More failures than one bad symbol before pausing: a chain fetch is per symbol.
        self.source = source or HttpSource("cboe", timeout_s=30.0, failure_threshold=8)
        self.clock = clock
        self.thresholds = thresholds

    def fetch(self, kinds: set[EventKind], since: date) -> list[AltDataEvent]:
        if EventKind.OPTIONS_FLOW not in kinds:
            return []
        self._maybe_refresh(self.clock())
        out: list[AltDataEvent] = []
        for path in sorted(self.cache_dir.glob("*.json")):
            try:
                if date.fromisoformat(path.stem) < since:
                    continue
                out.extend(_from_json(d) for d in json.loads(path.read_text()))
            except (ValueError, KeyError, OSError):
                log.warning("ignoring unreadable options-flow cache %s", path.name)
        return out

    def _maybe_refresh(self, now: datetime) -> None:
        et = now.astimezone(market_hours.ET)
        today = et.date()
        path = self.cache_dir / f"{today.isoformat()}.json"
        if et.weekday() >= 5 or et.time() < self.refresh_after or path.exists():
            return
        events, failed = self._download(today)
        if failed == len(self.symbols):
            return  # nothing fetched (Cboe down or blocked): try again next cycle
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([_to_json(e) for e in events]))
        log.info("options flow: %d unusual contracts across %d symbols (%d chains failed)",
                 len(events), len({e.symbol for e in events}), failed)

    def _download(self, today: date) -> tuple[list[AltDataEvent], int]:
        def one(sym: str) -> list[AltDataEvent] | None:
            try:
                payload = self.source.get_json(CHAIN_URL.format(symbol=sym))
            except SourceUnavailable:
                return None
            return parse_chain(sym, payload, today, **self.thresholds)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(one, self.symbols))
        events = [e for r in results if r for e in r]
        return events, sum(r is None for r in results)
