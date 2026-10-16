"""Offline provider reading <BARS_DIR>/<SYMBOL>.csv (columns: date,open,high,low,close,volume).

Used for paper trading and backtests without any network or broker access.
BARS_DIR defaults to data/bars.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from ctbot.models import Bar, Quote

log = logging.getLogger(__name__)


class CsvMarketData:
    def __init__(self, root: Path = Path("data/bars")) -> None:
        self.root = root
        self._warned: set[str] = set()
        if not root.is_dir():
            log.warning(
                "bars directory %s does not exist: price strategies and quotes will be empty",
                root,
            )

    def daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        path = self.root / f"{symbol.upper()}.csv"
        if not path.exists():
            if symbol.upper() not in self._warned and self.root.is_dir():
                self._warned.add(symbol.upper())
                log.warning("no bars for %s at %s", symbol.upper(), path)
            return []
        with path.open() as fh:
            rows = list(csv.DictReader(fh))
        bars = [
            Bar(
                symbol=symbol.upper(),
                ts=datetime.fromisoformat(r["date"]).replace(tzinfo=timezone.utc),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
            )
            for r in rows
        ]
        bars.sort(key=lambda b: b.ts)
        return bars[-lookback_days:]

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for s in symbols:
            bars = self.daily_bars(s, 1)
            if bars:
                px = bars[-1].close
                out[s.upper()] = Quote(symbol=s.upper(), bid=px, ask=px, last=px, ts=bars[-1].ts)
        return out

    def is_market_open(self) -> bool:
        return True
