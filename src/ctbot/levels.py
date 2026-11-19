"""The bot sets its own stop and profit levels for the main paper account (paper only).

`ctbot-learn levels` runs nightly, after the bars and the idea log are fresh. It replays the main
account's approved ideas of the last LOOKBACK_DAYS on 5-minute bars under every (max loss, give-back)
pair in GRID, the same way RiskManager.set_bracket would have traded them, and writes the winner to
data/autopilot.json. The give-back is the profit-take: a position is sold when it falls that far
from its best price.

What keeps a nightly setter from chasing noise:
* It starts from PRIOR, the 2011-2026 backtest's answer (docs/research/profit-fixes): wide beats tight.
* After that it moves only when a pair beats the current one with t >= MIN_T on per-day averages
  (ideas on one day move together, so days are the sample, not ideas). MIN_T = 3 allows for picking
  the best of 25 pairs.
* Ideas whose model keeps its own exit (systematic, an exit rule or its own give-back) are left out:
  these numbers do not touch them.

LEVELS_AUTO=false makes it report only. Every change is a row in the learning history.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ctbot import autopilot
from ctbot.research import bars as cache
from ctbot.research.backtest import cost_bps
from ctbot.research.score_ideas import _plan

log = logging.getLogger(__name__)

STEPS = (0.04, 0.06, 0.08, 0.10, 0.12)
GRID = [(a, b) for a in STEPS for b in STEPS]  # (max_loss_pct, giveback_pct)
PRIOR = (0.08, 0.08)
LOOKBACK_DAYS, MIN_DAYS, MIN_IDEAS, MIN_T = 60, 20, 60, 3.0
DEFAULT_STOP = 0.05  # RiskLimits.default_stop_loss_pct, used when Claude gave no stop


def replay(idea: dict[str, Any], bars: list, max_loss: float, giveback: float) -> float | None:
    """Net return of one approved idea under these two numbers (None: not finished or no bars)."""
    plan = _plan(idea)
    if not plan or not bars or plan.get("systematic") or plan.get("exit_rule") or plan.get("giveback"):
        return None
    t0, exit_at = datetime.fromisoformat(idea["ts"]), datetime.fromisoformat(plan["exit_at"])
    i = next((k for k, b in enumerate(bars) if b.ts >= t0), None)
    if i is None or bars[-1].ts < exit_at:
        return None
    entry = bars[i].open
    if plan.get("stop") is not None:  # a model's own stop, floored by the max loss; the trail is the give-back
        stop, trail = max(min(float(plan["stop"]), entry * 0.999), entry * (1 - max_loss)), giveback
    else:
        d = (idea.get("decision") or {}).get("stop_loss_pct") or DEFAULT_STOP
        d = min(d / 100 if d > 1 else d, max_loss)  # old log rows hold percent, not a fraction
        stop, trail = entry * (1 - d), min(d, giveback)
    stop, peak = max(stop, entry * (1 - trail)), entry
    for b in bars[i:]:
        if b.ts >= exit_at:
            px = b.open
            break
        if b.low <= stop:
            px = min(stop, b.open)
            break
        peak = max(peak, b.high)
        stop = max(stop, peak * (1 - trail))
    else:
        return None
    c = cost_bps(idea["symbol"]) / 1e4
    return px * (1 - c) / (entry * (1 + c)) - 1


def evaluate(ideas: list[dict[str, Any]], bars_for, current: tuple[float, float], now: datetime) -> dict[str, Any]:
    """Per-day average replay return of every pair, and the best pair's lead over `current`."""
    pairs = GRID if current in GRID else GRID + [current]
    days: dict[str, list[list[float]]] = {}
    seen, n = set(), 0
    for idea in ideas:
        d = idea.get("decision") or {}
        key = (idea.get("ts"), idea.get("symbol"))
        if idea.get("direction") != "long" or not d.get("approve") or key in seen:
            continue
        seen.add(key)
        if datetime.fromisoformat(idea["ts"]) < now - timedelta(days=LOOKBACK_DAYS):
            continue
        bars = bars_for(idea["symbol"])
        row = [replay(idea, bars, a, b) for a, b in pairs]
        if None not in row:
            days.setdefault(idea["ts"][:10], []).append(row)
            n += 1
    out: dict[str, Any] = {"ideas": n, "days": len(days), "current": current, "best": None, "t": None, "gain_bps": None}
    if n < MIN_IDEAS or len(days) < MIN_DAYS:
        return out
    daily = [[statistics.mean(r[k] for r in rows) for k in range(len(pairs))] for _, rows in sorted(days.items())]
    mean = [statistics.mean(d[k] for d in daily) for k in range(len(pairs))]
    best, cur = max(range(len(pairs)), key=mean.__getitem__), pairs.index(current)
    diff = [d[best] - d[cur] for d in daily]
    sd = statistics.stdev(diff)
    out.update(best=pairs[best], gain_bps=statistics.mean(diff) * 1e4,
               t=statistics.mean(diff) / sd * len(diff) ** 0.5 if sd else (99.0 if statistics.mean(diff) > 0 else 0.0))
    return out


def run(settings: Any, now: datetime) -> dict[str, Any]:
    """Nightly: decide main's two numbers and, unless LEVELS_AUTO=false, write them."""
    from ctbot import learn, promote  # lazy: both import widely

    cfg = autopilot.load(settings.autopilot_path)
    current = (cfg.max_loss_pct, cfg.giveback_pct)
    folder = learn.learning_dir(settings)
    state_path = Path(folder) / "levels.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    ideas = []
    try:
        for ln in settings.ideas_path.read_text().splitlines():
            try:
                ideas.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    loaded: dict[str, list] = {}

    def bars_for(sym: str) -> list:
        if sym not in loaded:
            loaded[sym] = sorted(cache.load("5minute", sym), key=lambda b: b.ts)
        return loaded[sym]

    ev = evaluate(ideas, bars_for, current, now)
    if not state_path.exists():  # first run: the long backtest's answer, before any replay can speak
        target, why = PRIOR, "the 2011-2026 backtest: stops this wide beat tighter ones in both periods"
    elif ev["best"] and ev["best"] != current and ev["t"] >= MIN_T:
        target = ev["best"]
        why = (f"over {ev['ideas']} approved ideas on {ev['days']} days it beat the current levels by "
               f"{ev['gain_bps']:+.1f} bps an idea (t {ev['t']:.1f})")
    else:
        target, why = current, "no other pair is clearly better yet"
    ev.update(target=target, why=why, changed=False)
    if target != current and os.environ.get("LEVELS_AUTO", "true").strip().lower() not in ("0", "false", "no"):
        autopilot.save(settings.autopilot_path, autopilot.validate(
            {**asdict(cfg), "max_loss_pct": target[0], "giveback_pct": target[1]}))
        learn._append(folder, {
            "event": os.urandom(4).hex(), "ts": now.isoformat(timespec="seconds"), "trigger": "levels", "action": "set",
            "before": {"max_loss_pct": current[0], "giveback_pct": current[1]},
            "after": {"max_loss_pct": target[0], "giveback_pct": target[1]}, "evidence": why[:500], "refused": None})
        promote._notify("CTbot set its own stop levels",
                        f"Max loss {target[0]:.0%}, give-back {target[1]:.0%} (was {current[0]:.0%} and {current[1]:.0%}).\nWhy: {why}.")
        ev["changed"] = True
    if ev["changed"] or not state_path.exists():
        state_path.write_text(json.dumps({"ts": now.isoformat(timespec="seconds"), "max_loss_pct": target[0],
                                          "giveback_pct": target[1], "why": why, "applied": ev["changed"]}))
    return ev
