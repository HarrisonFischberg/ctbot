"""Opening-range breakout on market-wide Stocks in Play (Zarattini, Barbon & Aziz 2024), long side.

Each morning the scanner picks the most unusually active Russell 1000 names. For each whose
first 5-minute bar closed green, buy when price trades through that bar's high; stop
0.10 x ATR(14) below the entry (the paper's rule); exit at 15:50 ET. Entries 9:40-11:00 ET.

Our own 88-large-cap backtest of this rule was negative out-of-sample (the paper's edge came
from a ~7,000-stock universe); this model tests the market-wide version forward on paper with
a fixed sleeve (size_pct of equity per trade).
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from ctbot import market_hours
from ctbot.market_hours import ET
from ctbot.models import Direction, EventKind, Signal
from ctbot.strategies.base import StrategyContext

log = logging.getLogger(__name__)
BAR = timedelta(minutes=5)


class OrbStocksInPlay:
    name = "orb_sip"
    event_kinds: frozenset[EventKind] = frozenset()

    def __init__(self, stop_atr: float = 0.10, size_pct: float = 0.20, top_k: int = 20,
                 last_entry: str = "11:00", scanner=None) -> None:
        self.stop_atr, self.size_pct, self.top_k = stop_atr, size_pct, top_k
        hh, mm = (int(x) for x in last_entry.split(":"))
        self.last_entry = time(hh, mm)
        self.scanner = scanner  # injected by the paper entrypoint (needs the Robinhood client)

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        md = ctx.market_data
        if self.scanner is None or ctx.now is None or not hasattr(md, "today_bars"):
            return []
        et = ctx.now.astimezone(ET).time()
        if not market_hours.session_open(ctx.now) or not (time(9, 40) <= et <= self.last_entry):
            return []
        rows = self.scanner.today()[: self.top_k]
        if not rows:
            return []
        atr = {r["symbol"]: r["atr14"] for r in rows}
        try:
            bars = md.today_bars(list(atr))
        except Exception:  # noqa: BLE001
            log.exception("orb_sip: could not load today's bars")
            return []
        exit_at = datetime.combine(ctx.today, time(15, 50), tzinfo=ET).isoformat()
        out = []
        for sym, raw in bars.items():
            done = [b for b in sorted(raw, key=lambda b: b.ts) if b.ts + BAR <= ctx.now]
            if len(done) < 2 or sym in ctx.held_symbols or not atr.get(sym):
                continue
            first = done[0]
            if first.ts.astimezone(ET).time() != time(9, 30) or first.close <= first.open:
                continue
            trig, last = first.high, done[-1]
            if last.high < trig:
                continue  # not broken out yet
            ref = max(trig, last.close)
            if ref > trig * 1.01:
                continue  # already ran >1% past the trigger: chasing, not a breakout entry
            stop = ref - self.stop_atr * atr[sym]
            out.append(Signal(self.name, sym, Direction.LONG, 0.8, 0.25, {
                "ref_price": round(ref, 4), "trigger": round(trig, 4), "stop": round(stop, 4), "target": None,
                "exit_at": exit_at, "size_pct": self.size_pct, "atr14": round(atr[sym], 4),
                "rvol_ratio": next(r["rvol_ratio"] for r in rows if r["symbol"] == sym),
                "paper": "Zarattini, Barbon & Aziz 2024 ORB on Stocks in Play (long side)"}, ts=ctx.now))
        return out
