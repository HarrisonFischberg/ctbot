"""`ctbot-dashboard`: local dashboard at http://127.0.0.1:8787. Paper trading only.

Binds to loopback and rejects requests whose Host header is not loopback (blocks DNS
rebinding). Endpoints:
  GET  /            the page (with a per-run random token embedded)
  GET  /api/state   JSON snapshot of the bot's files
  POST /api/paper/order   queue a PAPER buy/sell click for the paper loop to execute
  GET  /api/settings      which keys are set and where from (never the values)
  POST /api/settings      save or remove one key in ~/.ctbot/settings.json (secret_store)
  POST /api/settings/test run the read-only preflight checks
  POST /api/robinhood/connect  start the Robinhood sign-in (opens your browser)
  POST /api/robinhood/disconnect  delete the saved Robinhood sign-in
  GET  /api/bars?symbol=SPY&span=1d  candles for the stock chart (Robinhood, Webull, saved bars)
  POST /api/webull/connect     check the Webull keys; ask for a 2FA token if the Webull app wants one
  POST /api/webull/check       has the 2FA token been approved in the Webull app yet?
  POST /api/webull/disconnect  delete the saved Webull token

The order POST only appends to the paper queue; it never touches the ledger, Robinhood, or live
code. Every POST requires the page's token header and a same-origin request, so other websites
open in your browser cannot submit clicks or change keys. Stop the server with Ctrl+C.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from ctbot import allocations, manual_queue, secret_store
from ctbot.config import Settings
from ctbot.dashboard.state import _read_history, build_state

INDEX = Path(__file__).with_name("index.html")
log = logging.getLogger(__name__)


def _robinhood_md(settings: Settings):
    if settings.market_data != "robinhood":
        return None
    from ctbot.market_data.robinhood_mcp_client import ReadOnlyRobinhoodClient
    from ctbot.market_data.robinhood_readonly import RobinhoodReadOnlyMarketData

    return RobinhoodReadOnlyMarketData(
        ReadOnlyRobinhoodClient(interactive=False).call_many, quote_ttl_s=30.0
    )


def make_handler(settings: Settings, port: int):
    from ctbot.dashboard.candles import CandleSource
    from ctbot.market_data.webull_client import WebullClient

    md = _robinhood_md(settings)
    quotes = md.quotes if md is not None else None
    webull = WebullClient()
    candles = CandleSource(md, webull)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    token = secrets.token_urlsafe(24)

    def queue_order(body: dict) -> tuple[int, dict]:
        side = body.get("side")
        symbol = str(body.get("symbol", "")).upper()
        if side not in ("buy", "sell") or not re.fullmatch(r"[A-Z][A-Z.]{0,9}", symbol):
            return 400, {"error": "side must be buy or sell, with a valid symbol"}
        notional = body.get("notional")
        if side == "buy":
            try:
                notional = float(notional) if notional is not None else None
            except (TypeError, ValueError):
                return 400, {"error": "notional must be a number"}
            if notional is not None and not (1.0 <= notional <= settings.risk.max_trading_capital):
                return 400, {"error": f"notional must be $1-${settings.risk.max_trading_capital:.0f}"}
        idea = None
        if body.get("idea_id"):  # trust the bot's own idea record, not the browser
            idea = next((i for i in _read_history(settings.ideas_path)
                         if i.get("id") == body["idea_id"]), None)
            if idea is None or idea.get("symbol") != symbol:
                return 400, {"error": "unknown idea"}
        d = (idea or {}).get("decision") or {}
        order = manual_queue.enqueue(
            settings.manual_orders_path, side=side, symbol=symbol, notional=notional,
            idea_id=(idea or {}).get("id"), strategies=(idea or {}).get("strategies"),
            stop_loss_pct=d.get("stop_loss_pct"), take_profit_pct=d.get("take_profit_pct"),
        )
        log.info("queued paper %s %s (%s)", side, symbol, order["id"])
        return 202, {"queued": order["id"]}

    login: dict = {"proc": None, "started": 0.0}

    def settings_status() -> dict:
        from ctbot.market_data.robinhood_mcp_client import TOKEN_PATH

        p = login["proc"]
        if p is not None and p.poll() is None and time.time() - login["started"] > 300:
            p.terminate()  # sign-in abandoned: free the callback port
            p.wait(5)
        running = p is not None and p.poll() is None
        tok = webull.saved_token() or {}
        return {
            "keys": secret_store.status(),
            "robinhood": {"signed_in": TOKEN_PATH.exists(), "connecting": running,
                          "last_exit": None if p is None or running else p.returncode},
            # never the token itself: only whether one is saved and its status
            "webull": {"configured": webull.configured, "token": tok.get("status"), **wb},
        }

    wb: dict = {"needs_token": None, "checked": None, "error": None}  # last Connect result

    def webull_connect(_body: dict) -> tuple[int, dict]:
        from ctbot.net import SourceUnavailable

        wb.update(error=None, checked=None)
        if not webull.configured:
            return 400, {"error": "save the Webull App Key and App Secret first"}
        try:
            wb["needs_token"] = webull.token_required()
            if wb["needs_token"] and (webull.saved_token() or {}).get("status") != "NORMAL":
                webull.create_token()  # Webull texts a code; the user enters it in the Webull app
            if not wb["needs_token"] or webull.check_token() == "NORMAL":
                bars = webull.bars(["SPY"], "day", 5).get("SPY") or []
                wb["checked"] = f"SPY closed at {bars[-1].close:.2f} on {bars[-1].ts.date()}" if bars else "connected"
        except SourceUnavailable as e:
            wb["error"] = str(e)
        log.info("Webull connect: token %s, %s", "needed" if wb["needs_token"] else "not needed",
                 "ok" if wb["checked"] else "not yet")
        return 200, settings_status()

    def webull_check(_body: dict) -> tuple[int, dict]:
        from ctbot.net import SourceUnavailable

        try:
            if webull.check_token() == "NORMAL" and not wb["checked"]:
                return webull_connect({})
        except SourceUnavailable as e:
            wb["error"] = str(e)
        return 200, settings_status()

    def webull_disconnect(_body: dict) -> tuple[int, dict]:
        webull.forget_token()
        wb.update(needs_token=None, checked=None, error=None)
        log.info("Webull token deleted from this machine")
        return 200, settings_status()

    def save_setting(body: dict) -> tuple[int, dict]:
        key, value = body.get("key"), body.get("value", "")
        if not isinstance(key, str) or not isinstance(value, str):
            return 400, {"error": "key and value must be text"}
        try:
            secret_store.save(key, value)
        except ValueError as e:
            return 400, {"error": str(e)}
        value = value.strip()
        if value:
            os.environ[key] = value
        else:  # removed from Settings: fall back to .env, if it has one
            from dotenv import dotenv_values, find_dotenv

            fallback = dotenv_values(find_dotenv(usecwd=True)).get(key)
            if fallback:
                os.environ[key] = fallback
            else:
                os.environ.pop(key, None)
        log.info("setting %s %s", key, "saved" if value else "removed")  # never the value
        return 200, settings_status()

    def test_connections(_body: dict) -> tuple[int, dict]:
        from ctbot.preflight import run_checks

        return 200, {"checks": run_checks(Settings.from_env(dict(os.environ)))}

    def connect_robinhood(_body: dict) -> tuple[int, dict]:
        p = login["proc"]
        if p is not None and p.poll() is None:
            return 409, {"error": "a Robinhood sign-in is already open in your browser"}
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        with open(settings.log_dir / "robinhood-login.log", "ab") as out:
            login["proc"] = subprocess.Popen(
                [sys.executable, "-m", "ctbot.market_data.robinhood_mcp_client"],
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            )
        login["started"] = time.time()
        log.info("started Robinhood sign-in (pid %s)", login["proc"].pid)
        return 202, settings_status()

    def launchable() -> set[str]:
        from ctbot.strategies.registry import load_strategy_config

        cfg = load_strategy_config(settings.strategies_file)
        return {name for name, t in cfg.items() if t.get("enabled")}

    def launch(body: dict) -> tuple[int, dict]:
        name = body.get("strategy")
        if name not in launchable():
            return 400, {"error": "unknown or disabled strategy"}
        def pct(key):  # optional percentage, sent as a fraction (0.1 = 10%)
            v = body.get(key)
            return None if v in (None, "", 0) else float(v)

        def free_for(name: str) -> float:  # buying power not promised to other running strategies
            try:
                ledger = json.loads(settings.paper_ledger_path.read_text())
                brackets = json.loads(settings.paper_risk_state_path.read_text()).get("brackets", {})
            except (FileNotFoundError, json.JSONDecodeError):
                return settings.risk.max_trading_capital
            held: dict[str, float] = {}
            for p in ledger.get("positions", []):
                m = (brackets.get(p["symbol"]) or {}).get("model")
                held[m] = held.get(m, 0.0) + p["quantity"] * p["avg_cost"]
            others = sum(max(0.0, float(r["capital"]) - held.get(k, 0.0))
                         for k, r in allocations.load(settings.allocations_path).items()
                         if k != name and r.get("status") == allocations.RUNNING)
            return float(ledger.get("cash", 0.0)) - others + held.get(name, 0.0)

        try:
            capital = float(body.get("capital"))
            free = free_for(name)
            if capital > free + 0.01:
                raise ValueError(f"only ${max(0.0, free):,.0f} of buying power is free; the rest is kept for "
                                 "strategies you already invested in")
            run = allocations.start(settings.allocations_path, name, capital, settings.risk.max_trading_capital,
                                    pct("max_loss_pct"), pct("take_profit_pct"))
        except (TypeError, ValueError) as e:
            msg = str(e)
            return 400, {"error": msg if isinstance(e, ValueError) and ("$" in msg or "%" in msg) else "amounts must be numbers"}
        log.info("launcher: start %s with $%.2f (paper)", name, run["capital"])
        return 200, {"run": run}

    def launch_stop(body: dict) -> tuple[int, dict]:
        try:
            allocations.stop(settings.allocations_path, str(body.get("strategy")))
        except ValueError as e:
            return 400, {"error": str(e)}
        log.info("launcher: stop %s (paper)", body.get("strategy"))
        return 200, {"stopping": body.get("strategy")}

    def disconnect_robinhood(_body: dict) -> tuple[int, dict]:
        """Delete the Robinhood sign-in saved on this machine (data deletion, safeguard 20)."""
        from ctbot.market_data.robinhood_mcp_client import TOKEN_PATH

        p = login["proc"]
        if p is not None and p.poll() is None:
            p.terminate()
        TOKEN_PATH.unlink(missing_ok=True)
        log.info("Robinhood sign-in deleted from this machine")
        return 200, settings_status()

    routes = {
        "/api/robinhood/disconnect": disconnect_robinhood,
        "/api/launch": launch,
        "/api/launch/stop": launch_stop,
        "/api/paper/order": queue_order,
        "/api/settings": save_setting,
        "/api/settings/test": test_connections,
        "/api/robinhood/connect": connect_robinhood,
        "/api/webull/connect": webull_connect,
        "/api/webull/check": webull_check,
        "/api/webull/disconnect": webull_disconnect,
    }

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (http.server API)
            if self.headers.get("Host") not in allowed_hosts:
                return self._send(403, b"forbidden", "text/plain")
            path = self.path.split("?", 1)[0]
            if path == "/":
                page = INDEX.read_text().replace("__CTBOT_TOKEN__", token)
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            m = re.fullmatch(r"/reports/([A-Za-z0-9_.-]+\.html)", path)
            if m:  # quantstats tearsheets, served only from data/reports
                f = Path("data/reports") / m.group(1)
                if f.is_file():
                    return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
                return self._send(404, b"not found", "text/plain")
            if path == "/api/settings":
                return self._send(200, json.dumps(settings_status()).encode(), "application/json")
            if path == "/api/bars":
                q = parse_qs(self.path.partition("?")[2])
                sym = (q.get("symbol") or [""])[0].upper()
                if not re.fullmatch(r"[A-Z][A-Z.]{0,9}", sym):
                    return self._send(400, b'{"error": "bad symbol"}', "application/json")
                out = candles.get(sym, (q.get("span") or ["1d"])[0])
                return self._send(200, json.dumps(out).encode(), "application/json")
            if path == "/api/state":
                live = replace(settings,  # account saved on the Settings tab shows at once
                               robinhood_account_number=os.environ.get("ROBINHOOD_ACCOUNT_NUMBER", "").strip(),
                               robinhood_account_nickname=os.environ.get("ROBINHOOD_ACCOUNT_NICKNAME", "").strip())
                state = build_state(live, quotes, datetime.now(timezone.utc))
                return self._send(200, json.dumps(state, default=str).encode(), "application/json")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802 (http.server API)
            if self.headers.get("Host") not in allowed_hosts:
                return self._send(403, b"forbidden", "text/plain")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                return self._send(403, b"forbidden origin", "text/plain")
            if not secrets.compare_digest(self.headers.get("X-Ctbot-Token", ""), token):
                return self._send(403, b"bad token", "text/plain")
            route = routes.get(self.path.split("?", 1)[0])
            if route is None:
                return self._send(404, b"not found", "text/plain")
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(415, b"json only", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 4096:
                return self._send(413, b"bad length", "text/plain")
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                return self._send(400, b"bad json", "text/plain")
            status, payload = route(body if isinstance(body, dict) else {})
            return self._send(status, json.dumps(payload).encode(), "application/json")

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="ctbot dashboard (paper clicks only)")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = Settings.from_env()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(settings, args.port))
    print(f"ctbot dashboard (paper only): http://127.0.0.1:{args.port}  — Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
