"""Opening-range breakout on market-wide Stocks in Play (Zarattini, Barbon & Aziz 2024), long side.

Each morning the scanner picks the most unusually active Russell 1000 names. For each whose
first 5-minute bar closed green, buy when price trades through that bar's high; stop
0.10 x ATR(14) below the entry (the paper's rule); exit at 15:50 ET. Entries 9:40-11:00 ET.

Entries follow the day-trader pack's trader-entry rule (`confirm = "close"`, the default since
2026-10-23): a breakout counts only once a completed 5-minute bar CLOSES above the first bar's
high, so a wick through it is not an entry. `min_volume_x` > 0 also wants that bar's volume at
that multiple of the first bar's. `confirm = "touch"` is the paper's original rule (any trade
through the high).

Our own 88-large-cap backtest of this rule was negative out-of-sample (the paper's edge came
from a ~7,000-stock universe); this model tests the market-wide version forward on paper with
a fixed sleeve (size_pct of equity per trade).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from ctbot import market_hours
from ctbot.market_hours import ET
from ctbot.models import Direction, EventKind, Signal
from ctbot.strategies.base import StrategyContext
from ctbot.strategies.intraday import load_today_bars

BAR = timedelta(minutes=5)


class OrbStocksInPlay:
    name = "orb_sip"
    event_kinds: frozenset[EventKind] = frozenset()

    def __init__(self, stop_atr: float = 0.10, size_pct: float = 0.20, top_k: int = 20,
                 last_entry: str = "11:00", scanner=None, confirm: str = "close",
                 min_volume_x: float = 0.0) -> None:
        if confirm not in ("touch", "close"):
            raise ValueError(f"orb_sip confirm must be 'touch' or 'close', not {confirm!r}")
        self.stop_atr, self.size_pct, self.top_k = stop_atr, size_pct, top_k
        self.confirm, self.min_volume_x = confirm, min_volume_x
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
        bars = load_today_bars(md, list(atr), self.name)
        if bars is None:
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
            if self.confirm == "close":
                broke = next((b for b in done[1:] if b.close > trig), None)
                if broke is None:
                    continue  # no completed bar has closed above the range yet
                if self.min_volume_x > 0 and broke.volume < self.min_volume_x * first.volume:
                    continue  # the breakout bar was too thin
            elif last.high < trig:
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
