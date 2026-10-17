"""Dashboard prices that stop at the close.

While the market is open, prices come from the live source and are saved to disk. While it's
closed nothing is polled: the saved prices are shown, with their time. The one exception is a
single catch-up fetch per closed period for symbols last priced well before the close (the
dashboard wasn't running then), so an evening doesn't show mid-day prices.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ctbot import market_hours
from ctbot.models import Quote

log = logging.getLogger(__name__)

NEAR_CLOSE = timedelta(minutes=10)  # a price saved this close to the bell counts as a closing price


class SessionQuotes:
    def __init__(
        self,
        fetch: Callable[[list[str]], dict[str, Quote]],
        path: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._fetch, self._path, self._clock = fetch, path, clock
        self._saved: dict[str, dict[str, Any]] = self._load()  # symbol -> quote fields + saved_at
        self._caught_up: datetime | None = None  # the close a catch-up fetch already ran for
        self._lock = threading.Lock()

    def __call__(self, symbols: list[str]) -> dict[str, Quote]:
        now = self._clock()
        with self._lock:
            if market_hours.session_open(now):
                return self._fetch_and_save(symbols, now)
            close = market_hours.last_close(now)
            stale = [s for s in symbols if s not in self._saved
                     or datetime.fromisoformat(self._saved[s]["saved_at"]) < close - NEAR_CLOSE]
            if stale and self._caught_up != close:
                self._caught_up = close  # one attempt per closed period, even if it fails
                try:
                    self._fetch_and_save(stale, now)
                except Exception as e:  # noqa: BLE001 - show what was saved instead
                    log.warning("after-close price fetch failed: %s", e)
            return {s: self._quote(s) for s in symbols if s in self._saved}

    def _fetch_and_save(self, symbols: list[str], now: datetime) -> dict[str, Quote]:
        fresh = self._fetch(symbols)
        for s, q in fresh.items():
            self._saved[s] = {"bid": q.bid, "ask": q.ask, "last": q.last, "ts": q.ts.isoformat(),
                              "saved_at": now.isoformat()}
        if fresh:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._saved))
            tmp.replace(self._path)
        return fresh

    def _quote(self, s: str) -> Quote:
        d = self._saved[s]
        return Quote(s, d["bid"], d["ask"], d["last"], datetime.fromisoformat(d["ts"]))

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
