"""Phone notifications for the day's milestones, sent by ctbot-watchdog (every minute on a server).

With NOTIFY_URL set (an ntfy.sh topic URL, e.g. https://ntfy.sh/ctbot-<random>; the free ntfy
app subscribes to it), the watchdog posts when the bot turns unhealthy, and when it recovers (the watchdog's reason,
once per change). The three daily updates (morning, midday, close) are ctbot.brief.

Messages carry paper figures and reasons only, never a key or an account number. Anyone who
knows the topic name can read it, so use a long random one. Sent state: data/notify_state.json.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _equity_rows(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text().splitlines()[-5000:]
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            r = json.loads(line)
            r["_ts"] = datetime.fromisoformat(r["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        rows.append(r)
    return rows


def due(state: dict[str, Any], healthy: bool, reason: str) -> list[tuple[str, str, str]]:
    """(state key, title, body) for each message to send now. Marks them sent in `state`."""
    out: list[tuple[str, str, str]] = []
    if state.get("healthy", True) != healthy:
        out.append(("healthy", "CTbot recovered" if healthy else "CTbot problem", reason))
    state["healthy"] = healthy
    return out


def send(url: str, title: str, body: str, timeout_s: float = 10.0) -> None:
    import httpx

    httpx.post(url, content=body.encode(), headers={"Title": title}, timeout=timeout_s).raise_for_status()


def run(url: str, state_path: Path, healthy: bool, reason: str) -> list[str]:
    """Send what's due; a message that fails to send is retried on the next run."""
    try:
        state = json.loads(state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    before = dict(state)
    sent = []
    for key, title, body in due(state, healthy, reason):
        try:
            send(url, title, body)
            sent.append(title)
        except Exception:  # noqa: BLE001 - keep the old value so the next run tries again
            if key in before:
                state[key] = before[key]
            else:
                state.pop(key, None)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(state_path)
    return sent
