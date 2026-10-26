"""Autopilot: CTbot picks the trades from its whole strategy library, paper only.

You set two numbers on the dashboard's Autopilot page (saved in data/autopilot.json, read every
cycle):
* max loss per trade: the furthest a new position's starting stop may sit below its fill;
* give-back: how far below a position's highest price since entry the stop trails.

With autopilot on, every enabled model may open trades on its own (day trades, overnight holds,
multi-day swings and the trend cores), except models that research rejected or that are losing in
paper. Claude reviews each entry from a non-systematic model and proposes the starting stop;
code sizes the trade and enforces every risk limit. Positions from Claude-reviewed models carry
no fixed profit target: the stop ratchets up behind the price (never down) and sells when the
price falls back through it. Systematic models (the trend cores) keep their own backtested exits.

Benching is computed from the ledger's own fills over the last BENCH_WINDOW_DAYS, so a benched
model gets another chance once its losing trades age out of the window.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Rejected by research: never given money by the autopilot (reason shown on the page).
EXCLUDED = {
    "ranker": "failed its test on real prices (2026-10-20): it did no better than buying every stock equally, and its best results looked like luck",
}
MIN_TRADES_TO_BENCH = 10
BENCH_WINDOW_DAYS = 30
# With a capped loss and a trailing exit, a trade pays when its expected value is positive, not when it
# is more likely than not to win (trend entries win ~40% of the time). While the autopilot is on,
# Claude's approval is the gate and its win probability only has to clear this floor instead of
# MIN_CLAUDE_CONFIDENCE (Harold's choice, 2026-10-20). Settable per book in autopilot.json.
MIN_CONFIDENCE = 0.30
LIMITS = {"max_loss_pct": (0.01, 0.25), "giveback_pct": (0.01, 0.30), "min_confidence": (0.10, 0.90)}


@dataclass(frozen=True)
class AutopilotConfig:
    enabled: bool = False
    max_loss_pct: float = 0.05
    giveback_pct: float = 0.08
    min_confidence: float = MIN_CONFIDENCE


def load(path: Path) -> AutopilotConfig:
    try:
        raw = json.loads(path.read_text())
        return validate(raw)
    except (OSError, ValueError, TypeError):
        return AutopilotConfig()


def validate(raw: dict[str, Any]) -> AutopilotConfig:
    """Raises ValueError for anything outside the allowed ranges."""
    base = AutopilotConfig()
    out: dict[str, Any] = {"enabled": bool(raw.get("enabled", base.enabled))}
    for key, (lo, hi) in LIMITS.items():
        v = float(raw.get(key, getattr(base, key)))
        if not lo <= v <= hi:
            raise ValueError(f"{key} must be between {lo:.0%} and {hi:.0%}")
        out[key] = round(v, 4)
    return AutopilotConfig(**out)


def save(path: Path, cfg: AutopilotConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**asdict(cfg), "updated": datetime.now().astimezone().isoformat(timespec="seconds")}))
    os.replace(tmp, path)


def decide(models: list[str], fills: list[dict[str, Any]], now: datetime,
           library: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """model -> {"status": "trading" | "benched", "reason", "closed", "pnl", "win_rate"} from the
    paper record of the last BENCH_WINDOW_DAYS. Strategy-library entries (lib_*) also need the
    library backtest's admission (data/research/library_report.json)."""
    from ctbot.dashboard.state import model_stats
    from ctbot.library import backtest as lbt

    if library is None and any(m.startswith("lib_") for m in models):
        library = lbt.load_report()

    cutoff = now - timedelta(days=BENCH_WINDOW_DAYS)
    recent = [f for f in fills if datetime.fromisoformat(f["ts"]) >= cutoff]
    stats = {s["model"]: s for s in model_stats(recent, {})}
    out: dict[str, dict[str, Any]] = {}
    for m in models:
        s = stats.get(m, {})
        closed, pnl = int(s.get("closed", 0)), float(s.get("pnl", 0.0))
        rec = {"closed": closed, "pnl": round(pnl, 2), "win_rate": (s.get("wins", 0) / closed) if closed else None}
        if m in EXCLUDED:
            out[m] = {**rec, "status": "benched", "reason": EXCLUDED[m]}
        elif m.startswith("lib_") and not (verdict := lbt.reason(library, m))[0]:
            out[m] = {**rec, "status": "benched", "reason": verdict[1]}
        elif closed >= MIN_TRADES_TO_BENCH and pnl < 0:
            out[m] = {**rec, "status": "benched",
                      "reason": f"lost ${-pnl:,.2f} over its last {closed} practice trades ({BENCH_WINDOW_DAYS} days)"}
        else:
            why = (f"{closed} practice trades in the last {BENCH_WINDOW_DAYS} days, {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f} in total"
                   if closed else "no practice trades lately: it's trading to build a record")
            out[m] = {**rec, "status": "trading", "reason": why}
    return out


def apply(c, state, cfg: AutopilotConfig, fills: list[dict[str, Any]], now: datetime,
          invested: frozenset[str] = frozenset()) -> None:
    """Called by the pipeline each cycle while autopilot is on: every model that isn't benched
    trades on its own, and so does every launcher run (sized off its own capital by the
    pipeline's sleeves); the risk manager trails stops."""
    if c.configured is None:
        c.configured = list(c.strategies)
    verdicts = decide([ws.strategy.name for ws in c.configured], fills, now)
    trading = frozenset(m for m, v in verdicts.items() if v["status"] == "trading") | invested
    key = ("autopilot", trading)
    if c.launched != key:
        c.strategies = [replace(ws, mode="trade", standalone=True) if ws.strategy.name in trading
                        else replace(ws, mode="shadow") for ws in c.configured]
        c.launched = key
        log.info("autopilot: trading %s; benched %s", ", ".join(sorted(trading)) or "none",
                 ", ".join(f"{m} ({v['reason']})" for m, v in verdicts.items() if v["status"] == "benched") or "none")
    c.risk.max_loss_pct = cfg.max_loss_pct
    c.risk.trail_giveback = cfg.giveback_pct
    c.risk.min_confidence = cfg.min_confidence
