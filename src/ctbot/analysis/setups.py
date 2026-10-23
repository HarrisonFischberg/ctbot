"""Daily chart setups on completed closes (the day-trader pack's trader-scanner rules).

Five tags, each a plain rule on daily closes only, so they work on any daily series:

| tag             | rule                                                                     |
|-----------------|--------------------------------------------------------------------------|
| breakout_20d    | the last close is above every close of the 20 sessions before it          |
| bull_flag       | up more than 10% over the 10 sessions before the last 5, then the last 5 |
|                 | closes inside a 5% range                                                  |
| reclaim_ma20    | the close before was under its 20-day average, the last close is above it |
| oversold        | RSI 14 (Wilder) under 30                                                  |
| oversold_bounce | RSI 14 was under 30 the session before, and the last close is up          |

A tag means a chart matched a rule, not that it is a buy. They are context for Claude's review
and for the premarket plan; no strategy trades on them.

Source: github.com/Jakeschincariol/day-trader-skill (MIT), trader-scanner/scan.py.
"""

from __future__ import annotations

from typing import Any

MIN_CLOSES = 30
TAGS = ("breakout_20d", "bull_flag", "reclaim_ma20", "oversold", "oversold_bounce")


def wilder_rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI over the whole series (seeded with the first n changes)."""
    if len(closes) < n + 1:
        return None
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def find(closes: list[float]) -> dict[str, Any] | None:
    """Tags and the numbers behind them for completed daily closes (oldest first); None when
    there are fewer than MIN_CLOSES."""
    c = [float(x) for x in closes]
    if len(c) < MIN_CLOSES or not c[-1]:
        return None
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20, ma20_prev = sum(c[-20:]) / 20, sum(c[-21:-1]) / 20
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r, r_prev = wilder_rsi(c), wilder_rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return {"tags": tags, "rsi14": round(r, 1) if r is not None else None,
            "vs_20d_high": round(last / hi20 - 1, 4), "vs_ma20": round(last / ma20 - 1, 4)}


def for_symbol(md, symbol: str, lookback: int = 60) -> dict[str, Any] | None:
    """`find` on a market-data provider's completed daily bars; None on missing data."""
    try:
        bars = md.daily_bars(symbol, lookback)
    except Exception:  # noqa: BLE001 - context only
        return None
    return find([b.close for b in bars])
