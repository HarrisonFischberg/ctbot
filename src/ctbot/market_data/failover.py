"""Robinhood market data with backup sources for completed daily bars: Webull first (its daily
bars are split-adjusted), then TradingFlow. Each backup is used only once its keys are set.

Only daily bars fail over. Quotes and today's intraday bars stay Robinhood-only: backup candles can
run minutes behind, and fills, spreads and stops need live prices. So with
Robinhood down the daily models still see their history, but no entry passes the risk checks
without a quote: the bot fails closed.

After a Robinhood failure the wrapper reads from the backups for `retry_primary_s`, then tries
Robinhood again, so an outage costs one failed call per pause instead of one per symbol.

TradingFlow's daily bars are not adjusted for splits. A day whose previous close (preClose)
disagrees with the prior bar by more than `max_gap` marks an unadjusted split or bad data;
everything before it is dropped, so a strategy sees a shorter history, never a fake crash.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any

from ctbot import market_hours
from ctbot.models import Bar
from ctbot.net import SourceUnavailable

log = logging.getLogger(__name__)

TRADINGFLOW_LOOKBACK = 300  # trading days; Robinhood's history is ~275, enough for every model


def parse_daily_candles(symbol: str, payload: dict[str, Any], max_gap: float = 0.25) -> list[Bar]:
    sym = symbol.upper()
    bars: list[Bar] = []
    for b in sorted(payload.get("bars") or [], key=lambda b: str(b.get("date"))):
        try:
            day = date.fromisoformat(b["date"])
            o, h, lo, c = (float(b[k]) for k in ("open", "high", "low", "close"))
            v = float(b.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if min(o, h, lo, c) <= 0:
            continue
        pre = b.get("preClose")
        if bars and pre and abs(float(pre) / bars[-1].close - 1) > max_gap:
            log.info("%s: TradingFlow bars jump on %s (unadjusted split?); dropping earlier bars",
                     sym, day.isoformat())
            bars = []
        bars.append(Bar(sym, datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
                        o, h, lo, c, v))
    return bars


class TradingFlowDailyBars:
    """Completed daily bars from TradingFlow, cached for the trading day. On a cache miss it
    fetches every registered symbol in one MCP session, like RobinhoodReadOnlyMarketData."""

    def __init__(
        self,
        call_many: Callable[[list[tuple[str, dict[str, Any]]]], list[dict[str, Any]]],
        lookback: int = TRADINGFLOW_LOOKBACK,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._call_many = call_many
        self.lookback = lookback
        self.clock = clock
        self._daily: dict[str, tuple[date, list[Bar]]] = {}
        self._warm: set[str] = set()

    def warm(self, symbols) -> None:
        self._warm |= {s.upper() for s in symbols}

    def daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        sym = symbol.upper()
        today = market_hours.trading_date(self.clock())
        cached = self._daily.get(sym)
        if cached is None or cached[0] != today:
            stale = [s for s in self._warm | {sym} if s not in self._daily or self._daily[s][0] != today]
            self.prefetch(stale, today)
            cached = self._daily.get(sym, (today, []))
        return cached[1][-lookback_days:]

    def prefetch(self, symbols: list[str], today: date) -> None:
        syms = sorted({s.upper() for s in symbols})
        payloads = self._call_many(
            [("symbol_daily_candles", {"symbol": s, "lookback": self.lookback}) for s in syms])
        for sym, payload in zip(syms, payloads):
            # Today's bar is still forming; strategies use completed days only.
            self._daily[sym] = (today, [b for b in parse_daily_candles(sym, payload) if b.ts.date() < today])
        log.info("TradingFlow: daily bars for %d symbols (backup source)", len(syms))


class WebullDailyBars:
    """Completed, split-adjusted daily bars from Webull, cached for the trading day. On a cache
    miss it fetches every registered symbol (20 per request), like TradingFlowDailyBars."""

    def __init__(
        self,
        client,
        lookback: int = TRADINGFLOW_LOOKBACK,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._client = client
        self.lookback = lookback
        self.clock = clock
        self._daily: dict[str, tuple[date, list[Bar]]] = {}
        self._warm: set[str] = set()

    def warm(self, symbols) -> None:
        self._warm |= {s.upper() for s in symbols}

    def daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        sym = symbol.upper()
        today = market_hours.trading_date(self.clock())
        cached = self._daily.get(sym)
        if cached is None or cached[0] != today:
            stale = [s for s in self._warm | {sym} if s not in self._daily or self._daily[s][0] != today]
            self.prefetch(stale, today)
            cached = self._daily.get(sym, (today, []))
        return cached[1][-lookback_days:]

    def prefetch(self, symbols: list[str], today: date) -> None:
        if not self._client.configured:
            raise SourceUnavailable("webull: WEBULL_APP_KEY and WEBULL_APP_SECRET are not set")
        syms = sorted({s.upper() for s in symbols})
        got = self._client.bars(syms, "day", self.lookback)
        for sym in syms:
            bars = []
            for b in got.get(sym, []):  # a daily bar's UTC date is its trading date, as with Robinhood
                d = b.ts.astimezone(timezone.utc).date()
                if d < today:  # today's bar is still forming
                    bars.append(Bar(sym, datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc),
                                    b.open, b.high, b.low, b.close, b.volume))
            self._daily[sym] = (today, bars)
        log.info("Webull: daily bars for %d symbols (backup source)", len(syms))


class FirstBackup:
    """Several backup sources in order: the first one that has bars for a symbol wins."""

    def __init__(self, *sources) -> None:
        self.sources = sources

    def warm(self, symbols) -> None:
        for s in self.sources:
            s.warm(symbols)

    def daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        for s in self.sources:
            try:
                bars = s.daily_bars(symbol, lookback_days)
            except SourceUnavailable:
                continue
            if bars:
                return bars
        return []


class DailyBarFailover:
    """Wraps the primary market data; everything except daily bars goes straight to it."""

    def __init__(
        self,
        primary,
        backup,  # TradingFlowDailyBars, WebullDailyBars or a FirstBackup of them
        retry_primary_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._primary = primary
        self._backup = backup
        self.retry_primary_s = retry_primary_s
        self._clock = clock
        self._primary_down_until = 0.0

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._primary, name)  # quotes, today_bars, intraday_bars, ...

    def warm(self, symbols) -> None:
        if hasattr(self._primary, "warm"):
            self._primary.warm(symbols)
        self._backup.warm(symbols)

    def daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        if self._clock() < self._primary_down_until:
            return self._from_backup(symbol, lookback_days,
                                     SourceUnavailable("robinhood daily bars paused after a failure"))
        try:
            bars = self._primary.daily_bars(symbol, lookback_days)
        except Exception as e:  # noqa: BLE001 - any primary failure fails over
            self._primary_down_until = self._clock() + self.retry_primary_s
            log.warning("Robinhood daily bars failed (%s); using the backup source for %.0f min",
                        type(e).__name__, self.retry_primary_s / 60)
            return self._from_backup(symbol, lookback_days, e)
        return bars or self._from_backup(symbol, lookback_days, None)

    def _from_backup(self, symbol: str, lookback_days: int, primary_error: Exception | None) -> list[Bar]:
        try:
            bars = self._backup.daily_bars(symbol, lookback_days)
        except SourceUnavailable:
            bars = []
        if bars or primary_error is None:
            return bars
        raise primary_error  # both sources failed: behave exactly as without a backup
