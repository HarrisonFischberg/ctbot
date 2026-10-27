"""Recent company-news catalysts from Hey Astral, as counts for Claude's review.

Astral tags each article with an event type (earnings, partnership, financing, product_launch,
legal, ...) and an importance score. Only those labels and counts reach Claude: titles and
summaries are dropped, like Marketaux in news.py, so third-party text cannot steer a review.
A stock with fresh, important news is "in play" for the day; one with none usually isn't.

Results are cached for `ttl_s` per symbol. After a failed call Astral is skipped for
`cooldown_s`, so an outage costs one log line, not a slow review.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from ctbot.providers.astral import TOOL, CallMany

log = logging.getLogger(__name__)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    imp = [float(r["value_numeric"]) for r in rows if isinstance(r.get("value_numeric"), (int, float))]
    dates = sorted(str(r.get("published_at") or r.get("available_at") or "")[:10] for r in rows)
    return {
        "source": "astral",
        "articles": len(rows),
        "by_type": dict(Counter(str(r.get("event_type") or "other") for r in rows).most_common()),
        "max_importance": max(imp) if imp else None,
        "latest": dates[-1] if dates and dates[-1] else None,
    }


class AstralNews:
    def __init__(self, call_many: CallMany, lookback_days: int = 2, ttl_s: float = 3600.0,
                 cooldown_s: float = 1800.0, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.call_many = call_many
        self.lookback_days, self.ttl_s, self.cooldown_s = lookback_days, ttl_s, cooldown_s
        self.clock, self.mono = clock, mono
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._down_until = 0.0

    def for_symbol(self, symbol: str) -> dict[str, Any] | None:
        sym = symbol.upper()
        hit = self._cache.get(sym)
        if hit and self.mono() - hit[0] < self.ttl_s:
            return hit[1]
        if self.mono() < self._down_until:
            return hit[1] if hit else None
        since = (self.clock() - timedelta(days=self.lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            res = self.call_many([(TOOL, {"symbols": [sym], "families": ["company_news"],
                                          "feature_ids": ["importance"], "available_from": since,
                                          "limit": 200, "delivery": "inline"})])[0]
        except Exception as e:  # noqa: BLE001 - review context is optional
            log.info("astral news skipped for %s (%s); pausing %.0f min", sym, type(e).__name__, self.cooldown_s / 60)
            self._down_until = self.mono() + self.cooldown_s
            return hit[1] if hit else None
        out = summarize(res.get("rows") or [])
        self._cache[sym] = (self.mono(), out)
        return out
