"""Paper strategy launcher: "run this strategy with $N and let it go".

The dashboard writes data/allocations.json; the paper bot reads it every cycle. While any
strategy is running here, the launcher decides what trades: each running strategy trades on
its own (no other model has to agree) and is sized off its own capital, never more; every other
model drops to shadow. With nothing running, config/strategies.toml applies as before.

Each run may carry a max loss and a take-profit, as a share of its capital: when the run's gain or
loss (sold + still held) reaches either, the bot stops it and sells. Stopping marks it
"stopping": the paper bot sells the positions it opened at the next open-market cycle, then
marks it "stopped". Paper only: nothing here can reach live code.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNNING, STOPPING, STOPPED = "running", "stopping", "stopped"


def load(path: Path) -> dict[str, dict[str, Any]]:
    try:
        runs = json.loads(path.read_text()).get("runs", {})
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return {}
    return {k: v for k, v in runs.items() if isinstance(v, dict)}


def _write(path: Path, runs: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"runs": runs}, indent=2))
    os.replace(tmp, path)  # the bot never reads a half-written file


def running(path: Path) -> dict[str, float]:
    """Strategy name -> capital, for strategies that may open positions."""
    return {k: float(v["capital"]) for k, v in load(path).items() if v.get("status") == RUNNING}


def stopping(path: Path) -> set[str]:
    return {k for k, v in load(path).items() if v.get("status") == STOPPING}


def start(path: Path, name: str, capital: float, max_total: float,
          max_loss_pct: float | None = None, take_profit_pct: float | None = None) -> dict[str, Any]:
    """Start (or change) a run. Raises ValueError when the total would exceed max_total."""
    if not 10.0 <= capital <= max_total:
        raise ValueError(f"amount must be $10-${max_total:,.0f}")
    if max_loss_pct is not None and not 0.01 <= max_loss_pct <= 0.9:
        raise ValueError("max loss must be 1%-90%")
    if take_profit_pct is not None and not 0.01 <= take_profit_pct <= 10:
        raise ValueError("take profit must be 1%-1000%")
    runs = load(path)
    others = sum(float(v["capital"]) for k, v in runs.items() if k != name and v.get("status") == RUNNING)
    if others + capital > max_total + 1e-9:
        raise ValueError(f"only ${max_total - others:,.0f} of the ${max_total:,.0f} paper capital is unallocated")
    now = datetime.now(timezone.utc).isoformat()
    prev = runs.get(name, {})
    again = prev.get("status") == RUNNING
    runs[name] = {"capital": round(capital, 2), "status": RUNNING,
                  "max_loss_pct": max_loss_pct, "take_profit_pct": take_profit_pct,
                  "started_at": prev.get("started_at") if again else now,
                  "realized": prev.get("realized", 0.0) if again else 0.0,  # $ gained/lost on sells
                  "updated_at": now}
    _write(path, runs)
    return runs[name]


def stop(path: Path, name: str, reason: str = "stopped by you") -> None:
    runs = load(path)
    if name not in runs or runs[name].get("status") == STOPPED:
        raise ValueError("that strategy is not running")
    runs[name].update(status=STOPPING, reason=reason, updated_at=datetime.now(timezone.utc).isoformat())
    _write(path, runs)


def add_realized(path: Path, name: str, amount: float) -> None:
    runs = load(path)
    if name in runs:
        runs[name]["realized"] = round(float(runs[name].get("realized", 0.0)) + amount, 2)
        _write(path, runs)


def mark_stopped(path: Path, name: str) -> None:
    runs = load(path)
    if name in runs:
        runs[name].update(status=STOPPED, stopped_at=datetime.now(timezone.utc).isoformat())
        _write(path, runs)


def pick(perf: dict[str, Any], names: list[str], budget: float, top: int = 5) -> dict[str, float]:
    """Auto-invest: split budget evenly over the `top` names with the best simulated year
    (return per unit of worst drop), skipping any that lost money or have no record."""
    def score(name: str) -> float | None:
        w = (perf.get(name) or {}).get("windows") or {}
        w = w.get("1Y") or w.get("6M")
        return w["return"] / max(abs(w["worst_drop"]), 0.05) if w and w["return"] > 0 else None

    best = sorted((n for n in names if score(n) is not None), key=score, reverse=True)[:top]
    best = best[:max(1, int(budget // 10))]  # every slice must be at least $10
    return {n: int(budget / len(best) * 100) / 100 for n in best}  # floor: slices never sum past budget if best and budget >= 10 else {}
