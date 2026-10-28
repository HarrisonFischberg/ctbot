"""Compact, secret-free context for the dashboard assistant, built from the bot's own files.

Starts from build_state() (what the dashboard shows) and adds what the dashboard trims: every idea
recorded today (not just the last 100), pre-counted by outcome and reason, and today's paper log
(not just the last 300 lines), pre-counted by what was filtered before an idea was recorded. Every
open position, however old, gets its full story: when and at what price it was bought, which book
or launcher run owns it, why (strategies, signals, Claude's verdict), and its exit plan. Prices
and P&L all come from the one build_state() snapshot of this turn, so numbers agree with each
other. The model gets summary tables, not raw JSONL, and the whole thing is capped at MAX_CONTEXT_CHARS.

Never reads .env or ~/.ctbot/: build_state omits keys and keeps only the account's last 4
digits, and everything returned passes through safety.redact as a second line of defence.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ctbot import market_hours, trade_log
from ctbot.assistant.safety import redact
from ctbot.config import Settings
from ctbot.dashboard.state import _parse_log, _read_json

MAX_CONTEXT_CHARS = 40_000
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


def _dt(ts: str | None) -> datetime | None:
    try:
        t = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _et(ts: str | None) -> str | None:
    """'Mon Oct 26 09:30 ET' for an ISO time, so the model never has to convert zones."""
    t = _dt(ts)
    return t.astimezone(market_hours.ET).strftime("%a %b %d %H:%M ET") if t else None


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
            "list": [{"time": _et(f.get("ts")), "side": f.get("side"), "symbol": f.get("symbol"),
                      "price": f.get("price"), "quantity": f.get("quantity"), "tag": f.get("tag")}
                     for f in fills[-12:]]}


ENTRY_STATUSES = ("auto_submitted", "recommended")  # an idea that became (or could become) a buy


def entry_ideas(path: Path, symbols: set[str]) -> dict[str, list[dict[str, Any]]]:
    """symbol -> its approved ideas (any day), oldest first, for explaining why it was bought."""
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    i = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(i, dict) and i.get("symbol") in symbols and i.get("status") in ENTRY_STATUSES:
                    out.setdefault(i["symbol"], []).append(i)
    except FileNotFoundError:
        pass
    return out


def _why_from_idea(i: dict[str, Any]) -> dict[str, Any]:
    d = i.get("decision") or {}
    return {"idea_time": _et(i.get("ts")), "status": i.get("status"), "score": i.get("score"),
            "strategies": i.get("strategies"),
            "signals": [{"strategy": s.get("strategy"), "strength": s.get("strength"), "horizon_days": s.get("horizon_days"),
                         "evidence": _short(json.dumps(s.get("evidence"), default=str), 240)}
                        for s in i.get("signals") or []][:4],
            "claude": {"approve": d.get("approve"), "confidence": d.get("confidence"),
                       "rationale": _short(d.get("rationale"), 300),
                       "stop_loss_pct": d.get("stop_loss_pct"), "take_profit_pct": d.get("take_profit_pct")} if d else None}


def holdings(settings: Settings, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Every open position with its whole story, from the ledger, the risk state's brackets
    (exit plan + entry context saved at the buy) and the ideas file. Prices from this snapshot."""
    ledger = _read_json(settings.paper_ledger_path)
    brackets = _read_json(settings.paper_risk_state_path).get("brackets", {})
    fills = ledger.get("fills", [])
    runs = {r["strategy"]: r for r in state["launcher"]["runs"]}
    plain = {s["name"]: _short(s.get("plain") or s.get("description"), 200) for s in state["strategies"]}
    ideas = entry_ideas(settings.ideas_path, {p["symbol"] for p in state["positions"]})
    L = state["limits"]
    out = []
    for p in state["positions"]:
        sym, b = p["symbol"], brackets.get(p["symbol"], {})
        ctx = b.get("ctx") or {}
        opened = _dt(p.get("opened_at") or ctx.get("opened_at"))
        # this holding's buys and partial sells: everything since it was opened (an older closed
        # round trip in the same symbol is not part of it); a minute's slack for the fill's own stamp
        sym_fills = [f for f in fills if f.get("symbol") == sym and _dt(f.get("ts"))]
        mine = [f for f in sym_fills if not opened or _dt(f["ts"]) >= opened - timedelta(minutes=1)]
        if not mine:  # opened_at missing or rewritten: fall back to this symbol's recent fills
            mine = sym_fills[-6:]
        first = next((f for f in mine if f.get("side") == "buy"), None)
        opened = opened or _dt((first or {}).get("ts"))
        model = b.get("model")
        tags = sorted({m for f in mine if f.get("side") == "buy" for m in str(f.get("tag") or "").split(",") if m})
        models = ctx.get("models") or ([model] if model else tags)
        # the approved idea closest before the first buy (10 min slack for the order round trip)
        idea = next((i for i in reversed(ideas.get(sym, [])) if opened is None
                     or (_dt(i.get("ts")) and _dt(i["ts"]) <= opened + timedelta(minutes=10))), None)
        now = _dt(state["generated_at"])
        held_days = (now - opened).total_seconds() / 86400 if now and opened else None
        out.append({
            "symbol": sym, "quantity": round(p["quantity"], 4), "avg_cost": round(p["avg_cost"], 4),
            "price_now": p.get("last"), "market_value": round(p["market_value"], 2), "pnl": round(p["pnl"], 2),
            "pnl_pct": round(p["pnl_pct"], 4),
            "day_change": round(p["day_change"], 2) if p.get("day_change") is not None else None,
            "day_change_pct": round(p["day_change_pct"], 4) if p.get("day_change_pct") is not None else None,
            "bought": _et(opened.isoformat()) if opened else None, "held_days": round(held_days, 1) if held_days is not None else None,
            "fills": [{"time": _et(f.get("ts")), "side": f.get("side"), "quantity": round(float(f.get("quantity") or 0), 4),
                       "price": f.get("price"), "tag": f.get("tag")} for f in mine][-6:],
            "owner": f"launcher run {model} ({runs[model].get('status')})" if model in runs
            else (f"strategy {model}" if model else "main book"),
            "models": models,
            "what_the_models_do": {m: plain[m] for m in models if m in plain},
            "why": {"claude_confidence": ctx.get("confidence"), "claude_rationale": _short(ctx.get("rationale"), 300) or None,
                    "size_mult": ctx.get("size_mult"), "sector": ctx.get("sector"),
                    "stock_day_move_at_entry": ctx.get("stock_day_move"), "market_day_move_at_entry": ctx.get("market_day_move"),
                    "market_vs_trend_at_entry": ctx.get("market_vs_trend"), "account_pulled_back": ctx.get("pulled_back")}
            if ctx else "no entry context saved (bought before the trade log existed, or a systematic rebalance)",
            "idea_at_entry": _why_from_idea(idea) if idea else None,
            "exit_plan": {
                "stop": b.get("stop"), "target": b.get("target"),
                "trailing": {"giveback_pct": b["trail"], "peak": b.get("peak"), "entry": b.get("entry")} if b.get("trail") else None,
                "planned_sell_time": _et(b.get("exit_at")), "swings_so_far": b.get("swings", 0),
                "max_swings": L.get("swing_max_days"), "swing_min_profit_pct": L.get("swing_min_profit_pct"),
                "partial_profit_taken": b.get("partial_done", False),
                "risk_per_share_at_entry": b.get("r0") or ctx.get("r0"),
            } if b else "no bracket yet: the bot gives it the default stop at its next check",
        })
    return out


def closed_trades(path: Path, n: int = 12) -> list[dict[str, Any]]:
    """The last n finished round trips (trade_log.jsonl), newest first."""
    out = []
    for r in trade_log.read(path, n):
        ctx = r.get("context") or {}
        out.append({"symbol": r.get("symbol"), "models": r.get("models"), "bought": _et(r.get("opened_at")),
                    "sold": _et(r.get("closed_at")), "hold_hours": r.get("hold_hours"), "entry": r.get("entry"),
                    "exit": r.get("exit"), "pnl": r.get("pnl"), "ret": r.get("ret"), "exit_reason": r.get("exit_reason"),
                    "r_multiple": r.get("r_multiple"), "max_up": r.get("max_up"), "max_down": r.get("max_down"),
                    "why_bought": _short(ctx.get("rationale"), 160) or None, "note": r.get("note")})
    return out


def recent_fills(ledger_path: Path, n: int = 30) -> list[dict[str, Any]]:
    """The last n fills across all days, newest first."""
    return [{"time": _et(f.get("ts")), "side": f.get("side"), "symbol": f.get("symbol"), "price": f.get("price"),
             "quantity": round(float(f.get("quantity") or 0), 4), "tag": f.get("tag")}
            for f in reversed(_read_json(ledger_path).get("fills", [])[-n:])]


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
    ap = state.get("autopilot") or {}
    if ap.get("enabled"):  # the pipeline swaps MIN_CLAUDE_CONFIDENCE for the Autopilot floor while it is on
        out.append(f"Claude confidence floor in use: {ap['min_confidence']:.0%} (Autopilot setting; "
                   f"MIN_CLAUDE_CONFIDENCE={L['min_claude_confidence']} applies only with the Autopilot off)")
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
        "prices_snapshot": {"taken": state["market"]["et_time"], "saved_prices_from": state["market"].get("prices_as_of"),
                            "note": "every price, P&L and day change below is from this one snapshot"},
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
        "open_positions": holdings(settings, state),
        "recent_fills_all_days": recent_fills(settings.paper_ledger_path),
        "closed_trades": closed_trades(settings.trade_log_path),
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
                 lambda c: c.update(recent_fills_all_days=c["recent_fills_all_days"][:10]),
                 lambda c: c.update(closed_trades=c["closed_trades"][:5]),
                 lambda c: [p.pop("idea_at_entry", None) for p in c["open_positions"]],
                 lambda c: c.update(strategies=[{k: s[k] for k in ("name", "enabled", "mode", "weight")} for s in c["strategies"]]),
                 lambda c: c["today_log"].update(warnings_and_errors=c["today_log"]["warnings_and_errors"][:4]),
                 lambda c: c["today_trades"].update(list=c["today_trades"]["list"][-4:]),
                 lambda c: c["today_ideas"].pop("symbols_by_status", None)):
        if len(text) <= MAX_CONTEXT_CHARS:
            break
        trim(ctx)
        text = dump(ctx)
    return text[:MAX_CONTEXT_CHARS]
