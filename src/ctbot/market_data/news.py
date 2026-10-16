"""Per-ticker news sentiment from Marketaux (optional; free key, 100 requests a day).

Only numbers reach Claude: how many recent articles mention the ticker and their mean
entity sentiment (-1..1). Headlines and article text are never passed on, so third-party
text cannot steer the review. Results are cached for `ttl_s` and capped at
`max_requests_per_day`, so reviews (at most MAX_CLAUDE_REVIEWS_PER_DAY) stay inside the
free tier. Marketaux takes its key in the query string; HttpSource never logs URLs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ctbot.net import HttpSource, SourceUnavailable

log = logging.getLogger(__name__)

NEWS_URL = "https://api.marketaux.com/v1/news/all"


def summarize(symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
    sym = symbol.upper()
    scores = [
        float(e["sentiment_score"])
        for a in payload.get("data") or []
        for e in a.get("entities") or []
        if str(e.get("symbol", "")).upper() == sym and isinstance(e.get("sentiment_score"), (int, float))
    ]
    found = (payload.get("meta") or {}).get("found")
    return {
        "source": "marketaux",
        "articles_found": found if isinstance(found, int) else len(payload.get("data") or []),
        "sampled": len(scores),
        "mean_sentiment": round(sum(scores) / len(scores), 3) if scores else None,
    }


class MarketauxSentiment:
    def __init__(
        self,
        token: str,
        source: HttpSource | None = None,
        lookback_days: int = 3,
        ttl_s: float = 4 * 3600,
        max_requests_per_day: int = 90,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token
        self.source = source or HttpSource("marketaux")
        self.lookback_days = lookback_days
        self.ttl_s = ttl_s
        self.max_requests_per_day = max_requests_per_day
        self.clock = clock
        self.mono = mono
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._used: tuple[date | None, int] = (None, 0)

    def for_symbol(self, symbol: str) -> dict[str, Any] | None:
        sym = symbol.upper()
        hit = self._cache.get(sym)
        if hit and self.mono() - hit[0] < self.ttl_s:
            return hit[1]
        now = self.clock()
        day, used = self._used
        if day != now.date():
            day, used = now.date(), 0
        if used >= self.max_requests_per_day:
            return hit[1] if hit else None
        self._used = (day, used + 1)
        since = (now - timedelta(days=self.lookback_days)).strftime("%Y-%m-%dT%H:%M")
        try:
            payload = self.source.get_json(NEWS_URL, params={
                "symbols": sym, "filter_entities": "true", "language": "en",
                "published_after": since, "api_token": self._token,
            })
        except SourceUnavailable as e:
            log.info("news sentiment skipped for %s: %s", sym, e)
            return hit[1] if hit else None
        out = summarize(sym, payload)
        self._cache[sym] = (self.mono(), out)
        return out
