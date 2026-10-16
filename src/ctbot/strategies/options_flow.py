"""Unusual options activity: net bullish vs bearish premium per symbol.

Providers normalize each flow print so side=BUY means bullish (calls bought / puts
sold) and size_usd is the premium. Optional raw keys: is_sweep, expiry, open_interest.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from ctbot.models import Direction, EventKind, Side, Signal
from ctbot.strategies.base import StrategyContext


class OptionsFlowStrategy:
    name = "options_flow"
    event_kinds = frozenset({EventKind.OPTIONS_FLOW})

    def __init__(
        self,
        window_days: int = 3,
        min_print_premium_usd: float = 100_000,
        min_net_premium_usd: float = 500_000,
        saturation_premium_usd: float = 5_000_000,
        min_prints: int = 2,
        max_days_to_expiry: int = 60,
        sweeps_only: bool = False,
        horizon_days: float = 10.0,
    ) -> None:
        self.window_days = window_days
        self.min_print_premium_usd = min_print_premium_usd
        self.min_net_premium_usd = min_net_premium_usd
        self.saturation_premium_usd = saturation_premium_usd
        self.min_prints = min_prints
        self.max_days_to_expiry = max_days_to_expiry
        self.sweeps_only = sweeps_only
        self.horizon_days = horizon_days

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        net: dict[str, float] = defaultdict(float)
        prints: dict[str, int] = defaultdict(int)
        cutoff = ctx.today - timedelta(days=self.window_days)
        for ev in ctx.events:
            if ev.kind is not EventKind.OPTIONS_FLOW or ev.disclosed_date < cutoff:
                continue
            premium = ev.size_usd or 0.0
            if premium < self.min_print_premium_usd:
                continue
            if self.sweeps_only and not ev.raw.get("is_sweep"):
                continue
            expiry = ev.raw.get("expiry")
            if expiry and (date.fromisoformat(expiry) - ctx.today).days > self.max_days_to_expiry:
                continue
            net[ev.symbol] += premium if ev.side is Side.BUY else -premium
            prints[ev.symbol] += 1

        out: list[Signal] = []
        for sym, p in net.items():
            if prints[sym] < self.min_prints or abs(p) < self.min_net_premium_usd:
                continue
            if p < 0 and sym not in ctx.held_symbols:
                continue
            out.append(
                Signal(
                    strategy=self.name,
                    symbol=sym,
                    direction=Direction.LONG if p > 0 else Direction.EXIT,
                    strength=min(1.0, abs(p) / self.saturation_premium_usd),
                    horizon_days=self.horizon_days,
                    evidence={"net_premium_usd": round(p, 2), "prints": prints[sym]},
                )
            )
        return out
