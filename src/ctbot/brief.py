"""Phone updates at 9:45, 12:45 and 15 minutes after the close (ET), sent by ctbot-watchdog to NOTIFY_URL.

Each update gives main's day P&L next to SPY/QQQ, where main ranks among the paper books, today's
closed trades, any stock re-bought after a stop-out, then a short "why" written by Claude (Haiku).
The Claude call counts against MAX_CLAUDE_USD_PER_DAY and is skipped once that is spent, with no
key, or when it fails, so the numbers always go out. A slot missed (server down) is skipped once
the next one is due. Paper figures only, never a key or an account number. State: data/brief_state.json.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from ctbot import llm_costs, market_hours, notify, readout

SLOTS = (("Morning", time(9, 45)), ("Midday", time(12, 45)), ("Close", None))  # None: 15 min after the close
MODEL = "claude-haiku-5-5"
SYSTEM = (
    "You write a short phone update about a paper-trading bot's day. Using only the facts given, explain in "
    "plain English, at most 70 words and no lists, why the main book is up or down and how that compares with "
    "SPY and QQQ: name the trades or market move responsible. No advice, no hype, no markdown."
)


def due_slot(now: datetime, state: dict[str, Any]) -> tuple[str, date] | None:
    """(slot, trading day) to send now: the latest slot whose time has passed, if not yet sent."""
    day = market_hours.trading_date(now)
    if not market_hours.is_trading_day(day):
        return None
    _, closed = readout._session(day)
    latest = None
    for name, t in SLOTS:
        at = closed + timedelta(minutes=15) if t is None else datetime.combine(day, t, tzinfo=market_hours.ET)
        if now >= at and (t is None or now < closed):
            latest = name
    if latest is None or state.get(latest) == day.isoformat():
        return None
    return latest, day


def _fills_today(ledger: Path, day: date) -> list[dict[str, Any]]:
    try:
        fills = json.loads(ledger.read_text()).get("fills", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    out = []
    for f in fills:
        try:
            ts = datetime.fromisoformat(f["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if market_hours.trading_date(ts) == day:
            out.append({"time": f"{ts.astimezone(market_hours.ET):%H:%M}", "symbol": f.get("symbol"),
                        "side": f.get("side"), "price": round(float(f.get("price") or 0), 2), "why": f.get("tag")})
    return sorted(out, key=lambda f: f["time"])


def rebought(fills: list[dict[str, Any]]) -> list[str]:
    """Symbols bought again after a stop-out sell earlier the same day."""
    stopped: set[str] = set()
    out: list[str] = []
    for f in fills:
        if f["side"] == "sell" and "stop" in (f["why"] or ""):  # stop_loss, trailing_stop, stopped
            stopped.add(f["symbol"])
        elif f["side"] == "buy" and f["symbol"] in stopped and f["symbol"] not in out:
            out.append(f["symbol"])
    return out


def _market(equity_path: Path, day: date) -> dict[str, float]:
    rows = [r for r in notify._equity_rows(equity_path) if market_hours.trading_date(r["_ts"]) == day]
    return next((r["market"] for r in reversed(rows) if r.get("market")), {})


def plan_line(plan_dir: Path, day: date) -> str:
    try:
        p = json.loads((plan_dir / f"{day.isoformat()}.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return "No premarket plan today."
    if p.get("verdict") == "entries possible":
        return f"Plan: entries possible ({', '.join(p.get('trading_models', [])) or 'none'})."
    return f"Plan: no trade ({'; '.join(p.get('reasons', []))})."


def facts(settings: Any, day: date) -> tuple[str, list[dict[str, Any]]]:
    """(the numbers, as plain lines; main's fills today)."""
    books = readout.rows(settings, day)
    main = next((r for r in books if r["name"] == "main"), None)
    if main is None or main["end"] is None:
        return "No cycle finished today: check the bot.", []
    fills = _fills_today(settings.paper_ledger_path, day)
    mkt = _market(settings.equity_history_path, day)
    pnl = main["pnl"] or 0.0
    head = f"Main {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f} ({readout._fmt(main['pct'], '{:+.2%}')})"
    head += "".join(f" · {s} {m:+.1%}" for s, m in mkt.items())
    ranked = [r for r in books if r["pct"] is not None]
    lines = [head]
    if len(ranked) > 1 and main in ranked:
        lines.append(f"Rank {ranked.index(main) + 1} of {len(ranked)} books · best {ranked[0]['name']} "
                     f"{ranked[0]['pct']:+.2%} · worst {ranked[-1]['name']} {ranked[-1]['pct']:+.2%}")
    if main["closes"]:
        lines.append("Closed: " + ", ".join(f"{c['symbol']} {c['pnl']:+,.0f} ({c['why']})" for c in main["closes"]))
    if again := rebought(fills):
        lines.append("Re-bought after a stop-out: " + ", ".join(again))
    lines.append(f"Open: {main['positions']} positions" + "".join(f" · {h}" for h in main["halts"]))
    return "\n".join(lines), fills


def explain(settings: Any, day: date, numbers: str, fills: list[dict[str, Any]], client: Any = None,
            now: datetime | None = None) -> str | None:
    """Claude's short "why"; None when skipped (no key, today's cap spent) or the call fails."""
    usage = settings.llm_usage_path
    if llm_costs.spent_all_books(usage, day) >= settings.max_claude_usd_per_day:
        return None
    model = os.environ.get("BRIEF_MODEL", MODEL)
    try:
        if client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return None
            import anthropic

            client = anthropic.Anthropic()
        r = client.messages.create(model=model, max_tokens=300, system=SYSTEM, messages=[
            {"role": "user", "content": numbers + "\n\nMain's fills today:\n" + json.dumps(fills)}])
        if getattr(r, "usage", None) is not None:
            llm_costs.record(usage, model, r.usage, "brief", now)
        text = next((b.text for b in r.content if b.type == "text"), "").strip()
    except Exception:  # noqa: BLE001 - the numbers still go out
        return None
    return text or None


def run(url: str, settings: Any, state_path: Path, now: datetime | None = None, client: Any = None,
        plan_dir: Path = Path("data/day_plan")) -> str | None:
    """Send the due update once; a failed send is retried on the next run. Returns its title."""
    now = now or datetime.now(UTC)
    try:
        state = json.loads(state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    slot = due_slot(now, state)
    if slot is None:
        return None
    name, day = slot
    numbers, fills = facts(settings, day)
    why = explain(settings, day, numbers, fills, client, now) if numbers.startswith("Main") else None
    if name == "Morning":
        numbers += "\n" + plan_line(plan_dir, day)
    title = f"CTbot {name.lower()} update (paper)"
    notify.send(url, title, numbers + (f"\n\n{why}" if why else ""))
    state[name] = day.isoformat()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state))
    return title
