"""Track-record sizing: a strategy's reviewed entries grow or shrink with its recent results.

Each closed round trip in the paper ledger (FIFO per symbol, like dashboard.state.model_stats)
is credited to every strategy named in its buy's tag. A strategy's average return per trade is
pulled toward zero by `prior` imaginary flat trades, so a handful of trades barely moves it and
one trade can never move it much: with prior=10, one -6% trade shifts it by at most 0.6%.

    multiplier = clamp(1 + SLOPE x shrunk average return, min_mult, max_mult)

With SLOPE=25, a shrunk average of -2% per trade halves the size and +1% adds a quarter. A
strategy with no closed trades sizes at 1x. The autopilot's bench (10+ trades, net loss) still
applies on top. Pure functions; the pipeline sets RiskManager.size_mult each cycle.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

SLOPE = 25.0
WINDOW_DAYS = 30


def returns_by_model(fills: list[dict[str, Any]], since: datetime | None = None) -> dict[str, list[float]]:
    """model -> returns of its round trips closed at or after `since`."""
    lots: dict[str, list[list]] = {}  # symbol -> [[qty, price, models], ...]
    out: dict[str, list[float]] = {}
    for f in sorted(fills, key=lambda f: f["ts"]):
        sym, qty, px = f["symbol"], float(f["quantity"]), float(f["price"])
        if f["side"] == "buy":
            models = [m.strip() for m in (f.get("tag") or "").split(",") if m.strip()]
            lots.setdefault(sym, []).append([qty, px, models])
            continue
        recent = since is None or datetime.fromisoformat(f["ts"]) >= since
        q = lots.get(sym, [])
        while qty > 1e-9 and q:
            lot = q[0]
            take = min(qty, lot[0])
            if take >= lot[0] - 1e-9 and recent and lot[1] > 0:  # lot fully closed: one trade
                for m in lot[2]:
                    out.setdefault(m, []).append(px / lot[1] - 1)
            lot[0] -= take
            qty -= take
            if lot[0] <= 1e-9:
                q.pop(0)
    return out


def multiplier(rets: list[float], prior: int, lo: float, hi: float) -> float:
    shrunk = sum(rets) / (len(rets) + max(prior, 0)) if rets or prior else 0.0
    return round(min(hi, max(lo, 1.0 + SLOPE * shrunk)), 3)


def size_multipliers(fills: list[dict[str, Any]], now: datetime, prior: int = 10,
                     lo: float = 0.25, hi: float = 1.25) -> dict[str, dict[str, Any]]:
    """model -> {"mult", "trades", "avg_ret"} from the last WINDOW_DAYS of closed trades."""
    out = {}
    for m, rets in returns_by_model(fills, now - timedelta(days=WINDOW_DAYS)).items():
        out[m] = {"mult": multiplier(rets, prior, lo, hi), "trades": len(rets),
                  "avg_ret": round(sum(rets) / len(rets), 5)}
    return out


def for_candidate(mults: dict[str, float], models) -> float:
    """The most cautious multiplier among the strategies buying (1.0 for ones with no record)."""
    return min((mults.get(m, 1.0) for m in models), default=1.0)
