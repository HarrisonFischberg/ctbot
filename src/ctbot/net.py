"""Guarded HTTP for the free public data sources (TradingView scanner, Cboe, Marketaux).

Each source gets one HttpSource: a short timeout, one retry with backoff on 429/5xx/timeouts,
and a circuit breaker that stops calling a source for `cooldown_s` after `failure_threshold`
failures in a row, so a dead or rate-limiting service costs one log line, not a slow cycle.

Errors are reported as SourceUnavailable carrying only the source name and a short reason.
Request URLs are never logged or put in exception messages: Marketaux takes its key in the
query string, and httpx's own error text includes the full URL.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "ctbot/0.1 (+https://github.com/HarrisonFischberg/ctbot)"
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class _RedactKeys(logging.Filter):
    """Defense in depth: httpx logs request URLs at INFO, and Marketaux's key is a query
    parameter. Strip key-like query values from httpx's records, whatever the log level."""

    PATTERN = re.compile(r"((?:api_)?(?:token|key)|apikey)=[^&\s\"']+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = self.PATTERN.sub(r"\1=REDACTED", msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True


logging.getLogger("httpx").addFilter(_RedactKeys())


class SourceUnavailable(RuntimeError):
    """A public data source failed or is cooling down. Callers degrade; they never crash."""


class HttpSource:
    def __init__(
        self,
        name: str,
        timeout_s: float = 10.0,
        failure_threshold: int = 3,
        cooldown_s: float = 600.0,
        retries: int = 1,
        backoff_s: float = 1.0,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.retries = retries
        self.backoff_s = backoff_s
        self._client = client or httpx.Client(
            timeout=timeout_s, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        self._clock = clock
        self._sleep = sleep
        self._failures = 0
        self._open_until = 0.0

    @property
    def available(self) -> bool:
        return self._clock() >= self._open_until

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", url, params=params)

    def post_json(self, url: str, body: Any) -> Any:
        return self._request("POST", url, json=body)

    def _request(self, method: str, url: str, **kw: Any) -> Any:
        if not self.available:
            raise SourceUnavailable(f"{self.name}: cooling down after repeated failures")
        reason = "unknown error"
        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(self.backoff_s * attempt)
            try:
                r = self._client.request(method, url, **kw)
            except httpx.TimeoutException:
                reason = "timeout"
                continue
            except httpx.HTTPError as e:
                reason = type(e).__name__  # never str(e): it contains the URL
                continue
            if r.status_code in RETRY_STATUS:
                reason = f"HTTP {r.status_code}"
                continue
            if r.status_code >= 400:
                reason = f"HTTP {r.status_code}"
                break
            try:
                data = r.json()
            except ValueError:
                reason = "response was not JSON"
                break
            self._failures = 0
            return data
        self._fail(reason)
        raise SourceUnavailable(f"{self.name}: {reason}")

    def _fail(self, reason: str) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._open_until = self._clock() + self.cooldown_s
            self._failures = 0
            log.warning("%s unavailable (%s); pausing it for %.0f min",
                        self.name, reason, self.cooldown_s / 60)
