"""Track-record sizing: a strategy's reviewed entries grow or shrink with its recent results.

Each closed round trip in the paper ledger (FIFO per symbol, `fifo` below)
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


def models(fill: dict[str, Any]) -> list[str]:
    """Strategies named in a buy's tag ("a,b" when two bought together); ["untagged"] if none."""
    return [m.strip() for m in (fill.get("tag") or "").split(",") if m.strip()] or ["untagged"]


def fifo(fills: list[dict[str, Any]]) -> tuple[list[tuple[dict, dict, float, bool]], dict[str, list[list]]]:
    """Sells matched to the buys they close, first in first out per symbol. Returns
    ([(buy, sell, quantity, buy fully closed), ...] in time order, {symbol: [[quantity left, buy], ...]})."""
    lots: dict[str, list[list]] = {}
    pieces = []
    for f in sorted(fills, key=lambda f: f["ts"]):
        qty = float(f["quantity"])
        if f["side"] == "buy":
            lots.setdefault(f["symbol"], []).append([qty, f])
            continue
        q = lots.get(f["symbol"], [])
        while qty > 1e-9 and q:
            take = min(qty, q[0][0])
            q[0][0] -= take
            qty -= take
            closed = q[0][0] <= 1e-9
            pieces.append((q[0][1], f, take, closed))
            if closed:
                q.pop(0)
    return pieces, lots


def returns_by_model(fills: list[dict[str, Any]], since: datetime | None = None) -> dict[str, list[float]]:
    """model -> returns of its round trips closed at or after `since`."""
    out: dict[str, list[float]] = {}
    for buy, sell, _, closed in fifo(fills)[0]:
        bpx = float(buy["price"])
        if closed and bpx > 0 and (since is None or datetime.fromisoformat(sell["ts"]) >= since):
            for m in models(buy):
                out.setdefault(m, []).append(float(sell["price"]) / bpx - 1)
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
