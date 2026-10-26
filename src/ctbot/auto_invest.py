"""Invest for me: one amount, and the bot picks and keeps funding the strategies (paper only).

The dashboard saves the plan (amount, max loss) to data/auto_invest.json. Every paper cycle
tops each pick up to its share as cash frees (stocks of dropped picks sell and settle), and once
a week it re-ranks the library, swapping a pick only for one clearly better. Picks run through
the launcher (ctbot.allocations), so each keeps its own money and max loss.
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from ctbot import allocations
from ctbot.config import Settings
from ctbot.paper.simulator import buying_power

log = logging.getLogger(__name__)

TOP = 5
REPICK = timedelta(days=7)
KEEP_EDGE = 1.2      # a new pick must score 20% better than the one it replaces
MAX_CORR = 0.8       # two picks whose daily moves track this closely count as one


def path(settings: Settings) -> Path:
    return settings.allocations_path.with_name("auto_invest.json")


def _read(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(settings: Settings, amount: float, max_loss_pct: float | None) -> None:
    p = path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"amount": amount, "max_loss_pct": max_loss_pct, "picks": {}, "picked_at": None}))


def free_for(settings: Settings, name: str) -> float:
    """Buying power (margin included) not promised to other running strategies."""
    ledger = _read(settings.paper_ledger_path)
    if not ledger:
        return settings.risk.max_trading_capital
    brackets = _read(settings.paper_risk_state_path).get("brackets", {})
    held: dict[str, float] = {}
    for p in ledger.get("positions", []):
        m = (brackets.get(p["symbol"]) or {}).get("model")
        held[m] = held.get(m, 0.0) + p["quantity"] * p["avg_cost"]
    others = sum(max(0.0, float(r["capital"]) - held.get(k, 0.0))
                 for k, r in allocations.load(settings.allocations_path).items()
                 if k != name and r.get("status") == allocations.RUNNING)
    cash, cost = float(ledger.get("cash", 0.0)), sum(held.values())
    return buying_power(cash, cash + cost, cost, settings.paper_margin) - others + held.get(name, 0.0)


def score(perf: dict[str, Any], name: str) -> float | None:
    """Simulated year's return per unit of its worst drop; None if it lost money or has no record."""
    w = (perf.get(name) or {}).get("windows") or {}
    w = w.get("1Y") or w.get("6M")
    return w["return"] / max(abs(w["worst_drop"]), 0.05) if w and w["return"] > 0 else None


def _family(name: str) -> str:  # lib_<family>_<universe>: the same rule on ETFs and on stocks
    return name.rsplit("_", 1)[0] if name.startswith("lib_") else name


def _corr(a: list, b: list) -> float:
    pa, pb = dict(map(tuple, a)), dict(map(tuple, b))
    days = sorted(pa.keys() & pb.keys())
    ra = [pa[d2] / pa[d1] - 1 for d1, d2 in pairwise(days)]
    rb = [pb[d2] / pb[d1] - 1 for d1, d2 in pairwise(days)]
    try:
        return statistics.correlation(ra, rb)
    except statistics.StatisticsError:  # too short or flat: can't tell, so not alike
        return 0.0


def pick(perf: dict[str, Any], names: list[str], budget: float, keep: tuple[str, ...] = (),
         top: int = TOP) -> dict[str, float]:
    """Split budget evenly over the best `top` names: one per rule family, none moving like one
    already picked. Names in `keep` get a KEEP_EDGE head start so picks don't churn."""
    scored = {n: s * (KEEP_EDGE if n in keep else 1) for n in names if (s := score(perf, n)) is not None}
    best: list[str] = []
    for n in sorted(scored, key=scored.get, reverse=True):
        if len(best) == min(top, int(budget // 10)):  # every slice at least $10
            break
        curve = (perf.get(n) or {}).get("curve") or []
        if any(_family(n) == _family(b) or _corr(curve, (perf.get(b) or {}).get("curve") or []) > MAX_CORR
               for b in best):
            continue
        best.append(n)
    return {n: int(budget / len(best) * 100) / 100 for n in best} if best else {}  # floor: never past budget


def run(settings: Settings, now: datetime, force: bool = False) -> dict[str, float]:
    """Re-rank when forced or a week old, then top each pick up toward its share.
    Returns {strategy: capital} for runs started or resized this call."""
    from ctbot import autopilot
    from ctbot.dashboard.state import PERF_PATH
    from ctbot.strategies.registry import load_strategy_config

    p = path(settings)
    plan = _read(p)
    if not plan:
        return {}
    amount = min(float(plan["amount"]), settings.risk.max_trading_capital)
    picked_at = datetime.fromisoformat(plan["picked_at"]) if plan.get("picked_at") else None
    runs = allocations.load(settings.allocations_path)
    if force or picked_at is None or now - picked_at >= REPICK:
        names = sorted(n for n, t in load_strategy_config(settings.strategies_file).items() if t.get("enabled"))
        verdicts = autopilot.decide(names, _read(settings.paper_ledger_path).get("fills", []), now)
        perf = _read(PERF_PATH).get("strategies", {})
        picks = pick(perf, [n for n in names if verdicts[n]["status"] == "trading"], amount,
                     keep=tuple(plan.get("picks") or ()))
        for name in allocations.running(settings.allocations_path).keys() - picks.keys():
            allocations.stop(settings.allocations_path, name, "Invest for me picked others")
        plan.update(picks=picks, picked_at=now.isoformat())
        p.write_text(json.dumps(plan))
        picked_at = now
        runs = allocations.load(settings.allocations_path)
        log.info("auto-invest: picked %s", picks or "nothing (no winning simulated year)")
    changed: dict[str, float] = {}
    for name, share in plan["picks"].items():
        r = runs.get(name) or {}
        if r and r.get("status") != allocations.RUNNING and r.get("updated_at", "") >= picked_at.isoformat():
            continue  # it hit its max loss or take profit this week: its money waits for the re-pick
        current = float(r["capital"]) if r.get("status") == allocations.RUNNING else 0.0
        new = share if share <= current else min(share, max(current, free_for(settings, name)))
        new = int(new * 100) / 100
        if new >= 10 and abs(new - current) >= 1:
            try:
                allocations.start(settings.allocations_path, name, new, settings.risk.max_trading_capital,
                                  plan.get("max_loss_pct"), r.get("take_profit_pct"))
            except ValueError as e:  # e.g. another run still holds money: retried next cycle
                log.info("auto-invest: %s not topped up: %s", name, e)
                continue
            changed[name] = new
    if changed:
        log.info("auto-invest: %s (paper)", ", ".join(f"{k} ${v:,.0f}" for k, v in changed.items()))
    return changed
