"""Read-only client for the Hey Astral MCP server (insider trades and tagged company news).

Same OAuth flow as the Robinhood client (dynamic client registration + PKCE, localhost:8765
callback), with its own token at ~/.ctbot/astral_oauth.json (mode 0600). Sign in once with
`ctbot-astral-login`; the bot never opens a browser and raises AstralLoginRequired instead.

The server URL is the one claude.ai shows for the Hey Astral connector, set as ASTRAL_MCP_URL.
Astral also has order, deploy, broker and sharing tools: the allowlist below is research reads
only, checked before every request, so none of those can be reached from CTbot.
"""

from __future__ import annotations

import os
from pathlib import Path

from ctbot.market_data.robinhood_mcp_client import ReadOnlyRobinhoodClient

URL_ENV = "ASTRAL_MCP_URL"
TOKEN_PATH = Path.home() / ".ctbot" / "astral_oauth.json"

READ_ONLY_TOOLS = frozenset({
    "astral_research_fiscal_observations",  # Form 4 insider trades, company news
    "astral_symbol_resolve",                 # login self-test
})


class AstralLoginRequired(RuntimeError):
    pass


class AstralClient(ReadOnlyRobinhoodClient):
    allowed = READ_ONLY_TOOLS
    login_error = AstralLoginRequired
    login_hint = "Hey Astral sign-in needed: run `.venv/bin/ctbot-astral-login` in a terminal"
    client_name = "ctbot read-only research"

    def __init__(self, server_url: str, token_path: Path = TOKEN_PATH, interactive: bool = False) -> None:
        if not server_url.startswith("https://"):
            raise ValueError(f"{URL_ENV} must be an https:// URL")
        super().__init__(token_path, interactive)
        self.server_url = server_url


def login_main() -> None:
    """`ctbot-astral-login`: one-time interactive sign-in, then a harmless test call."""
    import logging
    import sys

    url = os.environ.get(URL_ENV, "").strip()
    if not url:
        sys.exit(f"Set {URL_ENV} (claude.ai > Settings > Connectors > Hey Astral shows the URL).")
    logging.getLogger("mcp").setLevel(logging.CRITICAL)
    client = AstralClient(url, interactive=True)
    try:
        data = client.call("astral_symbol_resolve", {"query": "SPY"})
    except BaseException as e:  # noqa: BLE001 - unwrap anyio exception groups for one line
        leaf = e
        while getattr(leaf, "exceptions", None):
            leaf = leaf.exceptions[0]
        sys.exit(f"Sign-in failed: {type(leaf).__name__}: {leaf}")
    print(f"Signed in. Token saved to {client.token_path} (mode 0600).")
    print(f"Test lookup: SPY supported={((data.get('direct_resolution') or {}).get('supported'))}")


if __name__ == "__main__":
    login_main()
