"""`ctbot-watchdog`: tells an outside dead-man's-switch service that the paper bot is alive.

Run it every minute (a systemd timer on a server, see deploy/). Each run checks the bot's own
files and pings HEARTBEAT_URL, a Healthchecks.io-style check URL:

- ok:   GET  HEARTBEAT_URL
- fail: POST HEARTBEAT_URL/fail with a one-line reason

The service alerts you (email, phone push, SMS) on a /fail ping, and also when pings stop
arriving, which covers the case this script can't report: the whole machine is down.

Checks, all from files the bot already writes (nothing is fetched, no keys are read or sent):
1. data/paper_heartbeat.json is fresh: the bot process is alive (its heartbeat thread writes
   every 5 s, during and outside market hours).
2. During market hours, data/equity_history.jsonl gained a line recently: cycles are finishing,
   not failing every time (an expired Robinhood sign-in, a data outage).
3. The paper log's recent lines don't ask for a Robinhood sign-in.

With NOTIFY_URL set it also sends phone messages at the open, after the close and when the bot
turns unhealthy or recovers (ctbot.notify).

Without HEARTBEAT_URL it only prints the verdict, so it also works as a quick status check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctbot import market_hours

HEARTBEAT_MAX_AGE_S = 90
LOGIN_NEEDED = "Robinhood sign-in needed"


def _last_line(path: Path, max_bytes: int = 8192) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - max_bytes))
            lines = fh.read().decode("utf-8", "replace").strip().splitlines()
    except FileNotFoundError:
        return ""
    return lines[-1] if lines else ""


def _tail(path: Path, lines: int = 50, max_bytes: int = 65536) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - max_bytes))
            return fh.read().decode("utf-8", "replace").splitlines()[-lines:]
    except FileNotFoundError:
        return []


def _ts(raw) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def check(heartbeat_path: Path, equity_path: Path, log_path: Path,
          now: datetime | None = None) -> tuple[bool, str]:
    """(healthy, reason). Reads files only."""
    now = now or datetime.now(timezone.utc)
    try:
        hb = json.loads(heartbeat_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return False, "paper bot not running: no heartbeat file"
    beat = _ts(hb.get("ts"))
    if beat is None:
        return False, "paper bot heartbeat unreadable"
    age = (now - beat).total_seconds()
    if age > HEARTBEAT_MAX_AGE_S:
        return False, f"paper bot not running: last heartbeat {age:.0f}s ago"

    if any(LOGIN_NEEDED in line for line in _tail(log_path)):
        return False, "Robinhood sign-in needed: run ctbot-robinhood-login on the server"

    if market_hours.session_open(now):
        interval = int(hb.get("interval") or 300)
        # A cycle with fresh Claude reviews across several books can take minutes; three missed
        # cycles in a row is a stuck or failing loop, not a slow one.
        allowed = timedelta(seconds=3 * interval + 300)
        opened = datetime.combine(market_hours.trading_date(now), market_hours.OPEN, tzinfo=market_hours.ET)
        if now - opened > allowed:  # the first cycles after the open get the same allowance
            try:
                last = _ts(json.loads(_last_line(equity_path)).get("ts"))
            except (json.JSONDecodeError, AttributeError):
                last = None
            if last is None or now - last > allowed:
                mins = "never" if last is None else f"{(now - last).total_seconds() / 60:.0f} min ago"
                return False, f"cycles not finishing: last completed cycle {mins}"
        return True, f"ok: market open, heartbeat {age:.0f}s ago"
    return True, f"ok: market closed, bot waiting (heartbeat {age:.0f}s ago)"


def ping(url: str, healthy: bool, reason: str, timeout_s: float = 10.0) -> None:
    import httpx

    url = url.rstrip("/")
    if healthy:
        httpx.get(url, timeout=timeout_s)
    else:
        httpx.post(f"{url}/fail", content=reason.encode(), timeout=timeout_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ping a dead-man's-switch URL while the paper bot is healthy")
    parser.parse_args(argv)
    from ctbot.config import Settings

    settings = Settings.from_env()
    healthy, reason = check(settings.heartbeat_path, settings.equity_history_path, settings.paper_log_path)
    print(reason)
    notify_url = os.environ.get("NOTIFY_URL", "").strip()
    if notify_url:  # phone messages: market open / day summary / problem (ctbot.notify)
        from ctbot import notify

        for title in notify.run(notify_url, settings.paper_ledger_path.with_name("notify_state.json"), None,
                                healthy, reason, settings.equity_history_path, settings.paper_ledger_path):
            print(f"notified: {title}")
        from ctbot import readout

        try:  # every book's day, to a side topic the phone doesn't subscribe to (ctbot.readout)
            if readout.run(notify_url.rstrip("/") + "-readout", settings,
                           settings.paper_ledger_path.with_name("readout_state.json")):
                print("readout sent")
        except Exception as e:  # noqa: BLE001 - retried on the next run; never blocks the heartbeat
            print(f"readout failed: {type(e).__name__}", file=sys.stderr)
    url = os.environ.get("HEARTBEAT_URL", "").strip()
    if url:
        try:
            ping(url, healthy, reason)
        except Exception as e:  # noqa: BLE001 - no ping is itself the alert; say why and move on
            print(f"heartbeat ping failed: {type(e).__name__}", file=sys.stderr)
            return 2
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
