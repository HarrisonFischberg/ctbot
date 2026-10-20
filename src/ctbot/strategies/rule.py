"""A strategy-library entry, live: one entry rule (ctbot.library.signals) on one universe.

Decides once a day in the 09:35-10:00 ET window on yesterday's completed daily bars, exactly what
`ctbot-library-research` backtests (signal at a close, buy at the next open). Each idea carries
the shared exit plan: a starting stop 2x the 14-day average range below the price (capped at 8%)
and the family's longest hold; under the autopilot the stop then trails. Claude reviews each
entry. Ships watch-only: the autopilot trades an entry only after the backtest admits it.
"""

from __future__ import annotations

import logging
from datetime import time

import pandas as pd

from ctbot.library import backtest as lbt
from ctbot.library import signals as sg
from ctbot.market_hours import ET
from ctbot.models import Direction, EventKind, Signal
from ctbot.strategies.base import StrategyContext
from ctbot.strategies.swing import _add_bdays, _at

log = logging.getLogger(__name__)
FIELDS = ("open", "high", "low", "close", "volume")


def _frame(bars) -> pd.DataFrame:
    return pd.DataFrame({f: [getattr(b, f) for b in bars] for f in FIELDS},
                        index=pd.DatetimeIndex([pd.Timestamp(b.ts.date()) for b in bars]))


class RuleStrategy:
    event_kinds: frozenset[EventKind] = frozenset()

    def __init__(self, family: str, universe: str, name: str | None = None) -> None:
        self.family = sg.FAMILIES[family]
        self.universe_key = universe
        self.symbols = sg.universe(universe)
        self.name = name or sg.entry_name(family, universe)
        self._done = None

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        if ctx.now is None or self._done == ctx.today:
            return []
        if not time(9, 35) <= ctx.now.astimezone(ET).time() <= time(10, 0):
            return []
        self._done = ctx.today
        frames = {}
        for sym in [*self.symbols, "SPY"]:
            if sym != "SPY" and sym not in ctx.universe:
                continue
            try:
                bars = [b for b in ctx.market_data.daily_bars(sym, 300) if b.ts.date() < ctx.today]
            except Exception:  # noqa: BLE001
                continue
            if len(bars) >= 260:
                frames[sym] = _frame(bars)
        spy = frames.get("SPY")
        if spy is None:
            return []
        hits = []
        for sym, df in frames.items():
            if sym == "SPY" and "SPY" not in self.symbols:
                continue
            if sym in ctx.held_symbols or not bool(self.family.rule(df, spy).iloc[-1]):
                continue
            a = float(sg.atr(df).iloc[-1])
            mom = float(df["close"].iloc[-1] / df["close"].iloc[-64] - 1)
            hits.append((mom, sym, a, float(df["close"].iloc[-1])))
        if not hits:
            return []
        hits.sort(reverse=True)  # same ranking as the backtest: strongest 3-month return first
        quotes = ctx.market_data.quotes([h[1] for h in hits[: lbt.MAX_POS]])
        exit_at = _at(_add_bdays(ctx.today, self.family.max_hold), 15, 50)
        out = []
        for mom, sym, a, close in hits[: lbt.MAX_POS]:
            q = quotes.get(sym)
            if not q or not q.last:
                continue
            dist = min(lbt.ATR_MULT * a / close, lbt.MAX_LOSS)
            out.append(Signal(self.name, sym, Direction.LONG, 0.6, float(self.family.max_hold), {
                "family": self.family.key, "rule": self.family.tagline, "source": self.family.source,
                "ref_price": round(q.last, 4), "stop": round(q.last * (1 - dist), 4), "target": None,
                "exit_at": exit_at, "size_pct": lbt.SLOT, "momentum_3m": round(mom, 4),
                "decided_on_bars_through": str(frames[sym].index[-1].date()),
            }, ts=ctx.now))
        return out
