"""Options flow from TradingFlow's trade-level data (needs TRADINGFLOW_API_KEY).

Stronger evidence than cboe_options: TradingFlow classifies each print by where it traded
against the quote (at the ask = bought, at the bid = sold) and reports bullish and bearish
premium per contract, so direction comes from the trades, not from one last price vs the mid.
Premium that traded inside the spread counts for neither side.

For each trading day it asks for the top `limit` call contracts and top `limit` put contracts
by premium among the universe symbols, then keeps contracts that expire in
`min_days_to_expiry`..`max_days_to_expiry` days (0DTE is mostly hedging and gamma trading) and
are unusual: day volume at least `min_volume` and at least `min_vol_oi` x open interest. Each kept
contract is one OPTIONS_FLOW event: side BUY when bullish premium exceeds bearish (calls bought,
puts sold), SELL otherwise, size_usd = the net directional premium.

Days are cached in `cache_dir`/YYYY-MM-DD.json. Days missed while the bot was off are
backfilled (up to `backfill_days` weekdays back). Today is re-read at most every `refresh_s`
during the session, and every day is read once more after its close, so the window always ends
with complete days. TradingFlow data runs about 15 minutes behind.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from ctbot import market_hours
from ctbot.models import AltDataEvent, EventKind, Side
from ctbot.net import SourceUnavailable
from ctbot.providers.cboe_options import DEFAULT_EXCLUDE

log = logging.getLogger(__name__)

SOURCE = "tradingflow_options"
FINAL_AFTER = time(16, 20)  # ET: the session's flow is complete (TradingFlow lags ~15 min)


def _f(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(x) else x


def parse_rank(
    payload: dict[str, Any],
    day: date,
    min_volume: float = 500,
    min_vol_oi: float = 1.0,
    min_days_to_expiry: int = 1,
    max_days_to_expiry: int = 60,
) -> list[AltDataEvent]:
    out: list[AltDataEvent] = []
    for r in payload.get("rows") or []:
        try:
            expiry = date.fromisoformat(r["expirationDate"])
            sym = str(r["symbol"]).upper()
        except (KeyError, TypeError, ValueError):
            continue
        if not min_days_to_expiry <= (expiry - day).days <= max_days_to_expiry:
            continue
        volume, oi = _f(r.get("dailyVolume")), _f(r.get("oi"))
        if volume < min_volume or volume < min_vol_oi * oi:
            continue
        bull, bear = _f(r.get("bullishPremium")), _f(r.get("bearishPremium"))
        if bull == bear:
            continue
        is_call = r.get("putCall") == "CALL"
        out.append(AltDataEvent(
            kind=EventKind.OPTIONS_FLOW,
            symbol=sym,
            side=Side.BUY if bull > bear else Side.SELL,
            actor="flow",
            event_date=day,
            disclosed_date=day,
            source=SOURCE,
            size_usd=round(abs(bull - bear), 2),
            raw={
                "contract": r.get("optionSymbol"),
                "option_type": "call" if is_call else "put",
                "expiry": expiry.isoformat(),
                "strike": r.get("strike"),
                "volume": volume,
                "open_interest": oi,
                "vol_oi": round(volume / oi, 2) if oi else None,
                "oi_change": r.get("oiChange"),
                "iv": r.get("iv"),
                "delta": r.get("delta"),
                "bullish_premium": bull,
                "bearish_premium": bear,
                "neutral_premium": _f(r.get("neutralPremium")),
                "side_inferred": "trade-level: at the ask = bought, at the bid = sold",
            },
        ))
    return out


def _to_json(ev: AltDataEvent) -> dict[str, Any]:
    return {"symbol": ev.symbol, "side": ev.side.value, "date": ev.event_date.isoformat(),
            "size_usd": ev.size_usd, "raw": ev.raw}


def _from_json(d: dict[str, Any]) -> AltDataEvent:
    day = date.fromisoformat(d["date"])
    return AltDataEvent(EventKind.OPTIONS_FLOW, d["symbol"], Side(d["side"]), "flow", day, day,
                        SOURCE, d.get("size_usd"), d.get("raw") or {})


class TradingFlowOptionsFlowProvider:
    name = SOURCE
    supports = frozenset({EventKind.OPTIONS_FLOW})

    def __init__(
        self,
        symbols: list[str],
        call_many: Callable[[list[tuple[str, dict[str, Any]]]], list[dict[str, Any]]],
        cache_dir: Path = Path("data/options_flow_tradingflow"),
        exclude: frozenset[str] = DEFAULT_EXCLUDE,
        limit: int = 100,
        backfill_days: int = 5,
        refresh_s: float = 1800.0,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        **thresholds: float,
    ) -> None:
        self.symbols = sorted({s.upper() for s in symbols} - set(exclude))
        self._call_many = call_many
        self.cache_dir = cache_dir
        self.limit = limit
        self.backfill_days = backfill_days
        self.refresh_s = refresh_s
        self.clock = clock
        self.thresholds = thresholds
        self._read_at: dict[date, datetime] = {}  # when an unfinished day was last read

    def fetch(self, kinds: set[EventKind], since: date) -> list[AltDataEvent]:
        if EventKind.OPTIONS_FLOW not in kinds:
            return []
        try:
            self._refresh(self.clock())
        except SourceUnavailable as e:
            log.warning("options flow (TradingFlow): %s; using cached days", e)
        out: list[AltDataEvent] = []
        for path in sorted(self.cache_dir.glob("*.json")):
            try:
                if date.fromisoformat(path.stem) < since:
                    continue
                out.extend(_from_json(d) for d in json.loads(path.read_text())["events"])
            except (ValueError, KeyError, TypeError, OSError):
                log.warning("ignoring unreadable options-flow cache %s", path.name)
        return out

    def _days_to_read(self, now: datetime) -> list[date]:
        et = now.astimezone(market_hours.ET)
        today = et.date()
        days, d = [], today
        while len(days) < self.backfill_days:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        due = []
        for d in reversed(days):
            if d == today and et.time() < market_hours.OPEN:
                continue  # no session yet
            path = self.cache_dir / f"{d.isoformat()}.json"
            try:
                final = json.loads(path.read_text()).get("final", False)
            except (FileNotFoundError, ValueError, OSError):
                final = False
            if final:
                continue
            last = self._read_at.get(d)
            if d == today and last is not None and (now - last).total_seconds() < self.refresh_s \
                    and et.time() < FINAL_AFTER:
                continue
            due.append(d)
        return due

    def _refresh(self, now: datetime) -> None:
        if not self.symbols:
            return
        due = self._days_to_read(now)
        if not due:
            return
        calls = [
            ("contract_rank_snapshot", {"rankMode": "premium", "sortOrder": "descend", "putCall": pc,
                                        "symbols": self.symbols, "limit": self.limit,
                                        "tradingDate": d.isoformat()})
            for d in due for pc in ("CALL", "PUT")
        ]
        payloads = self._call_many(calls)
        et = now.astimezone(market_hours.ET)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for i, d in enumerate(due):
            pair = payloads[2 * i: 2 * i + 2]
            self._read_at[d] = now
            if any(p.get("effectiveDate") != d.isoformat() for p in pair):
                # No session that day (holiday) or not loaded yet: TradingFlow answered with
                # another day. A past day is settled as empty; today is asked again later.
                if d < et.date():
                    self._write(d, [], final=True)
                continue
            events = [e for p in pair for e in parse_rank(p, d, **self.thresholds)]
            final = d < et.date() or et.time() >= FINAL_AFTER
            self._write(d, events, final)
            log.info("options flow (TradingFlow) %s: %d contracts across %d symbols%s", d.isoformat(),
                     len(events), len({e.symbol for e in events}), "" if final else " so far")

    def _write(self, d: date, events: list[AltDataEvent], final: bool) -> None:
        path = self.cache_dir / f"{d.isoformat()}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"final": final, "events": [_to_json(e) for e in events]}))
        tmp.replace(path)
