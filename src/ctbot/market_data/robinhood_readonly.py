"""Read-only market data from Robinhood's MCP server (via ReadOnlyRobinhoodClient).

Implements MarketDataProvider plus intraday_bars(). Only read-only tools are called; the
client enforces that allowlist. Gap-fill bars (interpolated=true) are dropped, and during
the regular session quotes whose bid/ask are older than max_quote_age_s are treated as
missing, so stale prices never reach the risk manager.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from ctbot import market_hours
from ctbot.models import Bar, Quote

log = logging.getLogger(__name__)

QUOTES_PER_CALL = 20
HISTORICALS_PER_CALL = 10
DAILY_HISTORY_CALENDAR_DAYS = 400  # ~275 trading days; enough for every price strategy
BACKFILL_CALENDAR_DAYS = 10  # rebuild missing recent daily bars from 5-minute bars this far back

CallMany = Callable[[list[tuple[str, dict[str, Any]]]], list[dict[str, Any]]]


def _dt(s: str) -> datetime:
    # Robinhood sends RFC3339 with up to nanoseconds; Python parses microseconds.
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, _, tz = rest.partition("+")
        s = f"{head}.{frac[:6]}+{tz}" if tz else f"{head}.{frac[:6]}"
    return datetime.fromisoformat(s)


def parse_bars(payload: dict[str, Any]) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    for res in payload["data"]["results"]:
        sym = res["symbol"].upper()
        out[sym] = [
            Bar(
                symbol=sym,
                ts=_dt(b["begins_at"]),
                open=float(b["open_price"]),
                high=float(b["high_price"]),
                low=float(b["low_price"]),
                close=float(b["close_price"]),
                volume=float(b["volume"]),
            )
            for b in res.get("bars", [])
            if not b.get("interpolated")
        ]
    return out


def parse_quotes(
    payload: dict[str, Any], now: datetime, max_quote_age_s: float
) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    in_session = market_hours.session_open(now)
    for res in payload["data"]["results"]:
        q = res.get("quote") or {}
        sym = (q.get("symbol") or "").upper()
        if not sym or q.get("state") != "active" or not q.get("has_traded", True):
            continue
        bid, ask = float(q.get("bid_price") or 0), float(q.get("ask_price") or 0)
        quote_time = _dt(q["venue_bid_time"]) if q.get("venue_bid_time") else None
        if in_session and (quote_time is None or (now - quote_time).total_seconds() > max_quote_age_s):
            log.warning("stale or missing quote for %s; ignoring", sym)
            continue
        # Last price: whichever of regular / non-regular trade is more recent.
        last, last_ts = float(q.get("last_trade_price") or 0), q.get("venue_last_trade_time")
        if q.get("last_non_reg_trade_price") and q.get("venue_last_non_reg_trade_time"):
            if not last_ts or _dt(q["venue_last_non_reg_trade_time"]) > _dt(last_ts):
                last = float(q["last_non_reg_trade_price"])
        out[sym] = Quote(sym, bid, ask, last, ts=quote_time or now)
    return out


class RobinhoodReadOnlyMarketData:
    def __init__(
        self,
        call_many: CallMany,
        max_quote_age_s: float = 120.0,
        quote_ttl_s: float = 5.0,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._call_many = call_many
        self.max_quote_age_s = max_quote_age_s
        self.quote_ttl_s = quote_ttl_s
        self.clock = clock
        self._daily: dict[str, tuple[object, list[Bar]]] = {}  # symbol -> (trading date, bars)
        self._warm: set[str] = set()  # symbols to batch-fetch together on a daily cache miss
        self._quotes: dict[str, tuple[float, Quote]] = {}

    # ---- MarketDataProvider ------------------------------------------------
    def daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        sym = symbol.upper()
        today = market_hours.trading_date(self.clock())
        cached = self._daily.get(sym)
        if cached is None or cached[0] != today:
            stale = [s for s in self._warm | {sym}
                     if s not in self._daily or self._daily[s][0] != today]
            self.prefetch_daily(stale)
            cached = self._daily.get(sym, (today, []))
        return cached[1][-lookback_days:]

    def warm(self, symbols) -> None:
        """Register the universe so a daily-bar cache miss fetches all of it in batches."""
        self._warm |= {s.upper() for s in symbols}

    def prefetch_daily(self, symbols: list[str]) -> None:
        """One call per 10 symbols; cached for the rest of the trading day."""
        now = self.clock()
        start = (now - timedelta(days=DAILY_HISTORY_CALENDAR_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
        syms = sorted({s.upper() for s in symbols})
        calls = [
            ("get_equity_historicals",
             {"symbols": syms[i:i + HISTORICALS_PER_CALL], "start_time": start, "interval": "day"})
            for i in range(0, len(syms), HISTORICALS_PER_CALL)
        ]
        today = market_hours.trading_date(now)
        for payload in self._call_many(calls):
            for sym, bars in parse_bars(payload).items():
                # Today's daily bar is still forming; strategies use completed days only.
                done = [b for b in bars if b.ts.date() < today]
                self._daily[sym] = (today, done)
        self._backfill_recent(syms, now, today)

    def _backfill_recent(self, syms: list[str], now: datetime, today) -> None:
        """Rebuild completed days whose daily bar is missing (or was gap-fill, so dropped).

        Robinhood marks the latest daily bar as interpolated until it is finalized, which can
        be well into the next morning; without this the price strategies run a day behind.
        Each rebuilt bar aggregates that day's regular-session 5-minute bars.
        """
        start = now - timedelta(days=BACKFILL_CALENDAR_DAYS)
        end = datetime.combine(today, datetime.min.time(), tzinfo=market_hours.ET)  # excludes today
        calls = [
            ("get_equity_historicals", {
                "symbols": syms[i:i + HISTORICALS_PER_CALL], "interval": "5minute",
                "start_time": start.strftime("%Y-%m-%dT00:00:00Z"),
                "end_time": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
            for i in range(0, len(syms), HISTORICALS_PER_CALL)
        ]
        for payload in self._call_many(calls):
            for sym, intraday in parse_bars(payload).items():
                cached_day, daily = self._daily.get(sym, (today, []))
                have = {b.ts.date() for b in daily}
                by_day: dict = {}
                for b in intraday:
                    by_day.setdefault(market_hours.trading_date(b.ts), []).append(b)
                added = []
                for d, bs in sorted(by_day.items()):
                    # Only extend the end of the series; earlier gaps are real market holidays.
                    if d in have or d >= today or (daily and d < daily[-1].ts.date()):
                        continue
                    bs.sort(key=lambda b: b.ts)
                    added.append(Bar(sym, datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc),
                                     bs[0].open, max(b.high for b in bs), min(b.low for b in bs),
                                     bs[-1].close, sum(b.volume for b in bs)))
                if added:
                    log.info("%s: rebuilt %d daily bar(s) from 5-minute bars (%s)", sym, len(added),
                             ", ".join(b.ts.date().isoformat() for b in added))
                    self._daily[sym] = (cached_day, daily + added)

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        mono = time.monotonic()
        syms = sorted({s.upper() for s in symbols})
        need = [s for s in syms if s not in self._quotes or mono - self._quotes[s][0] > self.quote_ttl_s]
        if need:
            calls = [
                ("get_equity_quotes", {"symbols": need[i:i + QUOTES_PER_CALL]})
                for i in range(0, len(need), QUOTES_PER_CALL)
            ]
            now = self.clock()
            fresh: dict[str, Quote] = {}
            for payload in self._call_many(calls):
                fresh.update(parse_quotes(payload, now, self.max_quote_age_s))
            for s in need:
                if s in fresh:
                    self._quotes[s] = (mono, fresh[s])
                else:
                    self._quotes.pop(s, None)
        return {s: self._quotes[s][1] for s in syms if s in self._quotes}

    def is_market_open(self) -> bool:
        return market_hours.session_open(self.clock())

    # ---- intraday ----------------------------------------------------------
    def today_bars(self, symbols: list[str], ttl_s: float = 60.0) -> dict[str, list[Bar]]:
        """Today's regular-session 5-minute bars, cached briefly so several strategies share
        one fetch per cycle. Includes the still-forming bar; callers drop it if needed."""
        now = self.clock()
        key = (market_hours.trading_date(now), tuple(sorted({s.upper() for s in symbols})))
        cache = self.__dict__.setdefault("_today_cache", {})  # several symbol sets per cycle
        hit = cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl_s:
            return hit[1]
        start = datetime.combine(key[0], market_hours.OPEN, tzinfo=market_hours.ET)
        bars = self.intraday_bars(list(key[1]), start)
        for k in [k for k in cache if k[0] != key[0]]:
            del cache[k]  # drop earlier days
        cache[key] = (time.monotonic(), bars)
        return bars

    def intraday_bars(
        self, symbols: list[str], start: datetime, end: datetime | None = None,
        interval: str = "5minute",
    ) -> dict[str, list[Bar]]:
        """Regular-session bars, left-edge labeled in UTC. Not cached."""
        syms = sorted({s.upper() for s in symbols})
        args: dict[str, Any] = {"start_time": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "interval": interval}
        if end is not None:
            args["end_time"] = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        calls = [
            ("get_equity_historicals", {**args, "symbols": syms[i:i + HISTORICALS_PER_CALL]})
            for i in range(0, len(syms), HISTORICALS_PER_CALL)
        ]
        out: dict[str, list[Bar]] = {}
        for payload in self._call_many(calls):
            out.update(parse_bars(payload))
        return out
