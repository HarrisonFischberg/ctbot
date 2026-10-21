"""Compact, secret-free context for the dashboard assistant, built from the bot's own files.

Starts from build_state() (what the dashboard shows) and adds what the dashboard trims: every idea
recorded today (not just the last 100), pre-counted by outcome and reason, and today's paper log
(not just the last 300 lines), pre-counted by what was filtered before an idea was recorded. The
model gets summary tables, not raw JSONL, and the whole thing is capped at MAX_CONTEXT_CHARS.

Never reads .env or ~/.ctbot/: build_state omits keys and keeps only the account's last 4
digits, and everything returned passes through safety.redact as a second line of defence.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ctbot import market_hours
from ctbot.assistant.safety import redact
from ctbot.config import Settings
from ctbot.dashboard.state import _parse_log, _read_json

MAX_CONTEXT_CHARS = 30_000
MAX_LOG_BYTES = 4_000_000  # today's log is read from the end, at most this much
TEXT = 220  # longest free-text field kept (rationales, log lines, descriptions)
# Rationales the decision engine writes when the Claude call itself failed (decision/engine.py).
CLAUDE_FAILURE = re.compile(r"^(rate_limited|api_error_\d+|connection_error|invalid_json|stop_reason_\w+)$")
EXECUTED = ("auto_submitted",)


def _short(s: Any, n: int = TEXT) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _day(ts: str | None) -> date | None:
    try:
        return market_hours.trading_date(datetime.fromisoformat(ts))
    except (TypeError, ValueError):
        return None


def ideas_on(path: Path, day: date) -> list[dict[str, Any]]:
    """Every idea recorded on `day` (New York date), oldest first. Reads the whole file."""
    out = []
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    idea = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(idea, dict) and _day(idea.get("ts")) == day:
                    out.append(idea)
    except FileNotFoundError:
        pass
    return out


def summarize_ideas(ideas: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts behind "why only N trades": outcomes, risk reasons, Claude verdicts and failures."""
    status = Counter(i.get("status") or "?" for i in ideas)
    reasons: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    approved = declined = 0
    confs = []
    for i in ideas:
        d = i.get("decision") or {}
        rat = str(d.get("rationale") or "")
        if d and CLAUDE_FAILURE.match(rat):
            failures[rat] += 1
        elif d:
            if d.get("approve"):
                approved += 1
            else:
                declined += 1
            if d.get("confidence") is not None and "systematic" not in rat:
                confs.append(float(d["confidence"]))
        if i.get("status") == "rejected":
            for r in (i.get("risk") or {}).get("reasons") or ["unknown"]:
                # confidence_0.55_below_0.65 -> one bucket, the numbers kept in an example
                reasons[re.sub(r"^confidence_[\d.]+_below_", "confidence_below_", r)] += 1
    by_status: dict[str, list[str]] = {}
    for i in ideas:
        by_status.setdefault(i.get("status") or "?", [])
        syms = by_status[i.get("status") or "?"]
        if i.get("symbol") not in syms and len(syms) < 10:
            syms.append(i.get("symbol"))
    return {
        "candidates": len(ideas),
        "symbols": len({i.get("symbol") for i in ideas}),
        "by_status": dict(status.most_common()),
        "executed_by_bot": sum(status[s] for s in EXECUTED),
        "recommended_waiting_for_you": status["recommended"],
        "shadow_not_executed": status["shadow"] + status["shadow_unreviewed"],
        "shadow_not_reviewed": status["shadow_unreviewed"],
        "review_budget_skipped": status["review_budget_used"],
        "too_close_to_close": status["too_close_to_close"],
        "claude_reviewed": approved + declined + sum(failures.values()),
        "claude_approved": approved,
        "claude_declined": declined,
        "claude_api_failures": dict(failures),
        "avg_claude_confidence": round(sum(confs) / len(confs), 3) if confs else None,
        "risk_rejections_by_reason": dict(reasons.most_common()),
        "symbols_by_status": by_status,
    }


def _today_log_lines(path: Path, local_day: date) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - MAX_LOG_BYTES))
            raw = fh.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return []
    lines = raw.splitlines()
    if size > MAX_LOG_BYTES and lines:
        lines = lines[1:]  # first line is probably cut
    stamp = local_day.isoformat()
    start = next((k for k, ln in enumerate(lines) if ln.startswith(stamp)), len(lines))
    return lines[start:]


CYCLE = re.compile(r"^(\d+) events, (\d+) signals, (\d+) candidates")


def summarize_log(path: Path, local_day: date) -> dict[str, Any]:
    """Today's paper log, counted: cycles, what was filtered before an idea was recorded,
    launcher changes, and the latest warnings and errors (numbers folded so repeats group)."""
    rows = _parse_log(_today_log_lines(path, local_day))
    c: Counter[str] = Counter()
    signals = cands = 0
    last_cycle = None
    launcher: list[str] = []
    problems: Counter[str] = Counter()
    latest: dict[str, dict[str, str]] = {}
    for r in rows:
        msg = r["msg"].split("\n", 1)[0]
        m = CYCLE.match(msg)
        if m:
            c["cycles"] += 1
            signals += int(m.group(2))
            cands += int(m.group(3))
            last_cycle = {"ts": r["ts"], "events": int(m.group(1)), "signals": int(m.group(2)),
                          "candidates": int(m.group(3))}
        for key, prefix in (("skipped_entry_near_close", "skipping entry"),
                            ("skipped_review_budget", "skipping review"),
                            ("shadow_longs", "shadow long"),
                            ("rejected_longs", "rejected long"),
                            ("recommended_longs", "recommended long"),
                            ("market_closed", "market closed"),
                            ("dashboard_clicks", "dashboard ")):
            if msg.startswith(prefix):
                c[key] += 1
        if re.match(r"over \d+ ideas this cycle", msg):
            c["over_max_candidates"] += 1
        if msg.startswith("submitted") and "fill=None" not in msg:
            c["fills"] += 1
        if msg.startswith("launcher:"):
            launcher.append(f"{r['ts'][11:16]} {_short(msg, 160)}")
        if r["level"] in ("WARNING", "ERROR", "CRITICAL"):
            key = r["level"] + " " + re.sub(r"\d+(\.\d+)?", "N", _short(msg, 120))
            problems[key] += 1
            latest[key] = {"ts": r["ts"], "level": r["level"], "msg": _short(msg)}
    return {
        "lines_today": len(rows),
        "cycles": c.pop("cycles", 0),
        "signals_total": signals,
        "candidates_total": cands,
        "last_cycle": last_cycle,
        "counts": dict(c),
        "launcher": launcher[-6:],
        "warnings_and_errors": [{**latest[k], "times": n} for k, n in problems.most_common(12)],
    }


def fills_on(ledger_path: Path, day: date) -> dict[str, Any]:
    fills = [f for f in _read_json(ledger_path).get("fills", []) if _day(f.get("ts")) == day]
    return {"buys": sum(f.get("side") == "buy" for f in fills), "sells": sum(f.get("side") == "sell" for f in fills),
            "list": [{"ts": f.get("ts", "")[11:16] + "Z", "side": f.get("side"), "symbol": f.get("symbol"),
                      "price": f.get("price"), "quantity": f.get("quantity"), "tag": f.get("tag")}
                     for f in fills[-12:]]}


def binding_limits(state: dict[str, Any], settings: Settings, ideas: dict[str, Any]) -> list[str]:
    """Plain statements of what is holding trading back right now, with the numbers."""
    a, L, out = state["account_summary"], state["limits"], []
    open_n = len(state["positions"])
    if not state["market"]["open"]:
        out.append(f"market closed (next open {state['market']['next_open']})")
    if a["kill_switch"]:
        out.append(f"kill switch is on (drawdown {a['drawdown_pct']:.1%} vs {L['max_total_drawdown_pct']:.0%} limit)")
    if a["day_pnl_pct"] <= -L["max_daily_loss_pct"]:
        out.append(f"daily loss halt: down {a['day_pnl_pct']:.1%} today vs {L['max_daily_loss_pct']:.0%} limit")
    if open_n >= L["max_open_positions"]:
        out.append(f"max_open_positions reached: {open_n}/{L['max_open_positions']}")
    if L.get("pdt_rule") and a["day_trades_5d"] >= L["max_day_trades_5d"]:
        out.append(f"PDT counter: {a['day_trades_5d']}/{L['max_day_trades_5d']} day trades in 5 days")
    if a["deployed"] >= L["max_trading_capital"] - 5:
        out.append(f"MAX_TRADING_CAPITAL: ${a['deployed']:.0f} of ${L['max_trading_capital']:.0f} deployed")
    cl = state["claude"]
    if cl["reviews_today"] >= cl["review_budget"]:
        out.append(f"Claude review budget used: {cl['reviews_today']}/{cl['review_budget']}")
    if state["entry_mode"] == "manual":
        out.append(f"entry_mode=manual: {ideas['recommended_waiting_for_you']} approved idea(s) today waited for your click")
    modes = Counter(s["mode"] for s in state["strategies"] if s["enabled"])
    runs = [r for r in state["launcher"]["runs"] if r.get("status") == "running"]
    if state["launcher"]["runs"]:
        out.append("launcher in charge: only " + (", ".join(r["strategy"] for r in runs) or "nothing")
                   + " may trade; every other model is shadow")
    elif modes.get("shadow"):
        out.append(f"{modes['shadow']} of {sum(modes.values())} enabled strategies are in shadow mode (suggest only)")
    paused = [m["model"] for m in state["models"] if m.get("paused")]
    if paused:
        out.append(f"paused for the day after a losing streak: {', '.join(paused)}")
    if not state["bot"]["running"]:
        out.append("paper bot not running (no heartbeat)" + (" - asleep until the open" if state["bot"].get("asleep_until") else ""))
    out.append(f"MIN_CLAUDE_CONFIDENCE={L['min_claude_confidence']}, MIN_AGREEING_STRATEGIES="
               f"{settings.ensemble.min_agreeing_strategies}, MIN_LONG_SCORE={settings.ensemble.min_long_score}")
    return out


def _compact_idea(i: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    d, rk = i.get("decision") or {}, i.get("risk") or {}
    out = {"id": i.get("id"), "time": (i.get("ts") or "")[11:16] + "Z", "symbol": i.get("symbol"),
           "direction": i.get("direction"), "status": i.get("status"), "score": i.get("score"),
           "strategies": i.get("strategies"), "claude": None if not d else {
               "approve": d.get("approve"), "confidence": d.get("confidence"),
               "rationale": _short(d.get("rationale"), 400 if detail else 140)},
           "risk_reasons": rk.get("reasons"), "click": (i.get("click") or {}).get("status")}
    if detail:
        out["signals"] = [{"strategy": s.get("strategy"), "direction": s.get("direction"),
                           "strength": s.get("strength"), "evidence": _short(json.dumps(s.get("evidence"), default=str), 300)}
                          for s in i.get("signals") or []][:6]
        out["quote"] = i.get("quote")
        out["planned_notional"] = rk.get("notional")
    return out


def _strategies(state: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for s in state["strategies"]:
        perf = s.get("performance") or {}
        out.append({"name": s["name"], "enabled": s["enabled"], "mode": s["mode"], "weight": s["weight"],
                    "standalone": s["standalone"], "plain": _short(s.get("plain") or s.get("description"), 160),
                    "backtest": {k: v for k, v in perf.items() if isinstance(v, (int, float, str))} if isinstance(perf, dict) else None})
    return out


def build_context(settings: Settings, state: dict[str, Any], now: datetime | None = None,
                  focus: dict[str, Any] | None = None, usage: dict[str, Any] | None = None) -> str:
    """The <bot_data> payload for one chat turn, as JSON text no longer than MAX_CONTEXT_CHARS."""
    now = now or datetime.now(timezone.utc)
    today = market_hours.trading_date(now)
    ideas = ideas_on(settings.ideas_path, today)
    summary = summarize_ideas(ideas)
    a = state["account_summary"]
    focus = focus or {}
    sym = str(focus.get("symbol") or "").upper()[:10]
    fdata: dict[str, Any] = {"tab": focus.get("tab")}
    if sym:
        fdata["symbol"] = sym
        fdata["position"] = next((p for p in state["positions"] if p["symbol"] == sym), None)
        fdata["quote"] = next((w for w in state["watchlist"] if w["symbol"] == sym and "bid" in w), None)
        fdata["ideas_today"] = [_compact_idea(i) for i in ideas if i.get("symbol") == sym][-6:]
    if focus.get("idea_id"):
        hit = next((i for i in reversed(ideas) if i.get("id") == focus["idea_id"]), None) or next(
            (i for i in state["ideas"] if i.get("id") == focus["idea_id"]), None)
        fdata["idea"] = _compact_idea(hit, detail=True) if hit else "not found"

    ctx: dict[str, Any] = {
        "now": {"utc": now.isoformat(timespec="minutes"), "new_york": state["market"]["et_time"],
                "trading_day": today.isoformat()},
        "user_is_looking_at": fdata,
        "today_trades": fills_on(settings.paper_ledger_path, today),
        "today_ideas": summary,
        "today_log": summarize_log(settings.paper_log_path, now.astimezone().date()),
        "binding_limits_now": binding_limits(state, settings, summary),
        "mode": state["mode"], "live_trading_enabled": state["live_trading_enabled"],
        "entry_mode": state["entry_mode"],
        "limits": state["limits"],
        "ensemble": {"min_long_score": settings.ensemble.min_long_score,
                     "min_exit_score": settings.ensemble.min_exit_score,
                     "min_agreeing_strategies": settings.ensemble.min_agreeing_strategies,
                     "max_candidates_per_cycle": settings.ensemble.max_candidates},
        "claude_reviews": {**state["claude"], "max_reviews_per_model_per_day": settings.max_reviews_per_model_per_day},
        "account": {k: a[k] for k in ("starting_capital", "equity", "cash", "unsettled", "market_value", "total_pnl",
                                      "day_pnl", "day_pnl_pct", "drawdown_pct", "kill_switch", "day_trades_5d", "deployed")},
        "positions": [{k: p.get(k) for k in ("symbol", "quantity", "avg_cost", "last", "pnl", "pnl_pct", "stop",
                                             "target", "model", "exit_at", "swings")} for p in state["positions"]],
        "models": [{k: m.get(k) for k in ("model", "closed", "wins", "win_rate", "pnl", "open", "open_pnl",
                                          "losing_streak", "paused", "exits")} for m in state["models"]],
        "strategies": _strategies(state),
        "launcher": {"runs": [{k: r.get(k) for k in ("strategy", "status", "capital", "max_loss_pct", "take_profit_pct",
                                                     "positions", "gain", "gain_pct", "waiting", "reason")}
                              for r in state["launcher"]["runs"]],
                     "allocated": state["launcher"]["allocated"], "max_total": state["launcher"]["max_total"]},
        "bot_heartbeat": state["bot"],
        "market": {k: state["market"].get(k) for k in ("open", "minutes_to_close", "next_open", "data_source",
                                                       "data_error", "prices_as_of")},
        "preflight": {"ts": state["preflight"].get("ts"), "ok": state["preflight"].get("ok"),
                      "failed": [c for c in state["preflight"].get("checks", []) if not c.get("ok")][:8]}
        if state.get("preflight") else None,
        "dashboard_clicks": [{k: o.get(k) for k in ("ts", "side", "symbol", "notional", "status", "reasons")}
                             for o in state["manual_orders"][:6]],
        "recent_ideas_today": [_compact_idea(i) for i in ideas[-14:]][::-1],
        "assistant_usage": usage,
    }
    ctx = redact(ctx)

    def dump(c: dict[str, Any]) -> str:
        # "<" escaped, so no idea or log text can close the <bot_data> block it is quoted in
        return json.dumps(c, default=str, separators=(",", ":")).replace("<", "\\u003c")

    text = dump(ctx)
    # Over the cap: shed the least useful detail first, then cut.
    for trim in (lambda c: c.update(recent_ideas_today=c["recent_ideas_today"][:5]),
                 lambda c: c.update(strategies=[{k: s[k] for k in ("name", "enabled", "mode", "weight")} for s in c["strategies"]]),
                 lambda c: c["today_log"].update(warnings_and_errors=c["today_log"]["warnings_and_errors"][:4]),
                 lambda c: c["today_trades"].update(list=c["today_trades"]["list"][-4:]),
                 lambda c: c["today_ideas"].pop("symbols_by_status", None)):
        if len(text) <= MAX_CONTEXT_CHARS:
            break
        trim(ctx)
        text = dump(ctx)
    return text[:MAX_CONTEXT_CHARS]
