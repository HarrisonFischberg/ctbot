"""Multi-day ETF models with positive after-cost edges in BOTH periods of a 2010-2026 backtest
(in-sample 2010-2019, out-of-sample 2020-2026; costs 2 bps per side). Both decide in the
15:30-15:44 ET window using the live price as today's close, so they trade near the close the
way the backtest did, and neither is a day trade.

* rsi2_etf (Connors RSI(2)): ETF above its 200-day average with a 2-day RSI under 10 -> buy;
  sell near the close once the price is back above its 5-day average. 14 ETFs pooled:
  +11.3 bps/trade (70% wins, n=2702) in-sample, +16.8 bps (69%, n=1872) out-of-sample.
* turn_of_month: buy SPY/QQQ the day before the month's last trading day, sell on the 3rd
  trading day of the next month. SPY +19.0/+34.3 bps, QQQ +33.0/+47.1 bps per trade (IS/OOS).

As stand-alone portfolios both trailed simply holding SPY/QQQ over 2010-2026 (they are in the
market only part of the time), so these are edges, not a path to beating the index.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from ctbot.market_hours import ET, next_business_day
from ctbot.models import Direction, EventKind, Signal
from ctbot.strategies.base import StrategyContext

log = logging.getLogger(__name__)

ETFS = ("SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLV", "XLY", "XLI", "XLP", "XLU", "XLE", "XLB", "SMH")


def _window(ctx: StrategyContext) -> bool:
    if ctx.now is None:
        return False
    t = ctx.now.astimezone(ET).time()
    return time(15, 30) <= t <= time(15, 44)


def _at(d: date, hh: int, mm: int) -> str:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).isoformat()


def _add_bdays(d: date, n: int) -> date:
    for _ in range(n):
        d = next_business_day(d)
    return d


def rsi(closes: list[float], n: int = 2) -> float:
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(len(closes) - n, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(len(closes) - n, len(closes))]
    ag, al = sum(gains) / n, sum(losses) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


class _Swing:
    event_kinds: frozenset[EventKind] = frozenset()
    name = "swing"

    def _closes_with_live(self, ctx: StrategyContext, sym: str, n: int) -> list[float] | None:
        """Completed daily closes plus the live price standing in for today's close."""
        try:
            hist = [b.close for b in ctx.market_data.daily_bars(sym, n)]
            q = ctx.market_data.quotes([sym]).get(sym)
        except Exception:  # noqa: BLE001
            log.exception("%s: no data for %s", self.name, sym)
            return None
        if len(hist) < n or not q or not q.last:
            return None
        return hist + [q.last]


class Rsi2Etf(_Swing):
    """Connors RSI(2): buy an ETF above its 200-day average when its 2-day RSI is under 10, near
    the close; sell near the close once price is back above its 5-day average (max 10 days).
    14 ETFs 2010-26: +11.3 bps/trade (70% wins) in-sample, +16.8 bps (69%) out-of-sample;
    capital-inefficient as a portfolio (trails holding the index)."""

    name = "rsi2_etf"

    def __init__(self, rsi_below: float = 10.0, max_hold_days: int = 10, disaster_stop: float = 0.10,
                 exclude: list[str] | None = None) -> None:
        self.rsi_below, self.max_hold_days, self.disaster_stop = rsi_below, max_hold_days, disaster_stop
        self.exclude = {s.upper() for s in exclude or []}  # e.g. QQQ when trend_core owns it

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        if not _window(ctx):
            return []
        out = []
        for sym in (s for s in ETFS if s in ctx.universe and s not in self.exclude):
            c = self._closes_with_live(ctx, sym, 200)
            if c is None:
                continue
            px, sma200, sma5, r = c[-1], sum(c[-200:]) / 200, sum(c[-5:]) / 5, rsi(c)
            if sym in ctx.held_symbols:
                if px > sma5:  # the model's own exit; only acts on positions this model opened
                    out.append(Signal(self.name, sym, Direction.EXIT, 1.0, 0, {"reason": "close_above_sma5",
                                      "price": round(px, 4), "sma5": round(sma5, 4)}, ts=ctx.now))
                continue
            if px > sma200 and r < self.rsi_below:
                exit_day = _add_bdays(ctx.today, self.max_hold_days)
                out.append(Signal(self.name, sym, Direction.LONG, min(1.0, (self.rsi_below - r) / self.rsi_below + 0.5),
                                  3.0, {"ref_price": round(px, 4), "rsi2": round(r, 2), "sma200": round(sma200, 4),
                                        "sma5": round(sma5, 4), "stop": round(px * (1 - self.disaster_stop), 4),
                                        "target": None, "exit_at": _at(exit_day, 15, 50),
                                        "exit_rule": "sell near the close once price > 5-day average",
                                        "backtest": "14 ETFs 2010-2026: IS +11.3 bps/trade 70% win n=2702; "
                                                    "OOS 2020-26 +16.8 bps 69% win n=1872"}, ts=ctx.now))
        return out


class TurnOfMonth(_Swing):
    """Buy the day before the month's last trading day, near the close; sell on the 3rd trading
    day of the next month. SPY 2010-26: +19.0 bps/trade in-sample, +34.3 bps out-of-sample."""

    name = "turn_of_month"

    def __init__(self, symbols: list[str] | tuple[str, ...] = ("SPY", "QQQ"), disaster_stop: float = 0.08) -> None:
        self.symbols, self.disaster_stop = tuple(s.upper() for s in symbols), disaster_stop

    @staticmethod
    def is_entry_day(d: date) -> bool:
        """Day before the month's last trading day (weekends known; exchange holidays not)."""
        last = next_business_day(d)
        return last.month == d.month and next_business_day(last).month != d.month

    @staticmethod
    def exit_day(d: date) -> date:
        x = d
        while x.month == d.month:
            x = next_business_day(x)
        return _add_bdays(x, 2)  # first trading day of next month + 2 = the 3rd

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        if not _window(ctx) or not self.is_entry_day(ctx.today):
            return []
        out = []
        for sym in self.symbols:
            if sym not in ctx.universe or sym in ctx.held_symbols:
                continue
            q = ctx.market_data.quotes([sym]).get(sym)
            if not q or not q.last:
                continue
            out.append(Signal(self.name, sym, Direction.LONG, 0.8, 4.0, {
                "ref_price": round(q.last, 4), "stop": round(q.last * (1 - self.disaster_stop), 4), "target": None,
                "exit_at": _at(self.exit_day(ctx.today), 15, 50),
                "backtest": "2010-2026 per trade: SPY IS +19.0 / OOS +34.3 bps; QQQ IS +33.0 / OOS +47.1 bps"},
                ts=ctx.now))
        return out


class TrendCore(_Swing):
    """Core allocation: hold the symbol (size_pct of equity) while its price is above its
    sma_days average; cash when it closes below; tops back up if it slips >5% under target.
    Decided 15:30-15:44 ET on the live price. Systematic: no Claude review, risk checks apply.
    QQQ 170d (length chosen on 2010-19 only): 2020-26 +20.4%/yr, max DD -21.9%, Sharpe 1.14 with
    3.5% cash yield; 1999-2009 crash test +3.6%/yr, max DD -46% (QQQ itself -83%)."""

    name = "trend_core"

    def __init__(self, symbol: str = "QQQ", sma_days: int = 200, size_pct: float = 0.5,
                 disaster_stop: float = 0.25, hold: str | None = None, vol_cap: float | None = None) -> None:
        self.symbol, self.sma_days, self.size_pct, self.disaster_stop = symbol, sma_days, size_pct, disaster_stop
        self.hold = (hold or symbol).upper()  # e.g. QLD (2x QQQ) while the signal stays on QQQ
        self.vol_cap = vol_cap  # e.g. 0.30: scale the position so its 20-day volatility is ~30%

    def target_weight(self, ctx: StrategyContext) -> float | None:
        if not self.vol_cap:
            return self.size_pct
        c = self._closes_with_live(ctx, self.hold, 20)
        if c is None:
            return None
        import math
        import statistics

        vol = statistics.pstdev([b / a - 1 for a, b in zip(c, c[1:])]) * math.sqrt(252)
        return self.size_pct * min(1.0, self.vol_cap / vol) if vol else self.size_pct

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        if not _window(ctx) or self.symbol not in ctx.universe:
            return []
        c = self._closes_with_live(ctx, self.symbol, self.sma_days - 1)
        w = self.target_weight(ctx)
        if c is None or w is None:
            return []
        px, sma = c[-1], sum(c[-self.sma_days:]) / self.sma_days
        ev = {"signal": self.symbol, "price": round(px, 4), "sma": round(sma, 4), "systematic": True}
        if self.hold in ctx.held_symbols:
            if px < sma:
                return [Signal(self.name, self.hold, Direction.EXIT, 1.0, 0, {**ev, "reason": "below_sma"}, ts=ctx.now)]
            # still in trend: the pipeline tops up or trims the position back to its target weight
            return [Signal(self.name, self.hold, Direction.LONG, 1.0, 365.0,
                           {**ev, "size_pct": round(w, 4), "rebalance": True}, ts=ctx.now)]
        if px > sma:
            q = ctx.market_data.quotes([self.hold]).get(self.hold)
            ref = q.last if q and q.last else px
            return [Signal(self.name, self.hold, Direction.LONG, 1.0, 365.0, {
                **ev, "ref_price": round(ref, 4), "size_pct": round(w, 4), "stop": round(ref * (1 - self.disaster_stop), 4),
                "target": None, "exit_at": _at(_add_bdays(ctx.today, 250), 15, 50)}, ts=ctx.now)]
        return []
