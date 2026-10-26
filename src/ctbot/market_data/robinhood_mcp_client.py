"""Read-only client for Robinhood's agentic MCP server (https://agent.robinhood.com/mcp/trading).

Signs in with OAuth (dynamic client registration + PKCE, localhost callback). The token is
stored at ~/.ctbot/robinhood_oauth.json (mode 0600), outside the project.

WARNING: that token belongs to the agentic (MCP) account and Robinhood may let it place
orders. This client is the paper process's only path to the server, and it refuses any tool
not in READ_ONLY_TOOLS before a request is sent. Never widen the allowlist with order tools:
order placement belongs only in ctbot.live.

Sign in once, interactively, with `ctbot-robinhood-login`. The bot itself never opens a
browser: if the token is missing or cannot be refreshed it raises RobinhoodLoginRequired.
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import socket
import webbrowser
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

SERVER_URL = "https://agent.robinhood.com/mcp/trading"
TOKEN_PATH = Path.home() / ".ctbot" / "robinhood_oauth.json"
CALLBACK_PORT = 8765
MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # long bar histories exceed the SDK's 1 MB default
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"

# Market data and account reads only. Anything else is refused client-side.
READ_ONLY_TOOLS = frozenset(
    {
        "get_equity_quotes",
        "get_equity_historicals",
        "get_equity_tradability",
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_earnings_calendar",
        "get_equity_analyst_ratings",
        "preview_scan",  # runs a screen without saving anything (create_scan is NOT allowed)
    }
)


class RobinhoodLoginRequired(RuntimeError):
    pass


class ToolNotAllowed(PermissionError):
    pass


def check_allowed(tool: str) -> None:
    if tool not in READ_ONLY_TOOLS:
        raise ToolNotAllowed(f"{tool!r} is not a read-only tool; refusing to call it")


class _FileTokenStorage:
    """mcp TokenStorage backed by one JSON file readable only by the user."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, key: str, value: dict[str, Any]) -> None:
        data = self._read()
        data[key] = value
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.chmod(self.path, 0o600)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        self._write("tokens", tokens.model_dump(mode="json", exclude_none=True))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        self._write("client_info", client_info.model_dump(mode="json", exclude_none=True))


def _wait_for_callback(timeout_s: float = 300.0) -> tuple[str, str | None, str | None]:
    """Serve one request on localhost and return (code, state, iss) from the OAuth redirect.

    `iss` (RFC 9207) must be passed on: Robinhood advertises it, and the SDK rejects a
    redirect without it as a possible mix-up attack.
    """
    result: dict[str, str | None] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            q = parse_qs(urlparse(self.path).query)
            result["code"] = (q.get("code") or [None])[0]
            result["state"] = (q.get("state") or [None])[0]
            result["error"] = (q.get("error") or [None])[0]
            result["iss"] = (q.get("iss") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ctbot: sign-in received. You can close this tab.")

        def log_message(self, *args):  # keep the auth code out of stderr
            pass

    # Safari resolves `localhost` to ::1 first and doesn't fall back to 127.0.0.1, so listen on both.
    class HTTPServer6(HTTPServer):
        address_family = socket.AF_INET6

    servers = [HTTPServer(("127.0.0.1", CALLBACK_PORT), Handler)]
    try:
        servers.append(HTTPServer6(("::1", CALLBACK_PORT), Handler))
    except OSError:  # no IPv6 loopback on this machine
        pass
    try:
        ready, _, _ = select.select(servers, [], [], timeout_s)
        if ready:
            ready[0].handle_request()
    finally:
        for server in servers:
            server.server_close()
    if result.get("error") or not result.get("code"):
        raise RobinhoodLoginRequired(f"sign-in failed: {result.get('error') or 'no code received'}")
    return result["code"], result.get("state"), result.get("iss")  # type: ignore[return-value]


class ReadOnlyRobinhoodClient:
    """Synchronous facade. Each call() / call_many() opens one MCP session."""

    def __init__(self, token_path: Path = TOKEN_PATH, interactive: bool = False) -> None:
        self.token_path = token_path
        self.interactive = interactive

    def _auth(self):
        from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        async def redirect_handler(url: str) -> None:
            if not self.interactive:
                raise RobinhoodLoginRequired(
                    "Robinhood sign-in needed: run `.venv/bin/ctbot-robinhood-login` from the claude_robinhood folder in a terminal"
                )
            print("Opening your browser to sign in to Robinhood. If it does not open, visit:")
            print(url)
            webbrowser.open(url)

        async def callback_handler() -> AuthorizationCodeResult:
            code, state, iss = await asyncio.to_thread(_wait_for_callback)
            return AuthorizationCodeResult(code=code, state=state, iss=iss)

        metadata = OAuthClientMetadata(
            client_name="ctbot read-only market data",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
        return OAuthClientProvider(
            server_url=SERVER_URL,
            client_metadata=metadata,
            storage=_FileTokenStorage(self.token_path),
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )

    async def _call_many(self, calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        out: list[dict[str, Any]] = []
        async with create_mcp_http_client(auth=self._auth()) as http:
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
        try:
            return asyncio.run(self._call_many(calls))
        except BaseExceptionGroup as eg:  # anyio task groups wrap errors; surface ours plainly
            for kind in (RobinhoodLoginRequired, ToolNotAllowed):
                match, _ = eg.split(kind)
                if match:
                    raise match.exceptions[0] from None
            raise

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.call_many([(tool, args)])[0]


def parse_tool_result(tool: str, result) -> dict[str, Any]:
    """Return the JSON payload of a CallToolResult, raising on tool errors."""
    text = "".join(getattr(b, "text", "") for b in (result.content or []))
    if result.is_error:
        raise RuntimeError(f"{tool} failed: {text[:300]}")
    if isinstance(result.structured_content, dict):
        return result.structured_content
    return json.loads(text)


def login_main() -> None:
    """`ctbot-robinhood-login`: one-time interactive sign-in, then a read-only test call."""
    import logging
    import sys

    logging.getLogger("mcp").setLevel(logging.CRITICAL)  # we report the root cause below
    client = ReadOnlyRobinhoodClient(interactive=True)
    try:
        data = client.call("get_equity_quotes", {"symbols": ["SPY"]})
    except BaseException as e:  # noqa: BLE001 - unwrap anyio exception groups for one line
        leaf = e
        while getattr(leaf, "exceptions", None):
            leaf = leaf.exceptions[0]
        sys.exit(f"Sign-in failed: {type(leaf).__name__}: {leaf}")
    quote = data["data"]["results"][0]["quote"]
    print(f"Signed in. Token saved to {client.token_path} (mode 0600).")
    print(f"Test quote: SPY last {quote['last_trade_price']} (state {quote['state']})")


if __name__ == "__main__":
    login_main()
