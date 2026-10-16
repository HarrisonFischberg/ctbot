"""Candles for the dashboard's stock chart: Robinhood first, then Webull, then the local bar cache.

Spans: 1d = the latest session in 5-minute candles, 1w = the last five sessions in hourly
candles, 3m = daily candles, 1y = weekly candles built from daily bars. All sources are
read-only. Results are cached per (symbol, span) for a minute (daily spans: five minutes), so a
chart left open never gets near Webull's limit of 60 bar requests a minute.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import Any

from ctbot import market_hours
from ctbot.models import Bar
from ctbot.research import bars as bar_cache

log = logging.getLogger(__name__)

# span -> (source interval, sessions kept for intraday spans / daily bars fetched)
SPANS: dict[str, tuple[str, int]] = {"1d": ("5minute", 1), "1w": ("hour", 5), "3m": ("day", 63), "1y": ("day", 260)}
CALENDAR_DAYS = {"1d": 6, "1w": 12, "3m": 100, "1y": 380}  # Robinhood start_time look-back
WEBULL_COUNT = {"1d": 200, "1w": 60, "3m": 63, "1y": 260}


def _sessions(bars: list[Bar], n: int) -> list[Bar]:
    """Keep the last n New York trading dates."""
    days = sorted({market_hours.trading_date(b.ts) for b in bars})[-n:]
    keep = set(days)
    return [b for b in bars if market_hours.trading_date(b.ts) in keep]


def _bucket(bars: list[Bar], key: Callable[[Bar], Any]) -> list[Bar]:
    """Merge consecutive bars that share key(bar) into one candle."""
    out: list[Bar] = []
    last = None
    for b in bars:
        k = key(b)
        if out and k == last:
            p = out[-1]
            out[-1] = Bar(p.symbol, p.ts, p.open, max(p.high, b.high), min(p.low, b.low), b.close, p.volume + b.volume)
        else:
            out.append(b)
        last = k
    return out


def _hourly(bars: list[Bar]) -> list[Bar]:  # buckets start at 9:30, 10:30, ... New York time
    def key(b: Bar):
        et = b.ts.astimezone(market_hours.ET)
        return et.date(), (et.hour * 60 + et.minute - 30) // 60
    return _bucket(bars, key)


def _weekly(bars: list[Bar]) -> list[Bar]:
    return _bucket(bars, lambda b: b.ts.date().isocalendar()[:2])


def _noon(b: Bar) -> Bar:
    """A daily bar's UTC date is its trading date (Robinhood stamps midnight UTC). Re-stamp it at
    noon UTC so the browser shows the same date in New York time."""
    d = b.ts.astimezone(timezone.utc).date()
    return Bar(b.symbol, datetime.combine(d, dtime(12), tzinfo=timezone.utc), b.open, b.high, b.low, b.close, b.volume)


def _shape(bars: list[Bar], span: str) -> list[Bar]:
    bars = sorted(bars, key=lambda b: b.ts)
    interval, n = SPANS[span]
    if interval != "day":  # every source already sends regular-session bars only
        return _sessions(bars, n)
    bars = [_noon(b) for b in bars][-n:]
    return _weekly(bars) if span == "1y" else bars


class CandleSource:
    def __init__(
        self,
        robinhood=None,                     # RobinhoodReadOnlyMarketData, or None
        webull=None,                        # WebullClient, or None
        cache_root: Path = bar_cache.CACHE_DIR,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._rh, self._wb, self._root, self._clock = robinhood, webull, cache_root, clock
        self._memo: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def _robinhood(self, sym: str, span: str) -> list[Bar]:
        interval = SPANS[span][0]
        now = self._clock()
        bars = self._rh.intraday_bars([sym], now - timedelta(days=CALENDAR_DAYS[span]), interval=interval).get(sym, [])
        today = market_hours.trading_date(now)
        if interval == "day" and (not bars or bars[-1].ts.astimezone(timezone.utc).date() < today):
            # Robinhood withholds today's daily bar until it is final: build it from today's 5-minute bars
            five = self._rh.today_bars([sym]).get(sym, [])
            if five:
                bars.append(Bar(sym, datetime.combine(today, dtime(0), tzinfo=timezone.utc), five[0].open,
                                max(b.high for b in five), min(b.low for b in five), five[-1].close,
                                sum(b.volume for b in five)))
        return bars

    def _webull(self, sym: str, span: str) -> list[Bar]:
        return self._wb.bars([sym], SPANS[span][0], WEBULL_COUNT[span]).get(sym, [])

    def _cache(self, sym: str, span: str) -> list[Bar]:
        interval = SPANS[span][0]
        if interval == "hour":
            return _hourly(bar_cache.load("5minute", sym, self._root))
        return bar_cache.load(interval, sym, self._root)

    def get(self, symbol: str, span: str) -> dict[str, Any]:
        sym, span = symbol.upper(), span if span in SPANS else "1d"
        with self._lock:
            hit = self._memo.get((sym, span))
            ttl = 300.0 if SPANS[span][0] == "day" else 60.0
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            sources = []
            if self._rh is not None:
                sources.append(("Robinhood", self._robinhood))
            if self._wb is not None and self._wb.configured:
                sources.append(("Webull", self._webull))
            sources.append(("saved bars", self._cache))
            notes, bars, used = [], [], None
            for name, fetch in sources:
                try:
                    bars = _shape(fetch(sym, span), span)
                except Exception as e:  # noqa: BLE001 - try the next source
                    notes.append(f"{name}: {str(e)[:120] or type(e).__name__}")
                    continue
                if bars:
                    used = name
                    break
                notes.append(f"{name}: no bars")
            out = {
                "symbol": sym, "span": span, "source": used, "notes": [] if used else notes,
                "interval": {"1d": "5 min", "1w": "1 hour", "3m": "1 day", "1y": "1 week"}[span],
                "bars": [{"t": int(b.ts.timestamp() * 1000), "o": b.open, "h": b.high, "l": b.low,
                          "c": b.close, "v": b.volume} for b in bars],
            }
            if used:  # a failure is retried on the next request; only results are cached
                self._memo[(sym, span)] = (time.monotonic(), out)
            return out
