"""`ctbot-preflight`: pre-open health check. Read-only; costs nothing.

Checks the things that silently turn a trading day into a no-trade day: the Robinhood
sign-in, the Claude API key/model, the paper bot's heartbeat, and the safety flags. Writes
data/preflight.json (the dashboard shows failures as an alert) and exits non-zero on failure.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any


def _key_source(key: str) -> str:
    """Where this process got a key, never its value: a launchd job doesn't see shell exports,
    so a key that works in Terminal can be stale or missing there."""
    import os

    from ctbot import secret_store

    value = os.environ.get(key)
    if not value:
        return "nowhere: not set"
    if key in secret_store.load():
        return "the Settings tab"
    try:
        from dotenv import dotenv_values, find_dotenv

        if dotenv_values(find_dotenv(usecwd=True)).get(key) == value:
            return ".env"
    except ImportError:
        pass
    return "the shell environment"


def run_checks(settings) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    add("paper mode, live off", settings.mode.value == "paper" and not settings.live_trading_enabled,
        f"mode={settings.mode.value} live={settings.live_trading_enabled}")
    if settings.market_data == "robinhood":
        try:
            from ctbot.market_data.robinhood_mcp_client import ReadOnlyRobinhoodClient

            q = ReadOnlyRobinhoodClient(interactive=False).call("get_equity_quotes", {"symbols": ["SPY"]})
            last = q["data"]["results"][0]["quote"]["last_trade_price"]
            add("robinhood sign-in", True, f"SPY last {float(last):.2f}")
        except Exception as e:  # noqa: BLE001
            add("robinhood sign-in", False, f"{type(e).__name__}: {e} -> run ctbot-robinhood-login")
    from ctbot.market_data.tradingflow_client import TradingFlowClient

    tf = TradingFlowClient()
    if tf.configured:  # optional backup source: only checked once a key is set
        try:
            bars = tf.call("symbol_daily_candles", {"symbol": "SPY", "lookback": 20})["bars"]
            add("tradingflow", True, f"SPY close {float(bars[-1]['close']):.2f} on {bars[-1]['date']}")
        except Exception as e:  # noqa: BLE001
            add("tradingflow", False, f"{e} -> check TRADINGFLOW_API_KEY")
    from ctbot.market_data.webull_client import WebullClient

    wb = WebullClient()
    if wb.configured:  # optional second candle source: only checked once both keys are set
        try:
            bars = wb.bars(["SPY"], "day", 5).get("SPY")
            if not bars:
                raise ValueError("webull: no SPY bars in the reply")
            add("webull", True, f"SPY close {bars[-1].close:.2f} on {bars[-1].ts.date().isoformat()}")
        except Exception as e:  # noqa: BLE001
            add("webull", False, f"{e} -> check the Webull keys, its OpenAPI market-data "
                                 "subscription, or press Connect Webull on Settings")
    try:
        import anthropic

        m = anthropic.Anthropic().models.retrieve(settings.claude_model)
        add("claude api", True, f"{m.id} ok")
    except Exception as e:  # noqa: BLE001
        add("claude api", False, f"{type(e).__name__}: {str(e)[:160]} (key from {_key_source('ANTHROPIC_API_KEY')})")
    from datetime import timedelta

    from ctbot import market_hours

    now = datetime.now(timezone.utc)
    # the bot must be up from an hour before the open through the close; off-hours a stopped bot is fine
    needed = market_hours.session_open(now) or market_hours.session_open(now + timedelta(hours=1))
    try:
        hb = json.loads(settings.heartbeat_path.read_text())
        age = (now - datetime.fromisoformat(hb["ts"])).total_seconds()
    except (FileNotFoundError, KeyError, ValueError):
        age = None
    if age is not None and age < 30:
        add("paper bot running", True, f"last heartbeat {age:.0f}s ago")
    elif needed:
        add("paper bot running", False, "not running -> start ./run_ctbot.sh")
    else:
        add("paper bot running", True, "stopped (fine: market closed)")
    return checks


def main() -> None:
    from ctbot.config import Settings

    settings = Settings.from_env()
    checks = run_checks(settings)
    ok = all(c["ok"] for c in checks)
    out = settings.paper_ledger_path.with_name("preflight.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "ok": ok, "checks": checks}, indent=2))
    for c in checks:
        print(f"{'OK  ' if c['ok'] else 'FAIL'} {c['check']:<20} {c['detail']}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
