"""Phone notifications for the day's milestones, sent by ctbot-watchdog (every minute on a server).

With NOTIFY_URL set (an ntfy.sh topic URL, e.g. https://ntfy.sh/ctbot-<random>; the free ntfy
app subscribes to it), the watchdog posts:
- once per session, when the first cycle after the open has finished: "market open, bot trading",
  with the premarket plan's verdict (data/day_plan/<date>.json) and the paper equity;
- once per session, 15 minutes after the close: the day's paper equity change and fill count;
- when the bot turns unhealthy, and when it recovers (the watchdog's reason, once per change).

Messages carry paper figures and reasons only, never a key or an account number. Anyone who
knows the topic name can read it, so use a long random one. Sent state: data/notify_state.json.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ctbot import market_hours


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


def _fills_on(ledger_path: Path, day: date) -> int:
    try:
        fills = json.loads(ledger_path.read_text()).get("fills", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return 0
    n = 0
    for f in fills:
        try:
            n += market_hours.trading_date(datetime.fromisoformat(f["ts"])) == day
        except (ValueError, KeyError, TypeError):
            pass
    return n


def _plan_line(plan_dir: Path, day: date) -> str:
    try:
        p = json.loads((plan_dir / f"{day.isoformat()}.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return "No premarket plan today."
    if p.get("verdict") == "entries possible":
        models = ", ".join(p.get("trading_models", [])) or "none"
        return f"Plan: entries possible ({models})."
    return f"Plan: no trade ({'; '.join(p.get('reasons', []))})."


def due(state: dict[str, Any], now: datetime, healthy: bool, reason: str, equity_path: Path,
        ledger_path: Path, plan_dir: Path) -> list[tuple[str, str, str]]:
    """(state key, title, body) for each message to send now. Marks them sent in `state`."""
    out: list[tuple[str, str, str]] = []
    if state.get("healthy", True) != healthy:
        out.append(("healthy", "CTbot recovered" if healthy else "CTbot problem", reason))
    state["healthy"] = healthy

    day = market_hours.trading_date(now)
    if not market_hours.is_trading_day(day):
        return out
    opened = datetime.combine(day, market_hours.OPEN, tzinfo=market_hours.ET)
    closed = datetime.combine(day, market_hours.close_time(day), tzinfo=market_hours.ET)
    rows = _equity_rows(equity_path)
    today = [r for r in rows if opened <= r["_ts"] <= min(now, closed + timedelta(minutes=15))]
    before = [r for r in rows if r["_ts"] < opened]

    if state.get("open") != day.isoformat() and today and now < closed and healthy:
        eq = today[0].get("equity")
        body = f"First cycle done at {today[0]['_ts'].astimezone(market_hours.ET):%H:%M} ET. "
        body += f"Paper equity ${eq:,.2f}. " if isinstance(eq, (int, float)) else ""
        out.append(("open", "Market open: CTbot is trading (paper)", body + _plan_line(plan_dir, day)))
        state["open"] = day.isoformat()

    if state.get("close") != day.isoformat() and now >= closed + timedelta(minutes=15):
        if today:
            end = today[-1].get("equity")
            start = before[-1].get("equity") if before else today[0].get("equity")
            body = f"Paper equity ${end:,.2f}"
            if isinstance(start, (int, float)) and start:
                body += f" ({end - start:+,.2f}, {(end - start) / start:+.2%} today)"
            body += f". Fills today: {_fills_on(ledger_path, day)}."
        else:
            body = "No cycle finished today: check the bot."
        out.append(("close", "Market closed: CTbot day summary (paper)", body))
        state["close"] = day.isoformat()
    return out


def send(url: str, title: str, body: str, timeout_s: float = 10.0) -> None:
    import httpx

    httpx.post(url, content=body.encode(), headers={"Title": title}, timeout=timeout_s).raise_for_status()


def run(url: str, state_path: Path, now: datetime | None, healthy: bool, reason: str, equity_path: Path,
        ledger_path: Path, plan_dir: Path = Path("data/day_plan")) -> list[str]:
    """Send what's due; a message that fails to send is retried on the next run."""
    now = now or datetime.now(timezone.utc)
    try:
        state = json.loads(state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    before = dict(state)
    sent = []
    for key, title, body in due(state, now, healthy, reason, equity_path, ledger_path, plan_dir):
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
