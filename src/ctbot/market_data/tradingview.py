"""TradingView's public stock scanner: technical ratings, relative volume and earnings dates.

One POST to scanner.tradingview.com covers the whole universe, needs no account or key, and is
cached for `ttl_s`. It is an undocumented public endpoint (the one the open-source
tradingview-screener library and atilaahmettaner/tradingview-mcp use), so it can change or
disappear without notice: every caller treats a miss as "no data", never as an error.

Used for two things:
- the earnings guard: Robinhood's calendar is limited to high-market-cap names, so smaller
  holdings had no report date at all; TradingView's next-report date fills that gap;
- extra context for Claude's review (rating, RSI, ATR, relative volume, gap, sector).

Only ticker symbols are sent. Nothing about the account leaves the computer.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from ctbot.net import HttpSource, SourceUnavailable

log = logging.getLogger(__name__)

SCAN_URL = "https://scanner.tradingview.com/america/scan"
# Primary US listings, in order of preference when a name is listed twice. ETFs on NYSE Arca
# are reported as AMEX.
EXCHANGES = ("NASDAQ", "NYSE", "AMEX", "CBOE")
COLUMNS = (
    "name", "exchange", "close", "Recommend.All", "Recommend.MA", "Recommend.Other", "RSI",
    "ATR", "relative_volume_10d_calc", "premarket_change", "gap", "Perf.W", "Perf.1M",
    "Perf.3M", "price_52_week_high", "sector", "market_cap_basic", "earnings_release_next_date",
)
CHUNK = 200


def rating_label(score: float | None) -> str | None:
    """TradingView's own buckets for its -1..1 technical rating."""
    if score is None:
        return None
    if score >= 0.5:
        return "strong_buy"
    if score >= 0.1:
        return "buy"
    if score > -0.1:
        return "neutral"
    if score > -0.5:
        return "sell"
    return "strong_sell"


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _pct(v: Any) -> float | None:
    """TradingView sends percentages as 1.5 for 1.5%; the rest of CTbot uses fractions."""
    f = _num(v)
    return None if f is None else round(f / 100, 4)


def parse_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Scanner rows -> {symbol: row dict}, keeping the preferred listing of duplicated names."""
    out: dict[str, dict[str, Any]] = {}
    rank: dict[str, int] = {}
    for row in payload.get("data") or []:
        values = row.get("d") or []
        if len(values) != len(COLUMNS):
            continue
        rec = dict(zip(COLUMNS, values))
        sym = str(rec.get("name") or "").upper()
        if not sym:
            continue
        r = EXCHANGES.index(rec["exchange"]) if rec.get("exchange") in EXCHANGES else len(EXCHANGES)
        if sym not in out or r < rank[sym]:
            out[sym], rank[sym] = rec, r
    return out


def technicals(rec: dict[str, Any]) -> dict[str, Any]:
    close, atr, high = _num(rec.get("close")), _num(rec.get("ATR")), _num(rec.get("price_52_week_high"))
    score = _num(rec.get("Recommend.All"))
    out = {
        "source": "tradingview",
        "rating": rating_label(score),
        "rating_score": None if score is None else round(score, 3),
        "moving_average_rating": rating_label(_num(rec.get("Recommend.MA"))),
        "oscillator_rating": rating_label(_num(rec.get("Recommend.Other"))),
        "rsi14": None if _num(rec.get("RSI")) is None else round(_num(rec["RSI"]), 1),
        "atr14_pct_of_price": round(atr / close, 4) if atr and close else None,
        "relative_volume_10d": None if _num(rec.get("relative_volume_10d_calc")) is None
        else round(_num(rec["relative_volume_10d_calc"]), 2),
        "premarket_change": _pct(rec.get("premarket_change")),
        "gap_today": _pct(rec.get("gap")),
        "perf_1w": _pct(rec.get("Perf.W")),
        "perf_1m": _pct(rec.get("Perf.1M")),
        "perf_3m": _pct(rec.get("Perf.3M")),
        "below_52w_high": round(1 - close / high, 4) if close and high else None,
        "sector": rec.get("sector") or None,
        "market_cap_usd": _num(rec.get("market_cap_basic")),
    }
    return {k: v for k, v in out.items() if v is not None}


class TradingViewSnapshot:
    def __init__(
        self,
        source: HttpSource | None = None,
        ttl_s: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source or HttpSource("tradingview")
        self.ttl_s = ttl_s
        self.clock = clock
        self._warm: set[str] = set()
        self._rows: dict[str, dict[str, Any]] = {}
        self._fetched_at: float | None = None
        self._asked: set[str] = set()  # symbols covered by the last fetch, found or not

    def warm(self, symbols: Iterable[str]) -> None:
        """Register symbols so one refresh fetches all of them together."""
        self._warm |= {s.upper() for s in symbols}

    def rows(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        need = {s.upper() for s in symbols}
        fresh = self._fetched_at is not None and self.clock() - self._fetched_at < self.ttl_s
        if not fresh or not need <= self._asked:
            self._refresh(need | self._warm)
        return {s: self._rows[s] for s in need if s in self._rows}

    def _refresh(self, symbols: set[str]) -> None:
        syms = sorted(symbols)
        got: dict[str, dict[str, Any]] = {}
        try:
            for i in range(0, len(syms), CHUNK):
                payload = self.source.post_json(SCAN_URL, {
                    "filter": [
                        {"left": "name", "operation": "in_range", "right": syms[i:i + CHUNK]},
                        {"left": "exchange", "operation": "in_range", "right": list(EXCHANGES)},
                    ],
                    "columns": list(COLUMNS),
                    "range": [0, 2 * CHUNK],
                })
                got.update(parse_rows(payload))
        except SourceUnavailable as e:
            log.info("TradingView data skipped: %s", e)  # keep the last good rows
            return
        self._rows, self._asked, self._fetched_at = got, set(syms), self.clock()

    # ---- lookups ----------------------------------------------------------------
    def technicals(self, symbol: str) -> dict[str, Any] | None:
        rec = self.rows([symbol]).get(symbol.upper())
        return technicals(rec) if rec else None

    def next_earnings(self, symbol: str) -> datetime | None:
        rec = self.rows([symbol]).get(symbol.upper())
        ts = _num(rec.get("earnings_release_next_date")) if rec else None
        return datetime.fromtimestamp(ts, timezone.utc) if ts else None
