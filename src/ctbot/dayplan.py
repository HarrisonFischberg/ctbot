"""Day workflow records: a premarket plan and an end-of-day reconciliation. Read-only.

`ctbot-dayplan` (before the open) writes data/day_plan/<date>.json: whether today is a session
(and an early close), the schedule each model runs on, which models can trade vs only watch,
risk headroom (positions, capital, day trades, kill switch, HALT file), the Claude budget, and
data freshness (cached bars, the ranking model's age). It ends in a plain verdict: entries
possible today, or "no trade" with the reasons. It never fetches quotes or calls an LLM.

`ctbot-reconcile` (after the close) writes data/eod/<date>.json and exits 1 on any mismatch:
* positions rebuilt from the ledger's fills (FIFO) match the ledger's positions;
* every bracket belongs to a held position and every held position has a bracket;
* every "filled" journal outcome has a matching ledger fill (same client_order_id);
plus attribution: realized P&L per model today, open P&L, cycles run vs no-trade cycles, and
today's Claude spend.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ctbot import llm_costs, market_hours
from ctbot.config import Settings

SCHEDULE = {  # when each model looks for setups (ET); cadence follows the 5-minute bar loop
    "orb": "09:35-11:00 after the 30-min range, 5-min bars", "orb_sip": "09:36-11:00, 5-min bars",
    "vwap_revert": "intraday, 5-min bars", "overnight": "15:30-15:50 entry, next open exit",
    "rsi2_etf": "15:30-15:44 on the live price", "turn_of_month": "15:30-15:44 on the live price",
    "trend_core": "15:30-15:44 on the live price", "ranker": "09:35-10:00 on yesterday's close, weekly",
}


def _strategies(path: Path) -> dict[str, dict[str, Any]]:
    from ctbot.strategies.registry import with_library

    with path.open("rb") as fh:
        return with_library(tomllib.load(fh))


def _json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def plan(settings: Settings, day: date, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    session = market_hours.is_trading_day(day)
    if not session:
        reasons.append("market closed (weekend or NYSE holiday)")
    cfg = _strategies(settings.strategies_file)
    models = []
    for name, t in cfg.items():
        if not isinstance(t, dict) or not t.get("enabled", False):
            continue
        cls = t.get("class", name)
        models.append({"model": name, "mode": t.get("mode", "trade"), "schedule": SCHEDULE.get(cls, "every cycle"),
                       "reviewed_by_claude": not cls.startswith(("trend_core", "ranker"))})
    trading = [m["model"] for m in models if m["mode"] == "trade"]
    if not trading:
        reasons.append("no model is in trade mode (all watching)")
    risk = _json(settings.paper_risk_state_path)
    ledger = _json(settings.paper_ledger_path)
    held = [p["symbol"] for p in ledger.get("positions", [])]
    window = [d for d in risk.get("day_trade_dates", []) if date.fromisoformat(d) > day - timedelta(days=7)]
    if risk.get("kill_switch"):
        reasons.append("drawdown kill switch is latched (delete the risk state file to clear)")
    if settings.halt_path.exists():
        reasons.append(f"HALT file present: {settings.halt_path}")
    if len(held) >= settings.risk.max_open_positions:
        reasons.append(f"already holding the maximum {settings.risk.max_open_positions} positions")
    spent = llm_costs.spent(settings.llm_usage_path, day)
    freshness = {}
    spy = Path("data/bars_cache/day/SPY.csv")
    if spy.exists():
        freshness["bars_cache_day_spy_modified"] = datetime.fromtimestamp(spy.stat().st_mtime, timezone.utc).isoformat(
            timespec="minutes")
    model_file = _json(Path("data/research/ranker_model.json"))
    if model_file:
        age = (day - date.fromisoformat(model_file["trained_through"])).days
        freshness["ranker_model_trained_through"] = model_file["trained_through"]
        freshness["ranker_model_age_days"] = age
    early = day in market_hours.early_closes(day.year)
    return {
        "date": day.isoformat(), "generated": now.isoformat(timespec="seconds"), "session": session,
        "close_et": market_hours.close_time(day).isoformat() if session else None, "early_close": early,
        "models": models, "trading_models": trading, "held": held,
        "risk_headroom": {"open_positions": len(held), "max_open_positions": settings.risk.max_open_positions,
                          "day_trades_recent": len(window), "max_day_trades_5d": settings.risk.max_day_trades_5d,
                          "kill_switch": bool(risk.get("kill_switch")), "halt_file": settings.halt_path.exists(),
                          "max_trading_capital": settings.risk.max_trading_capital},
        "claude_budget": {"spent_today_usd": round(spent, 4), "cap_usd": settings.max_claude_usd_per_day,
                          "review_shadow_ideas": settings.review_shadow_ideas, "model": settings.claude_model},
        "data_freshness": freshness,
        "verdict": "entries possible" if not reasons else "no trade",
        "reasons": reasons,
    }


def _fifo_positions(fills: list[dict[str, Any]]) -> dict[str, float]:
    pos: dict[str, float] = {}
    for f in sorted(fills, key=lambda f: f["ts"]):
        q = float(f["quantity"]) * (1 if f["side"] == "buy" else -1)
        pos[f["symbol"]] = pos.get(f["symbol"], 0.0) + q
    return {s: q for s, q in pos.items() if abs(q) > 1e-6}


def reconcile(settings: Settings, day: date) -> dict[str, Any]:
    from ctbot.contracts import Journal
    from ctbot.dashboard.state import model_stats

    ledger = _json(settings.paper_ledger_path)
    risk = _json(settings.paper_risk_state_path)
    fills = ledger.get("fills", [])
    ledger_pos = {p["symbol"]: float(p["quantity"]) for p in ledger.get("positions", [])}
    rebuilt = _fifo_positions(fills)
    issues: list[str] = []
    for s in sorted(set(ledger_pos) | set(rebuilt)):
        if abs(ledger_pos.get(s, 0.0) - rebuilt.get(s, 0.0)) > 1e-6:
            issues.append(f"position {s}: ledger {ledger_pos.get(s, 0.0):.6f} vs fills {rebuilt.get(s, 0.0):.6f}")
    brackets = risk.get("brackets", {})
    for s in sorted(set(brackets) - set(ledger_pos)):
        issues.append(f"bracket for {s} but no position (stale bracket)")
    for s in sorted(set(ledger_pos) - set(brackets)):
        issues.append(f"position {s} has no bracket (gets the default stop next cycle)")
    journal = Journal(settings.journal_path)
    ids = {f["client_order_id"] for f in fills}
    today = [r for r in journal.read() if r.get("ts", "")[:10] == day.isoformat()]
    for r in today:
        if r.get("kind") == "execution_outcome" and r.get("status") == "filled" and r["client_order_id"] not in ids:
            issues.append(f"journal fill {r['client_order_id']} ({r['symbol']}) missing from the ledger")
    todays_fills = [f for f in fills if market_hours.trading_date(datetime.fromisoformat(f["ts"])) == day]
    before = [f for f in fills if market_hours.trading_date(datetime.fromisoformat(f["ts"])) < day]
    realized_today = {}
    after_stats = {s["model"]: s["pnl"] for s in model_stats(before + todays_fills, {})}
    before_stats = {s["model"]: s["pnl"] for s in model_stats(before, {})}
    for m in after_stats:
        delta = after_stats[m] - before_stats.get(m, 0.0)
        if abs(delta) > 1e-9:
            realized_today[m] = round(delta, 2)
    cycles = [r for r in today if r.get("kind") == "cycle"]
    return {
        "date": day.isoformat(), "ok": not issues, "issues": issues,
        "fills_today": len(todays_fills), "realized_pnl_by_model": realized_today,
        "open_positions": ledger_pos, "cash": ledger.get("cash"),
        "cycles": len(cycles), "no_trade_cycles": sum(r.get("outcome") == "no_trade" for r in cycles),
        "claude_spend_usd": round(llm_costs.spent(settings.llm_usage_path, day), 4),
    }


def _write(kind: str, day: date, payload: dict[str, Any]) -> Path:
    out = Path("data") / kind / f"{day.isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str))
    return out


def _day(arg: str | None) -> date:
    return date.fromisoformat(arg) if arg else market_hours.trading_date(datetime.now(timezone.utc))


def plan_main() -> None:
    ap = argparse.ArgumentParser(description="Premarket plan (read-only)")
    ap.add_argument("--date")
    args = ap.parse_args()
    day = _day(args.date)
    p = plan(Settings.from_env(), day)
    path = _write("day_plan", day, p)
    print(f"{day}: {p['verdict']}" + (f" ({'; '.join(p['reasons'])})" if p["reasons"] else ""))
    print(f"trading models: {', '.join(p['trading_models']) or 'none'}; held: {', '.join(p['held']) or 'none'}")
    print(f"Claude spend today ${p['claude_budget']['spent_today_usd']:.2f} of ${p['claude_budget']['cap_usd']:.2f}")
    print(f"written: {path}")


def reconcile_main() -> None:
    ap = argparse.ArgumentParser(description="End-of-day reconciliation and attribution (read-only)")
    ap.add_argument("--date")
    args = ap.parse_args()
    day = _day(args.date)
    r = reconcile(Settings.from_env(), day)
    path = _write("eod", day, r)
    print(f"{day}: {'OK' if r['ok'] else 'MISMATCH'}; fills {r['fills_today']}, cycles {r['cycles']} "
          f"({r['no_trade_cycles']} no-trade), Claude ${r['claude_spend_usd']:.2f}")
    for m, v in r["realized_pnl_by_model"].items():
        print(f"  {m}: realized {v:+.2f}")
    for i in r["issues"]:
        print(f"  ! {i}")
    print(f"written: {path}")
    sys.exit(0 if r["ok"] else 1)
