"""Backtest vs live: is each strategy's paper record in line with what its backtest promised?

config/backtests.toml holds each strategy's backtested mean return per trade after costs.
The live side is the strategy's closed paper round trips (track_record.returns_by_model).
With fewer than `min_trades` it is too early to say. Otherwise the live mean is compared
with the backtest mean using the live trades' standard error: more than two standard errors
below is "below backtest" (the edge may not be real, or costs and fills differ), more than
two above is "above backtest", anything in between is "in line".
"""

from __future__ import annotations

import math
import statistics
import tomllib
from pathlib import Path
from typing import Any

MIN_TRADES = 10


def load(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict) and isinstance(v.get("avg_ret"), (int, float))}


def verdict(rets: list[float], expected: dict[str, Any] | None, min_trades: int = MIN_TRADES) -> dict[str, Any]:
    n = len(rets)
    live = statistics.fmean(rets) if rets else None
    if not expected:
        return {"status": "none", "n": n, "live_avg": live}
    out = {"n": n, "live_avg": live, "bt_avg": float(expected["avg_ret"]),
           "bt_win_rate": expected.get("win_rate"), "source": expected.get("source")}
    if n < min_trades:
        return {**out, "status": "too_early"}
    se = statistics.stdev(rets) / math.sqrt(n)
    gap = live - out["bt_avg"]
    status = "below" if gap < -2 * se else "above" if gap > 2 * se else "in_line"
    return {**out, "status": status, "gap": gap, "se": se}


def by_model(fills: list[dict[str, Any]], expectations: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """strategy -> verdict, for every strategy with closed trades or a backtest on file."""
    from ctbot.track_record import returns_by_model

    rets = returns_by_model(fills)
    return {m: verdict(rets.get(m, []), expectations.get(m)) for m in sorted(set(rets) | set(expectations))}
