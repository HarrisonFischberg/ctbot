"""Single-name mean reversion: buy sharp dips (low z-score) with a short half-life."""

from __future__ import annotations

import numpy as np

from ctbot.analysis import stats
from ctbot.models import Direction, EventKind, Signal
from ctbot.strategies.base import StrategyContext


class MeanReversionStrategy:
    name = "mean_reversion"
    event_kinds: frozenset[EventKind] = frozenset()

    def __init__(
        self,
        lookback_days: int = 120,
        z_window: int = 20,
        z_entry: float = 2.0,
        z_exit: float = 0.5,
        max_half_life_days: float = 20.0,
        trend_sma_days: int = 100,
    ) -> None:
        self.lookback_days = lookback_days
        self.z_window = z_window
        self.z_entry = z_entry
        self.z_exit = z_exit
        self.max_half_life_days = max_half_life_days
        self.trend_sma_days = trend_sma_days

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        out: list[Signal] = []
        for sym in ctx.universe:
            c = np.array(
                [b.close for b in ctx.market_data.daily_bars(sym, self.lookback_days)], dtype=float
            )
            if len(c) < max(60, self.trend_sma_days):
                continue
            lp = stats.log_prices(c)
            z = stats.zscore(lp, window=self.z_window)

            if sym in ctx.held_symbols and z >= -self.z_exit:
                out.append(Signal(self.name, sym, Direction.EXIT, 0.6, 1.0, {"zscore": round(z, 3)}))
                continue
            if z > -self.z_entry:
                continue
            # Only buy dips in names still above their long trend.
            if c[-1] < stats.sma(c, self.trend_sma_days) * 0.9:
                continue
            hl = stats.half_life(lp[-60:])
            if hl > self.max_half_life_days:
                continue
            out.append(
                Signal(
                    strategy=self.name,
                    symbol=sym,
                    direction=Direction.LONG,
                    strength=min(1.0, abs(z) / (2 * self.z_entry)),
                    horizon_days=hl,
                    evidence={"zscore": round(z, 3), "half_life_days": round(hl, 2)},
                )
            )
        return out
