"""End-of-day readout of every paper book (main + config/variants.toml), as Markdown.

ctbot-watchdog sends it once per session, 15 minutes after the close, to a side topic of
NOTIFY_URL (NOTIFY_URL + "-readout", as an attached file), so a reader that can't reach the server
(a cloud Claude session) can fetch it from ntfy's cache. The phone, subscribed only to NOTIFY_URL,
doesn't get it. ntfy.sh keeps the message 12 h and the attachment 3 h.

Paper figures only: book names, symbols, equity, fills, Claude spend, halts, and the paper log's
warning/error messages with numbers that look like addresses and long tokens masked. No keys, no
account numbers, no hostnames. `python -m ctbot.readout [YYYY-MM-DD]` prints it.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ctbot import llm_costs, market_hours, variants

_LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d) [\d:,]+ (WARNING|ERROR|CRITICAL) (\S+) (.*)$")
_MASK = [(re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b"), "<ip>"),
         (re.compile(r"[A-Za-z0-9_\-]{24,}"), "<token>"),
         (re.compile(r"\S+\.ts\.net\b"), "<host>")]


def mask(text: str) -> str:
    for pat, sub in _MASK:
        text = pat.sub(sub, text)
    return text


def _session(day: date) -> tuple[datetime, datetime]:
    return (datetime.combine(day, market_hours.OPEN, tzinfo=market_hours.ET),
            datetime.combine(day, market_hours.close_time(day), tzinfo=market_hours.ET))


def _equity(path: Path, day: date) -> tuple[float | None, float | None, int]:
    """(equity before the open, equity at the close, positions at the close)."""
    opened, closed = _session(day)
    start = end = None
    positions = 0
    try:
        lines = path.read_text().splitlines()[-20000:]
    except FileNotFoundError:
        return None, None, 0
    for ln in lines:
        try:
            r = json.loads(ln)
            ts = datetime.fromisoformat(r["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if ts < opened:
            start = r.get("equity")
        elif ts <= closed + timedelta(minutes=15):
            start = r.get("equity") if start is None else start
            end, positions = r.get("equity"), r.get("positions", 0)
    return start, end, positions


def _trades(ledger: Path, day: date) -> tuple[list[str], list[dict[str, Any]]]:
    """(symbols bought today, round trips closed today) from the ledger's fills, FIFO per symbol."""
    try:
        fills = json.loads(ledger.read_text()).get("fills", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return [], []
    lots: dict[str, list[list]] = {}
    buys, closes = [], []
    for f in sorted(fills, key=lambda f: f["ts"]):
        try:
            sym, qty, px = f["symbol"], float(f["quantity"]), float(f["price"])
            today = market_hours.trading_date(datetime.fromisoformat(f["ts"])) == day
        except (KeyError, ValueError, TypeError):
            continue
        if f.get("side") == "buy":
            lots.setdefault(sym, []).append([qty, px])
            if today:
                buys.append(sym)
            continue
        pnl, q = 0.0, lots.get(sym, [])
        left = qty
        while left > 1e-9 and q:
            take = min(left, q[0][0])
            pnl += take * (px - q[0][1])
            q[0][0] -= take
            left -= take
            if q[0][0] <= 1e-9:
                q.pop(0)
        if today:
            closes.append({"symbol": sym, "pnl": pnl, "why": f.get("tag") or "?"})
    return buys, closes


def _halts(s: Any) -> list[str]:
    out = []
    if s.halt_path.exists():
        out.append("HALT file (no new entries)")
    try:
        st = json.loads(s.paper_risk_state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    if st.get("kill_switch"):
        out.append("kill switch (drawdown limit)")
    if st.get("risk_off"):
        out.append("pulled back: " + "; ".join(st["risk_off"]))
    return out


def _log_events(log_path: Path, day: date, limit: int = 8) -> tuple[int, int, list[str]]:
    """(warnings, errors, the most frequent distinct messages) logged on `day`."""
    counts: dict[str, int] = {}
    warn = err = 0
    for p in sorted(log_path.parent.glob(log_path.name + "*"), reverse=True):  # rotated .3 .. .1, then live
        try:
            lines = p.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for ln in lines:
            m = _LOG_LINE.match(ln)
            if not m or m.group(1) != day.isoformat():
                continue
            warn += m.group(2) == "WARNING"
            err += m.group(2) != "WARNING"
            msg = f"{m.group(2)} {mask(m.group(4))[:140]}"
            counts[msg] = counts.get(msg, 0) + 1
    top = sorted(counts, key=lambda k: -counts[k])[:limit]
    return warn, err, [f"{k} (x{counts[k]})" if counts[k] > 1 else k for k in top]


def _fmt(x: float | None, spec: str) -> str:
    return "-" if x is None else spec.format(x)


def build(settings: Any, day: date) -> str:
    """The Markdown readout for `day`."""
    from ctbot.config import Settings

    books = [("main", "Your main account", settings)]
    try:
        for v in variants.load(settings.strategies_file.with_name(variants.CONFIG.name)):
            books.append((v.name, v.title, Settings.from_env(variants.book_env({}, settings.paper_ledger_path, v))))
    except (ValueError, OSError) as e:
        books.append(("?", f"variants not loaded: {e}", None))
    totals = {r["name"]: r for r in variants.summary(settings)}

    rows = []
    for name, title, s in books:
        if s is None:
            continue
        start, end, positions = _equity(s.equity_history_path, day)
        buys, closes = _trades(s.paper_ledger_path, day)
        pnl = end - start if isinstance(start, (int, float)) and isinstance(end, (int, float)) else None
        rows.append({"name": name, "title": title, "start": start, "end": end, "pnl": pnl,
                     "pct": pnl / start if pnl is not None and start else None, "positions": positions,
                     "buys": buys, "closes": closes, "usd": llm_costs.spent(s.llm_usage_path, day),
                     "halts": _halts(s), "total": totals.get(name, {})})
    rows.sort(key=lambda r: -(r["pnl"] if r["pnl"] is not None else -1e18))

    out = [f"# CTbot paper readout, {day:%a %Y-%m-%d}", "",
           "| # | book | day P&L | day % | closed (W/L) | buys | open at close | Claude $ | since start | vs main |",
           "|---|---|---:|---:|---|---:|---:|---:|---:|---|"]
    for i, r in enumerate(rows, 1):
        wins = sum(c["pnl"] > 0 for c in r["closes"])
        t, vs = r["total"], r["total"].get("vs_main")
        out.append(f"| {i} | {r['name']} | {_fmt(r['pnl'], '{:+,.2f}')} | {_fmt(r['pct'], '{:+.2%}')} | "
                   f"{len(r['closes'])} ({wins}/{len(r['closes']) - wins}) | {len(r['buys'])} | {r['positions']} | "
                   f"{r['usd']:.2f} | {_fmt(t.get('return'), '{:+.1%}')} | "
                   f"{vs['verdict'] + ' (' + str(vs['days']) + 'd)' if vs else '-'} |")
    out += ["", f"Claude spend today, all books: ${sum(r['usd'] for r in rows):.2f}", "", "## Books", ""]
    for r in rows:
        eq = "no cycle today" if r["end"] is None else f"equity ${r['end']:,.2f}"
        out.append(f"**{r['name']}** ({r['title']}): {eq}")
        if r["buys"]:
            out.append(f"- bought: {', '.join(r['buys'])}")
        for c in r["closes"]:
            out.append(f"- closed {c['symbol']} {c['pnl']:+,.2f} ({c['why']})")
        for h in r["halts"]:
            out.append(f"- {h}")
        out.append("")
    warn, err, msgs = _log_events(settings.paper_log_path, day)
    out += ["## Events", "", f"Paper log: {err} errors, {warn} warnings."]
    out += [f"- {m}" for m in msgs]
    return "\n".join(out) + "\n"


def due_day(now: datetime) -> date | None:
    """Today's trading date once the readout is due (15 min after the close), else None."""
    day = market_hours.trading_date(now)
    if not market_hours.is_trading_day(day) or now < _session(day)[1] + timedelta(minutes=15):
        return None
    return day


def send(url: str, day: date, text: str, timeout_s: float = 20.0) -> None:
    import httpx

    httpx.put(url, content=text.encode(), timeout=timeout_s,
              headers={"Filename": f"readout-{day}.md", "Title": f"CTbot paper readout {day}"}).raise_for_status()


def run(url: str, settings: Any, state_path: Path, now: datetime | None = None) -> bool:
    """Send today's readout once; a failed send is retried on the next run."""
    day = due_day(now or datetime.now(UTC))
    try:
        sent = json.loads(state_path.read_text()).get("readout")
    except (FileNotFoundError, json.JSONDecodeError):
        sent = None
    if day is None or sent == day.isoformat():
        return False
    send(url, day, build(settings, day))
    state_path.write_text(json.dumps({"readout": day.isoformat()}))
    return True


def main(argv: list[str] | None = None) -> None:
    from ctbot.config import Settings

    args = sys.argv[1:] if argv is None else argv
    day = date.fromisoformat(args[0]) if args else market_hours.trading_date(datetime.now(UTC))
    print(build(Settings.from_env(), day), end="")


if __name__ == "__main__":
    main()
