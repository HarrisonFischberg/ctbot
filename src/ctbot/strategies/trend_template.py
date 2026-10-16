"""Stage-2 trend template: long names in a confirmed, rising long-term uptrend.

Port of maverick-mcp's supply/demand breakout screen (MIT, wshobson/maverick-mcp), itself a
Minervini-style trend template. A name qualifies only when ALL hold: close above its 50-,
150- and 200-day averages, 50 > 150 > 200, and the 200-day average higher than 22 bars ago.
Breakout strength is 50, +25 when volume is above 1.2x its 30-day average, +25 when close is
within 25% of the 252-day high; strength = breakout / 100. Leaders are ranked by strength,
then by distance above the 200-day average. Held names that close below the 50-day average
get an exit signal.

Not standalone by default: it confirms other models (an ensemble vote), it does not open
trades alone.
"""

from __future__ import annotations

import numpy as np

from ctbot.analysis import stats
from ctbot.models import Bar, Direction, EventKind, Signal
from ctbot.strategies.base import StrategyContext

NEED_BARS = 200 + 22


def evaluate(bars: list[Bar], volume_surge: float = 1.2, near_high: float = 0.75) -> dict | None:
    """Gate + score for one symbol's completed daily bars (oldest first); None if it fails."""
    if len(bars) < NEED_BARS:
        return None
    c = np.array([b.close for b in bars], dtype=float)
    v = np.array([b.volume for b in bars], dtype=float)
    h = np.array([b.high for b in bars], dtype=float)
    close = c[-1]
    sma50, sma150, sma200 = stats.sma(c, 50), stats.sma(c, 150), stats.sma(c, 200)
    sma200_22_ago = stats.sma(c[:-22], 200)
    gate = {
        "close_above_sma50": close > sma50,
        "close_above_sma150": close > sma150,
        "close_above_sma200": close > sma200,
        "sma50_above_sma150": sma50 > sma150,
        "sma150_above_sma200": sma150 > sma200,
        "sma200_rising": sma200 > sma200_22_ago,
    }
    if not all(gate.values()):
        return None
    surge = bool(v[-1] > volume_surge * v[-30:].mean())
    high_252 = float(h[-252:].max())
    near = bool(close > high_252 * near_high)
    breakout = 50 + 25 * surge + 25 * near
    return {
        "breakout_strength": breakout,
        "volume_surge": surge,
        "within_25pct_of_52w_high": near,
        "above_sma200": round(float(close / sma200 - 1), 4),
        "below_52w_high": round(float(1 - close / high_252), 4),
        "sma50": round(sma50, 2),
        "sma200": round(sma200, 2),
    }


class TrendTemplate:
    name = "trend_template"
    event_kinds: frozenset[EventKind] = frozenset()

    def __init__(
        self,
        min_breakout: int = 75,
        top_n: int = 5,
        volume_surge: float = 1.2,
        near_high: float = 0.75,
        horizon_days: float = 20.0,
    ) -> None:
        self.min_breakout = min_breakout
        self.top_n = top_n
        self.volume_surge = volume_surge
        self.near_high = near_high
        self.horizon_days = horizon_days

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        scored: list[tuple[str, dict]] = []
        for sym in ctx.universe:
            ev = evaluate(ctx.market_data.daily_bars(sym, 260), self.volume_surge, self.near_high)
            if ev and ev["breakout_strength"] >= self.min_breakout:
                scored.append((sym, ev))
        scored.sort(key=lambda t: (t[1]["breakout_strength"], t[1]["above_sma200"]), reverse=True)
        leaders = scored[: self.top_n]
        out = [
            Signal(self.name, sym, Direction.LONG, ev["breakout_strength"] / 100, self.horizon_days, ev)
            for sym, ev in leaders
        ]
        leader_syms = {s for s, _ in leaders}
        for sym in ctx.held_symbols - leader_syms:
            c = np.array([b.close for b in ctx.market_data.daily_bars(sym, 60)], dtype=float)
            if len(c) >= 50 and c[-1] < stats.sma(c, 50):
                out.append(Signal(self.name, sym, Direction.EXIT, 0.5, self.horizon_days,
                                  {"reason": "close_below_sma50"}))
        return out
