"""Intraday paper models, live versions of the rules in ctbot.research.backtest.

Each emits at most one LONG signal per symbol per day, with its own exit plan in the
signal's evidence: "stop" and "target" (prices, either may be null) and "exit_at" (ISO time),
which the risk manager turns into the position's bracket. They need a market-data provider
with today_bars() (Robinhood); with offline CSV data they emit nothing.

Fixed rules, not tuned. The 2026 backtest found no reliable edge for any of them; running
them forward on paper is how that gets confirmed or refuted.
"""

from __future__ import annotations

import logging
import statistics
from datetime import date, datetime, time, timedelta

from ctbot import market_hours
from ctbot.market_hours import ET
from ctbot.models import Bar, Direction, EventKind, Signal
from ctbot.research.backtest import vwap_series
from ctbot.strategies.base import StrategyContext

log = logging.getLogger(__name__)
BAR = timedelta(minutes=5)


def _at(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET)


def _completed(bars: list[Bar], now: datetime) -> list[Bar]:
    return [b for b in sorted(bars, key=lambda b: b.ts) if b.ts + BAR <= now]


def _uptrend(ctx: StrategyContext, sym: str, n: int = 50) -> bool:
    closes = [b.close for b in ctx.market_data.daily_bars(sym, n + 1)]
    return len(closes) == n + 1 and closes[-1] > sum(closes[-n - 1:-1]) / n


class _IntradayBase:
    event_kinds: frozenset[EventKind] = frozenset()
    name = "intraday"

    def _today(self, ctx: StrategyContext) -> dict[str, list[Bar]] | None:
        md = ctx.market_data
        if ctx.now is None or not hasattr(md, "today_bars") or not market_hours.session_open(ctx.now):
            return None
        try:
            return md.today_bars(list(ctx.universe))
        except Exception:  # noqa: BLE001 - data hiccup: skip this cycle, don't kill the bot
            log.exception("%s: could not load today's bars", self.name)
            return None

    def _signal(self, sym: str, strength: float, horizon: float, now: datetime, **evidence) -> Signal:
        return Signal(self.name, sym, Direction.LONG, round(min(1.0, strength), 3), horizon,
                      {k: (round(v, 4) if isinstance(v, float) else v) for k, v in evidence.items()}, ts=now)


class OpeningRangeBreakout(_IntradayBase):
    """Break of the 9:30-10:00 range, 10:00-12:00, above VWAP, on 1.2x+ usual opening volume.
    Stop: range low (at most 2% away). Target: 2x the risk. Out by 15:50 ET."""

    name = "orb"

    def __init__(self, min_rvol: float = 1.2, max_risk_pct: float = 0.02, reward_risk: float = 2.0) -> None:
        self.min_rvol, self.max_risk_pct, self.reward_risk = min_rvol, max_risk_pct, reward_risk
        self._avg_open_vol: dict[tuple[str, date], float | None] = {}

    def _avg_first30(self, sym: str, d: date) -> float | None:
        key = (sym, d)
        if key not in self._avg_open_vol:
            from ctbot.research import bars as cache

            by_day: dict[date, float] = {}
            for b in cache.load("5minute", sym):
                bd = b.ts.astimezone(ET)
                if bd.date() < d and bd.time() < time(10, 0):
                    by_day[bd.date()] = by_day.get(bd.date(), 0.0) + b.volume
            vols = [by_day[x] for x in sorted(by_day)[-20:]]
            self._avg_open_vol[key] = statistics.mean(vols) if len(vols) >= 10 else None
        return self._avg_open_vol[key]

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        today = self._today(ctx)
        if not today or not (_at(ctx.today, 10, 5) <= ctx.now <= _at(ctx.today, 12, 5)):
            return []
        out = []
        for sym, raw in today.items():
            bars = _completed(raw, ctx.now)
            if len(bars) < 7 or sym in ctx.held_symbols:
                continue
            rng, last = bars[:6], bars[-1]
            hi, lo = max(b.high for b in rng), min(b.low for b in rng)
            avg = self._avg_first30(sym, ctx.today)
            rvol = sum(b.volume for b in rng) / avg if avg else None
            if rvol is None or rvol < self.min_rvol:
                continue
            if last.close > hi and last.close > vwap_series(bars)[-1]:
                stop = max(lo, last.close * (1 - self.max_risk_pct))
                target = last.close + self.reward_risk * (last.close - stop)
                out.append(self._signal(sym, rvol / 3, 0.25, ctx.now, ref_price=last.close, stop=stop, target=target,
                                        exit_at=_at(ctx.today, 15, 50).isoformat(), range_high=hi,
                                        range_low=lo, rvol=rvol))
        return out


class VwapReversion(_IntradayBase):
    """In names above their 50-day average, buy a 0.75%+ dip below VWAP (10:30-15:00).
    Stop -1.5%. Target: VWAP at signal time. Out by 15:50 ET."""

    name = "vwap_revert"

    def __init__(self, min_dip: float = 0.0075, stop_pct: float = 0.015) -> None:
        self.min_dip, self.stop_pct = min_dip, stop_pct

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        today = self._today(ctx)
        if not today or not (_at(ctx.today, 10, 30) <= ctx.now <= _at(ctx.today, 15, 5)):
            return []
        out = []
        for sym, raw in today.items():
            bars = _completed(raw, ctx.now)
            if len(bars) < 12 or sym in ctx.held_symbols:
                continue
            vw = vwap_series(bars)[-1]
            dist = bars[-1].close / vw - 1
            if dist <= -self.min_dip and _uptrend(ctx, sym):
                out.append(self._signal(sym, -dist / (4 * self.min_dip), 0.2, ctx.now, ref_price=bars[-1].close,
                                        stop=bars[-1].close * (1 - self.stop_pct), target=vw,
                                        exit_at=_at(ctx.today, 15, 50).isoformat(), vwap=vw, dip=dist))
        return out


class OvernightHold(_IntradayBase):
    """In names above their 50-day average, buy 15:30-15:44 ET and sell at 9:35 ET next
    trading day. Not a day trade, so it does not use the PDT budget. Stop -3%, no target."""

    name = "overnight"

    def __init__(self, stop_pct: float = 0.03, max_per_day: int = 2) -> None:
        self.stop_pct, self.max_per_day = stop_pct, max_per_day

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        today = self._today(ctx)
        if not today or not (_at(ctx.today, 15, 30) <= ctx.now <= _at(ctx.today, 15, 44)):
            return []
        nxt = _at(market_hours.next_business_day(ctx.today), 9, 35).isoformat()
        picks = []
        for sym, raw in today.items():
            bars = _completed(raw, ctx.now)
            if len(bars) < 70 or sym in ctx.held_symbols or not _uptrend(ctx, sym):
                continue
            day_ret = bars[-1].close / bars[0].open - 1
            picks.append((day_ret, sym, bars[-1].close))
        # strongest closes first; a few per day keeps Claude calls and exposure small
        picks.sort(reverse=True)
        return [self._signal(sym, 0.5 + max(0.0, r) * 10, 1.0, ctx.now, ref_price=px, stop=px * (1 - self.stop_pct),
                             target=None, exit_at=nxt, day_return=r)
                for r, sym, px in picks[: self.max_per_day]]
