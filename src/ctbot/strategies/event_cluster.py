"""Generic "follow the informed actors" strategy over AltDataEvents.

Congressional, insider/institutional and copy-trading strategies are all
configurations of this: count distinct actors buying (or selling) a symbol inside
a recent window, discard stale disclosures, and scale strength by breadth.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from ctbot.models import AltDataEvent, Direction, EventKind, Side, Signal
from ctbot.strategies.base import StrategyContext


class EventClusterStrategy:
    name = "event_cluster"
    event_kinds: frozenset[EventKind] = frozenset()

    def __init__(
        self,
        window_days: int = 30,
        max_disclosure_lag_days: int = 45,
        min_actors: int = 2,
        saturation_actors: int = 5,
        min_size_usd: float = 0.0,
        horizon_days: float = 30.0,
        tracked_actors: list[str] | None = None,
        restrict_to_universe: bool = False,
    ) -> None:
        self.window_days = window_days
        self.max_disclosure_lag_days = max_disclosure_lag_days
        self.min_actors = min_actors
        self.saturation_actors = saturation_actors
        self.min_size_usd = min_size_usd
        self.horizon_days = horizon_days
        # None = no actor filter; an empty list = follow nobody.
        self.tracked_actors = (
            {a.lower() for a in tracked_actors} if tracked_actors is not None else None
        )
        self.restrict_to_universe = restrict_to_universe

    def _eligible(self, ev: AltDataEvent, ctx: StrategyContext) -> bool:
        if ev.kind not in self.event_kinds:
            return False
        if ev.disclosed_date < ctx.today - timedelta(days=self.window_days):
            return False
        if ev.disclosure_lag_days > self.max_disclosure_lag_days:
            return False
        if ev.size_usd is not None and ev.size_usd < self.min_size_usd:
            return False
        if self.tracked_actors is not None and ev.actor.lower() not in self.tracked_actors:
            return False
        if self.restrict_to_universe and ev.symbol not in ctx.universe:
            return False
        return True

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        buyers: dict[str, set[str]] = defaultdict(set)
        sellers: dict[str, set[str]] = defaultdict(set)
        dollars: dict[str, float] = defaultdict(float)
        for ev in ctx.events:
            if not self._eligible(ev, ctx):
                continue
            (buyers if ev.side is Side.BUY else sellers)[ev.symbol].add(ev.actor)
            sign = 1 if ev.side is Side.BUY else -1
            dollars[ev.symbol] += sign * (ev.size_usd or 0.0)

        out: list[Signal] = []
        for sym in set(buyers) | set(sellers):
            net = len(buyers[sym]) - len(sellers[sym])
            if abs(net) < self.min_actors:
                continue
            if net < 0 and sym not in ctx.held_symbols:
                continue  # bearish clusters only matter for positions we hold
            out.append(
                Signal(
                    strategy=self.name,
                    symbol=sym,
                    direction=Direction.LONG if net > 0 else Direction.EXIT,
                    strength=min(1.0, abs(net) / self.saturation_actors),
                    horizon_days=self.horizon_days,
                    evidence={
                        "buyers": sorted(buyers[sym]),
                        "sellers": sorted(sellers[sym]),
                        "net_dollars": round(dollars[sym], 2),
                    },
                )
            )
        return out


class CongressionalStrategy(EventClusterStrategy):
    name = "congressional"
    event_kinds = frozenset({EventKind.CONGRESS_TRADE})


class InstitutionalStrategy(EventClusterStrategy):
    """Open-market insider buys (Form 4) plus 13F position increases."""

    name = "institutional"
    event_kinds = frozenset({EventKind.INSIDER_TRADE, EventKind.INSTITUTIONAL_HOLDING})


class CopyTradingStrategy(EventClusterStrategy):
    """Mirror a configured list of investors; one tracked actor is enough by default.

    Reads only INVESTOR_TRADE so 13F events are not double-counted with `institutional`.
    """

    name = "copy_trading"
    event_kinds = frozenset({EventKind.INVESTOR_TRADE})

    def __init__(self, tracked_actors: list[str] | None = None, min_actors: int = 1, **kw) -> None:
        super().__init__(tracked_actors=tracked_actors or [], min_actors=min_actors, **kw)
