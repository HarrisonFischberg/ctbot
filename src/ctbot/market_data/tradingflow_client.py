"""Read-only client for TradingFlow's MCP server (https://app.tradingflow.com/api/mcp).

TradingFlow serves OHLCV candles and trade-level options flow. CTbot uses it as a second,
independent source: a backup for Robinhood's daily bars (market_data/failover.py) and an
options-flow feed (providers/tradingflow_options.py). The server has no order tools; the
allowlist below still refuses anything else, so a tool added server-side is never reached.

Auth is an API key, TRADINGFLOW_API_KEY (from .env or the dashboard's Settings tab), sent as the
x-api-key header and read at call time, so a key saved on the dashboard applies without a
restart. The key is never logged or put in an exception message.

Every failure surfaces as net.SourceUnavailable. After `failure_threshold` failed calls in a
row the client pauses for `cooldown_s`, so an outage costs one log line, not a slow cycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Iterable
from typing import Any

from ctbot.market_data.robinhood_mcp_client import ToolNotAllowed, parse_tool_result
from ctbot.net import SourceUnavailable

log = logging.getLogger(__name__)

SERVER_URL = "https://app.tradingflow.com/api/mcp"
KEY_ENV = "TRADINGFLOW_API_KEY"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

# Only the tools CTbot uses. Raw SQL (execute_tf_query) is deliberately left out.
READ_ONLY_TOOLS = frozenset({"symbol_daily_candles", "contract_rank_snapshot"})


def check_allowed(tool: str) -> None:
    if tool not in READ_ONLY_TOOLS:
        raise ToolNotAllowed(f"{tool!r} is not an allowed TradingFlow tool; refusing to call it")


def _leaf(e: BaseException) -> BaseException:
    while getattr(e, "exceptions", None):  # anyio task groups wrap the real error
        e = e.exceptions[0]
    return e


class TradingFlowClient:
    """Synchronous facade, like ReadOnlyRobinhoodClient: each call_many() opens one MCP session."""

    def __init__(
        self,
        api_key: str | None = None,
        failure_threshold: int = 3,
        cooldown_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_key = api_key
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0

    @property
    def api_key(self) -> str:
        return self._api_key if self._api_key is not None else os.environ.get(KEY_ENV, "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def available(self) -> bool:
        return self.configured and self._clock() >= self._open_until

    async def _call_many(self, calls: list[tuple[str, dict[str, Any]]], key: str) -> list[dict[str, Any]]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        out: list[dict[str, Any]] = []
        async with create_mcp_http_client(headers={"x-api-key": key}) as http:
            async with streamable_http_client(
                SERVER_URL, http_client=http, max_sse_event_size=MAX_RESPONSE_BYTES
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for tool, args in calls:
                        check_allowed(tool)  # re-checked right before sending
                        result = await session.call_tool(tool, args)
                        out.append(parse_tool_result(tool, result))
        return out

    def call_many(self, calls: Iterable[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        calls = list(calls)
        for tool, _ in calls:
            check_allowed(tool)
        if not calls:
            return []
        key = self.api_key
        if not key:
            raise SourceUnavailable(f"tradingflow: {KEY_ENV} is not set")
        if self._clock() < self._open_until:
            raise SourceUnavailable("tradingflow: cooling down after repeated failures")
        try:
            out = asyncio.run(self._call_many(calls, key))
        except Exception as e:  # noqa: BLE001 - every failure becomes SourceUnavailable
            leaf = _leaf(e)
            if isinstance(leaf, ToolNotAllowed):
                raise leaf from None
            reason = f"{type(leaf).__name__}: {str(leaf)[:160]}".replace(key, "REDACTED")
            self._fail(reason)
            raise SourceUnavailable(f"tradingflow: {reason}") from None
        self._failures = 0
        return out

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.call_many([(tool, args)])[0]

    def _fail(self, reason: str) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._open_until = self._clock() + self.cooldown_s
            self._failures = 0
            log.warning("tradingflow unavailable (%s); pausing it for %.0f min",
                        reason, self.cooldown_s / 60)
