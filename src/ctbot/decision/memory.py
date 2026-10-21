"""Outcome memory for reviews: what this bot's own models actually did, as of a given time.

Borrowed idea: TradingAgents' decision log + settled outcomes fed back to its portfolio manager
(memory/log.py, settlement.py). Differences: outcomes here are filled paper trades from the
ledger (not hypothetical moves), the summary is computed by code (no LLM reflection), and only
fills and idea scores dated before `as_of` are used, so a replay never sees its own future.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _fills(ledger_path: Path, as_of: datetime) -> list[dict[str, Any]]:
    try:
        data = json.loads(ledger_path.read_text())
    except (OSError, ValueError):
        return []
    return [f for f in data.get("fills", []) if datetime.fromisoformat(f["ts"]) < as_of]


def outcome_memory(ledger_path: Path, models: list[str], symbol: str, as_of: datetime,
                   idea_scores_path: Path | None = None, last_n: int = 5) -> dict[str, Any]:
    from ctbot.dashboard.state import model_stats

    fills = _fills(ledger_path, as_of)
    stats = {s["model"]: s for s in model_stats(fills, {})}
    out: dict[str, Any] = {"as_of": as_of.isoformat(timespec="seconds"), "models": {}}
    for m in models:
        rows = [s for name, s in stats.items() if m in name.split(",")]
        closed = sum(s["closed"] for s in rows)
        wins = sum(s["wins"] for s in rows)
        avg = [s["avg_ret"] for s in rows if s["avg_ret"] is not None]
        out["models"][m] = {"closed_trades": closed, "win_rate": round(wins / closed, 3) if closed else None,
                            "avg_return": round(sum(avg) / len(avg), 5) if avg else None,
                            "realized_pnl": round(sum(s["pnl"] for s in rows), 2)}
    # last round trips in this symbol (FIFO on this symbol's fills only)
    lots, trips = [], []
    for f in sorted((f for f in fills if f["symbol"] == symbol), key=lambda f: f["ts"]):
        if f["side"] == "buy":
            lots.append([float(f["quantity"]), float(f["price"]), f.get("tag")])
        elif lots:
            qty, px, tag = lots.pop(0)
            trips.append({"model": tag, "return": round(float(f["price"]) / px - 1, 5), "closed": f["ts"][:10],
                          "exit": f.get("tag")})
    out["symbol_last_trades"] = trips[-last_n:]
    if idea_scores_path is not None:
        try:
            scores = json.loads(idea_scores_path.read_text())
            gen = scores.get("generated_at")
            if gen is None or datetime.fromisoformat(gen) < as_of:
                summ = scores.get("summary", {})
                out["idea_replay"] = {m: summ[m] for m in models if m in summ}
        except (OSError, ValueError, TypeError):
            pass
    return out
