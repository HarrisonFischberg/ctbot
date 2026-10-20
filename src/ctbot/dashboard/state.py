"""Build the dashboard's JSON snapshot from the bot's local files. Read-only.

Trade ideas come from ideas.jsonl (written by the paper loop); their click status is merged
in from the manual order queue and its results.

Reads the paper ledger, risk state, equity history, strategy config and log file. It never
constructs PaperBroker (which can write the ledger) and never includes secrets: API keys
are not read, and the Robinhood account number is reduced to its last 4 digits.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctbot import manual_queue, market_hours
from ctbot.config import Settings, load_universe
from ctbot.models import Quote

QuotesFn = Callable[[list[str]], dict[str, Quote]]

MAX_HISTORY_POINTS = 2000
MAX_LOG_LINES = 300
MAX_FILLS = 50
MAX_IDEAS = 100
HEARTBEAT_GRACE_S = 15
PERF_PATH = Path("data/lab/performance.json")  # ctbot-perf: 3/6/12-month simulated returns


def _ideas(settings: Settings) -> list[dict[str, Any]]:
    ideas = _read_history(settings.ideas_path)[-MAX_IDEAS:]
    orders = manual_queue.read_orders(settings.manual_orders_path)
    results = {r["order_id"]: r for r in manual_queue.read_results(settings.manual_results_path)}
    by_idea: dict[str, dict[str, Any]] = {}
    for o in orders:  # latest click per idea wins
        if o.get("idea_id"):
            r = results.get(o["id"])
            by_idea[o["idea_id"]] = {"order_id": o["id"], "status": r["status"] if r else "queued",
                                     "reasons": r["reasons"] if r else [], "ts": o["ts"]}
    for idea in ideas:
        idea["click"] = by_idea.get(idea["id"])
    return list(reversed(ideas))


def _manual_activity(settings: Settings) -> list[dict[str, Any]]:
    results = {r["order_id"]: r for r in manual_queue.read_results(settings.manual_results_path)}
    out = []
    for o in manual_queue.read_orders(settings.manual_orders_path)[-30:]:
        r = results.get(o["id"])
        out.append({**o, "status": r["status"] if r else "queued", "reasons": r["reasons"] if r else []})
    return list(reversed(out))


C2_FIELDS = ("strategy_id", "name", "monthly_cost", "r3m", "r6m", "r12m", "maxdd_12m", "sharpe_12m",
             "trades_12m", "long_share", "median_hold_days", "last_date", "age_days", "fails")


def _c2(root: Path | None = None) -> dict[str, Any]:
    """Collective2 research files written by ctbot-c2-rank / ctbot-c2-track. No API calls."""
    from ctbot.research import c2_rank

    root = root or c2_rank.C2_DIR
    rankings = sorted(root.glob("ranking_*.json"))
    ranking = _read_json(rankings[-1]) if rankings else {}
    watch = _read_json(root / "watchlist.json").get("strategies", [])
    track = {r["strategy_id"]: r for r in _read_json(root / "track.json").get("rows", [])}
    by_id = {r["strategy_id"]: r for r in ranking.get("candidates", [])}
    rows = []
    for s in watch:
        sid = s["strategy_id"]
        r, t = by_id.get(sid, {}), track.get(sid, {})
        fwd, spy, fee = t.get("forward_return"), t.get("spy_return"), t.get("fee_drag_on_250")
        rows.append({**{k: r.get(k, s.get(k)) for k in C2_FIELDS}, "added": t.get("added"),
                     "as_of": t.get("as_of"), "months": t.get("months"), "forward_return": fwd,
                     "spy_return": spy, "fee_drag_on_250": fee,
                     "edge_after_fees": (fwd - (fee or 0) - spy) if fwd is not None and spy is not None else None})
    return {
        "ranked_at": ranking.get("generated_at"), "counts": ranking.get("counts", {}),
        "medians": ranking.get("medians", {}), "filters": ranking.get("filters", {}),
        "watchlist": rows, "tracked_at": _read_json(root / "track.json").get("updated"),
        "runners_up": [{k: r.get(k) for k in C2_FIELDS} for r in ranking.get("candidates", [])
                       if not r.get("fails") and r["strategy_id"] not in {w["strategy_id"] for w in watch}][:10],
    }


def model_stats(fills: list[dict[str, Any]], marks: dict[str, float]) -> list[dict[str, Any]]:
    """Per-model round trips from tagged fills (FIFO per symbol). A buy's tag names the model;
    the sell that closes it is credited to that model. Open lots are marked to market."""
    lots: dict[str, list[list]] = {}  # symbol -> [[qty, price, model, ts], ...]
    stats: dict[str, dict[str, Any]] = {}

    def st(m):
        return stats.setdefault(m, {"model": m, "closed": 0, "wins": 0, "pnl": 0.0, "rets": [],
                                    "open": 0, "open_pnl": 0.0, "exits": {}})

    for f in sorted(fills, key=lambda f: f["ts"]):
        sym, qty, px = f["symbol"], float(f["quantity"]), float(f["price"])
        if f["side"] == "buy":
            lots.setdefault(sym, []).append([qty, px, f.get("tag") or "untagged", f["ts"]])
            continue
        q = lots.get(sym, [])
        while qty > 1e-9 and q:
            lot = q[0]
            take = min(qty, lot[0])
            s = st(lot[2])
            pnl = take * (px - lot[1])
            s["pnl"] += pnl
            if take >= lot[0] - 1e-9:  # lot fully closed: count one trade
                s["closed"] += 1
                s["wins"] += px > lot[1]
                s["rets"].append(px / lot[1] - 1)
                reason = f.get("tag") or "?"
                s["exits"][reason] = s["exits"].get(reason, 0) + 1
            lot[0] -= take
            qty -= take
            if lot[0] <= 1e-9:
                q.pop(0)
    for sym, q in lots.items():
        for qty, px, model, _ in q:
            s = st(model)
            s["open"] += 1
            s["open_pnl"] += qty * (marks.get(sym, px) - px)
    out = []
    for s in stats.values():
        rets = s.pop("rets")
        s["win_rate"] = s["wins"] / s["closed"] if s["closed"] else None
        s["avg_ret"] = sum(rets) / len(rets) if rets else None
        out.append(s)
    return sorted(out, key=lambda s: s["pnl"] + s["open_pnl"], reverse=True)


def _research() -> dict[str, Any]:
    """Lab leaderboard, trend sweep and tearsheet list (files written by ctbot-lab/-sweep/-tearsheet)."""
    lab = _read_json(Path("data/lab/leaderboard.json"))
    sweep = _read_json(Path("data/lab/sweep_trend.json"))
    reports = sorted(p.name for p in Path("data/reports").glob("*.html")) if Path("data/reports").exists() else []
    return {"lab": {k: lab.get(k) for k in ("generated_at", "cash_apy", "periods", "rows", "walk_forward", "stress")}
            if lab else {},
            "sweep": {k: sweep.get(k) for k in ("symbol", "chosen_in_sample", "oos_sharpe_median",
                                                "oos_sharpe_of_chosen", "rows")} if sweep else {},
            "reports": reports}


def _bot_status(settings: Settings, now: datetime) -> dict[str, Any]:
    hb = _read_json(settings.heartbeat_path)
    if not hb.get("ts"):
        return {"running": False, "age_s": None, "asleep_until": None}
    age = (now - datetime.fromisoformat(hb["ts"])).total_seconds()
    return {"running": age <= HEARTBEAT_GRACE_S, "age_s": age, "asleep_until": hb.get("asleep_until")}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _read_history(path: Path) -> list[dict[str, Any]]:
    out: deque[dict[str, Any]] = deque(maxlen=MAX_HISTORY_POINTS)
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return list(out)


def _tail(path: Path, n: int) -> list[str]:
    try:
        with path.open(errors="replace") as fh:
            return [ln.rstrip("\n") for ln in deque(fh, maxlen=n)]
    except FileNotFoundError:
        return []


def _parse_log(lines: list[str]) -> list[dict[str, str]]:
    """'<date> <time>,ms LEVEL logger message' -> dicts; continuation lines join the previous."""
    out: list[dict[str, str]] = []
    for ln in lines:
        parts = ln.split(" ", 4)
        if len(parts) == 5 and parts[2] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            msg = parts[4]
            kind = (
                "fill" if msg.startswith("submitted") and "fill=None" not in msg
                else "reject" if msg.startswith(("rejected", "skipping entry"))
                else "risk" if "triggered" in msg or "blocked" in msg
                else "info"
            )
            out.append({"ts": f"{parts[0]} {parts[1]}", "level": parts[2], "logger": parts[3],
                        "msg": msg, "kind": kind})
        elif out:
            out[-1]["msg"] += "\n" + ln
    return out


def _describe(name: str, cls_name: str | None = None) -> str:
    """First paragraph of the model class docstring (what it does, its backtest note)."""
    try:
        from ctbot.strategies.registry import STRATEGY_CLASSES

        cls = STRATEGY_CLASSES[cls_name or name]
        doc = (cls.__doc__ or "").strip() or (__import__(cls.__module__, fromlist=["x"]).__doc__ or "").strip()
        return " ".join(doc.split("\n\n")[0].split())
    except Exception:  # noqa: BLE001
        return ""


def _strategies(path: Path) -> list[dict[str, Any]]:
    import tomllib

    from ctbot.dashboard.plain import describe

    perf = _read_json(PERF_PATH).get("strategies", {})

    try:
        cfg = tomllib.loads(path.read_text())
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return []
    return [
        {
            "name": name,
            "enabled": bool(t.get("enabled", False)),
            "weight": t.get("weight", 1.0),
            "standalone": bool(t.get("standalone", False)),
            "risk": t.get("risk", {}),
            "params": t.get("params", {}),
            "mode": t.get("mode", "trade"),
            "description": _describe(name, t.get("class")),
            "plain": describe(name, t.get("class"), t.get("params", {})),
            "performance": perf.get(name),
        }
        for name, t in cfg.items()
    ]


def _autopilot(settings: Settings, strategies: list[dict[str, Any]], fills: list[dict[str, Any]],
               now: datetime) -> dict[str, Any]:
    """Autopilot page: the two knobs, and which models it lets trade now and why."""
    from ctbot import autopilot

    cfg = autopilot.load(settings.autopilot_path)
    enabled = [s for s in strategies if s["enabled"]]
    verdicts = autopilot.decide([s["name"] for s in enabled], fills, now)
    models = [{"name": s["name"], "plain": s.get("plain"), "description": s.get("description"),
               "systematic": s["name"].startswith("trend_core"), "configured_mode": s["mode"],
               **verdicts[s["name"]]} for s in enabled]
    return {"enabled": cfg.enabled, "max_loss_pct": cfg.max_loss_pct, "giveback_pct": cfg.giveback_pct,
            "limits": autopilot.LIMITS, "models": models}


def _launcher(settings: Settings, positions: list[dict[str, Any]]) -> dict[str, Any]:
    """Paper launcher runs (ctbot.allocations) with what each one holds now."""
    from ctbot import allocations

    runs = []
    for name, r in allocations.load(settings.allocations_path).items():
        held = [p for p in positions if p.get("model") == name]
        open_pnl = sum(p["pnl"] for p in held)
        gain = float(r.get("realized", 0.0)) + open_pnl
        cost = sum(p["quantity"] * p["avg_cost"] for p in held)
        # cash promised to a running strategy but not spent yet (the bot sizes off cost, as here)
        waiting = max(0.0, float(r["capital"]) - cost) if r.get("status") == allocations.RUNNING else 0.0
        runs.append({"strategy": name, **r, "positions": len(held), "value": sum(p["market_value"] for p in held),
                     "cost": cost, "waiting": waiting,
                     "pnl": open_pnl, "gain": gain, "gain_pct": gain / float(r["capital"]) if r.get("capital") else 0.0})
    allocated = sum(float(r["capital"]) for r in runs if r.get("status") == allocations.RUNNING)
    # cash held for running strategies: not free for a new investment
    reserved = sum(r["waiting"] for r in runs)
    return {"runs": runs, "allocated": allocated, "reserved": reserved, "max_total": settings.risk.max_trading_capital}


def build_state(
    settings: Settings,
    quotes_fn: QuotesFn | None = None,
    now: datetime | None = None,
    data_error: str | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    ledger = _read_json(settings.paper_ledger_path)
    risk_state = _read_json(settings.paper_risk_state_path)
    universe = load_universe(settings.universe_file) if settings.universe_file.exists() else []

    positions = ledger.get("positions", [])
    symbols = sorted({p["symbol"] for p in positions} | set(universe))
    quotes: dict[str, Quote] = {}
    if quotes_fn and symbols:
        try:
            quotes = quotes_fn(symbols)
        except Exception as e:  # noqa: BLE001 - shown in the UI, never fatal
            data_error = data_error or f"{type(e).__name__}: {e}"

    cash = float(ledger.get("cash", settings.starting_capital))
    unsettled = sum(float(u["amount"]) for u in ledger.get("unsettled", []))
    brackets = risk_state.get("brackets", {})
    pos_out, market_value = [], 0.0
    for p in positions:
        q = quotes.get(p["symbol"])
        last = q.mid if q else None
        px = last if last else p["avg_cost"]
        mv = p["quantity"] * px
        market_value += mv
        b = brackets.get(p["symbol"], {})
        pos_out.append({
            "symbol": p["symbol"], "quantity": p["quantity"], "avg_cost": p["avg_cost"],
            "last": last, "market_value": mv, "pnl": mv - p["quantity"] * p["avg_cost"],
            "pnl_pct": (px / p["avg_cost"] - 1) if p["avg_cost"] else 0.0,
            "stop": b.get("stop"), "target": b.get("target"), "opened_at": p.get("opened_at"),
            "trail": b.get("trail"), "peak": b.get("peak"),
            "model": b.get("model"), "exit_at": b.get("exit_at"), "swings": b.get("swings", 0),
        })

    equity = cash + unsettled + market_value
    L = settings.risk
    peak = max(float(risk_state.get("peak_equity", settings.starting_capital)), equity)
    day_start = float(risk_state.get("day_start_equity", equity))
    today = market_hours.trading_date(now)
    day_trades = [d for d in risk_state.get("day_trade_dates", [])
                  if (today - datetime.fromisoformat(d).date()).days < 7]
    acct = settings.robinhood_account_number

    return {
        "generated_at": now.isoformat(),
        "mode": settings.mode.value,
        "live_trading_enabled": settings.live_trading_enabled,
        "account": {
            "nickname": settings.robinhood_account_nickname or "not set",
            "last4": acct[-4:] if acct else None,
        },
        "market": {
            "open": market_hours.session_open(now),
            "minutes_to_close": market_hours.minutes_to_close(now),
            "next_open": market_hours.next_open(now).strftime("%a %b %d · %H:%M ET"),
            # closed: the dashboard shows saved prices (dashboard/last_quotes.py); say how old
            "prices_as_of": max(q.ts for q in quotes.values()).astimezone(market_hours.ET).strftime(
                "%a %b %d · %H:%M ET") if quotes and not market_hours.session_open(now) else None,
            "et_time": now.astimezone(market_hours.ET).strftime("%a %b %d · %H:%M:%S ET"),
            "data_source": settings.market_data,
            "data_error": data_error,
        },
        "claude": {"model": settings.claude_model, "effort": settings.claude_effort,
                   "reviews_today": sum(1 for i in _read_history(settings.ideas_path)
                                        if i.get("decision") and i.get("ts", "")[:10] == now.date().isoformat()),
                   "review_budget": settings.max_claude_reviews_per_day},
        "entry_mode": settings.entry_mode,
        "preflight": _read_json(settings.paper_ledger_path.with_name("preflight.json")),
        "bot": _bot_status(settings, now),
        "ideas": _ideas(settings),
        "autopilot": _autopilot(settings, _strategies(settings.strategies_file), ledger.get("fills", []), now),
        "manual_orders": _manual_activity(settings),
        "collective2": _c2(),
        "research": _research(),
        "idea_scores": {k: v for k, v in _read_json(settings.ideas_path.with_name("idea_scores.json")).items()
                        if k in ("generated_at", "scored", "pending", "summary")},
        "models": [
            {**m, "losing_streak": risk_state.get("model_losing_streak", {}).get(m["model"], 0),
             "paused": 0 < L.model_max_losing_streak <= risk_state.get("model_losing_streak", {}).get(m["model"], 0)}
            for m in model_stats(ledger.get("fills", []), {p["symbol"]: p["last"] for p in pos_out if p["last"]})
        ],
        "account_summary": {
            "starting_capital": settings.starting_capital,
            "equity": equity,
            "cash": cash,
            "unsettled": unsettled,
            "market_value": market_value,
            "total_pnl": equity - settings.starting_capital,
            "peak_equity": peak,
            "drawdown_pct": (equity / peak - 1) if peak else 0.0,
            "day_start_equity": day_start,
            "day_pnl": equity - day_start,
            "day_pnl_pct": (equity / day_start - 1) if day_start else 0.0,
            "kill_switch": bool(risk_state.get("kill_switch", False)),
            "day_trades_5d": len(day_trades),
            "deployed": max(0.0, equity - cash),
        },
        "limits": {
            "max_position_pct": L.max_position_pct,
            "max_open_positions": L.max_open_positions,
            "max_daily_loss_pct": L.max_daily_loss_pct,
            "max_total_drawdown_pct": L.max_total_drawdown_pct,
            "max_day_trades_5d": L.max_day_trades_5d,
            "max_trading_capital": L.max_trading_capital,
            "max_spread_pct": L.max_spread_pct,
            "default_stop_loss_pct": L.default_stop_loss_pct,
            "max_stop_loss_pct": L.max_stop_loss_pct,
            "min_claude_confidence": L.min_claude_confidence,
            "min_share_price": L.min_share_price,
            "min_avg_dollar_volume": L.min_avg_dollar_volume,
            "no_entries_last_minutes": L.no_entries_last_minutes,
            "swing_min_profit_pct": L.swing_min_profit_pct,
            "swing_max_days": L.swing_max_days,
            "model_max_losing_streak": L.model_max_losing_streak,
        },
        "positions": pos_out,
        "fills": list(reversed(ledger.get("fills", [])))[:MAX_FILLS],
        "equity_history": _read_history(settings.equity_history_path),
        "watchlist": [
            {"symbol": s, "bid": quotes[s].bid, "ask": quotes[s].ask, "last": quotes[s].last,
             "spread_pct": ((quotes[s].ask - quotes[s].bid) / quotes[s].mid) if quotes[s].mid else None}
            if s in quotes else {"symbol": s}
            for s in universe
        ],
        "strategies": _strategies(settings.strategies_file),
        "launcher": _launcher(settings, pos_out),
        "benchmarks": _read_json(PERF_PATH).get("benchmarks", {}),
        "log": _parse_log(_tail(settings.paper_log_path, MAX_LOG_LINES)),
    }
