"""Cross-sectional momentum: rank universe by skip-adjusted return, long the leaders in uptrends."""

from __future__ import annotations

import numpy as np

from ctbot.analysis import stats
from ctbot.models import Direction, EventKind, Signal
from ctbot.strategies.base import StrategyContext


class MomentumStrategy:
    name = "momentum"
    event_kinds: frozenset[EventKind] = frozenset()

    def __init__(
        self,
        lookback_days: int = 120,
        skip_days: int = 5,
        trend_sma_days: int = 50,
        top_n: int = 3,
        min_return: float = 0.05,
        horizon_days: float = 20.0,
    ) -> None:
        self.lookback_days = lookback_days
        self.skip_days = skip_days
        self.trend_sma_days = trend_sma_days
        self.top_n = top_n
        self.min_return = min_return
        self.horizon_days = horizon_days

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        need = self.lookback_days + self.skip_days + 1
        scored: list[tuple[str, float, float]] = []
        for sym in ctx.universe:
            c = np.array([b.close for b in ctx.market_data.daily_bars(sym, need)], dtype=float)
            if len(c) < need:
                continue
            ret = stats.total_return(c, self.lookback_days, self.skip_days)
            if np.isnan(ret) or ret < self.min_return:
                continue
            if c[-1] < stats.sma(c, self.trend_sma_days):
                continue
            vol = stats.realized_vol(c)
            scored.append((sym, ret, ret / vol if vol and not np.isnan(vol) else ret))

        scored.sort(key=lambda t: t[2], reverse=True)
        leaders = scored[: self.top_n]
        out: list[Signal] = []
        for rank, (sym, ret, risk_adj) in enumerate(leaders):
            out.append(
                Signal(
                    strategy=self.name,
                    symbol=sym,
                    direction=Direction.LONG,
                    strength=1.0 - rank / max(1, len(leaders)),
                    horizon_days=self.horizon_days,
                    evidence={"return": round(ret, 4), "return_per_vol": round(risk_adj, 3)},
                )
            )
        # Held names that fell out of trend -> exit signal.
        leader_syms = {s for s, _, _ in leaders}
        for sym in ctx.held_symbols - leader_syms:
            c = np.array([b.close for b in ctx.market_data.daily_bars(sym, need)], dtype=float)
            if len(c) >= self.trend_sma_days and c[-1] < stats.sma(c, self.trend_sma_days):
                out.append(
                    Signal(self.name, sym, Direction.EXIT, 0.5, self.horizon_days,
                           {"reason": "below_trend_sma"})
                )
        return out
