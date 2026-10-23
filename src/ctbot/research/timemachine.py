"""Time machine: replay the rest of a session thousands of times before a day trade.

The day-trader pack's trader-timemachine (github.com/Jakeschincariol/day-trader-skill, MIT),
on CTbot's own 5-minute bars:

1. **The pool.** Full regular sessions before the plan's day (all 78 five-minute bars, so half
   days and gaps are left out). Each bar becomes four ratios to the close before it: open,
   high, low and close over the previous close.
2. **One replay.** Start at the entry. For each bar left until the exit time, draw a past bar
   from the same time of day (plus or minus `window` bars) and scale it to the current price.
   The stop is checked before the target, so a bar that touches both counts as the stop.
3. **The odds** over `replays` draws (seeded, so a run can be repeated): target first, stop
   first, neither (still open at the exit time), and green (target first, or above the entry
   at the exit time). R is +target_r at the target, -1 at the stop, otherwise the move from
   the entry over the risk. A gap through the stop still counts as -1R; a real fill can be worse.
4. **The history.** The same plan (entry at the same time of day, the stop and the target at
   the same distances) on every past session's real bars. What happened, not a simulation.
5. **The verdict.** Of the replays that hit a side, how often the target came first, against
   break-even 1 / (1 + target_r): 50% at 1R, 33% at 2R. Within 5 points "close to a coin
   flip" (1R) or "close to break-even"; 5-15 points "leans"; 15+ "strongly".

A simulation from the stock's own recent bars, not a prediction: earnings, news and halts can
do things those days never did. Context for Claude's review only; it never blocks a trade.

    ctbot-timemachine --symbol TSLA --date 2026-10-19 --entry 364.18 --stop 358.32 \
        --target 370.04 --entry-time 10:15        # bars from data/bars_cache/5minute
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from ctbot import market_hours
from ctbot.market_hours import ET
from ctbot.models import Bar

log = logging.getLogger(__name__)

SLOTS = 78  # 9:30 to 16:00
MIN_SESSIONS = 20
SEED = 19


def slot_of(ts: datetime) -> int:
    """Five-minute slot of a bar's start time: 0 = 9:30 ET, 77 = 15:55 ET."""
    et = ts.astimezone(ET)
    return (et.hour * 60 + et.minute - (9 * 60 + 30)) // 5


def full_sessions(bars: list[Bar], before: date) -> list[list[Bar]]:
    """Sessions before `before` that have every one of the 78 regular bars, oldest first."""
    days: dict[date, dict[int, Bar]] = defaultdict(dict)
    for b in bars:
        d = b.ts.astimezone(ET).date()
        s = slot_of(b.ts)
        if d < before and 0 <= s < SLOTS:
            days[d][s] = b
    return [[days[d][s] for s in range(SLOTS)] for d in sorted(days) if len(days[d]) == SLOTS]


def ratio_pool(sessions: list[list[Bar]]) -> list[list[tuple[float, float, float, float]]]:
    """pool[slot] = (open, high, low, close) over the previous close, from every session.
    Slot 0 is relative to the previous session's last close (the overnight gap)."""
    pool: list[list[tuple[float, float, float, float]]] = [[] for _ in range(SLOTS)]
    prev_close = None
    for bars in sessions:
        for s, b in enumerate(bars):
            pc = bars[s - 1].close if s else prev_close
            if pc:
                pool[s].append((b.open / pc, b.high / pc, b.low / pc, b.close / pc))
        prev_close = bars[-1].close
    return pool


def _bars_from(day: date, t: datetime) -> int:
    """Slot of the first bar starting at or after `t` (bars before it are done or in progress)."""
    secs = (t - datetime.combine(day, market_hours.OPEN, tzinfo=ET)).total_seconds()
    return int(-(-secs // 300))


def _pctl(xs: list[float], q: float) -> float:
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def verdict(p_target: float, p_stop: float, target_r: float | None) -> str:
    if target_r is None or p_target + p_stop <= 0:
        return "No target: the odds are where the price sits at the exit time."
    share = p_target / (p_target + p_stop)
    even = 1 / (1 + target_r)
    gap = share - even
    side = "the target" if gap > 0 else "the stop"
    odds = f"target first {p_target:.1%} vs stop first {p_stop:.1%}"
    if abs(gap) < 0.05:
        return f"{'Close to a coin flip' if abs(target_r - 1) < 1e-9 else 'Close to break-even'}: {odds}."
    if abs(gap) < 0.15:
        return f"Leans toward {side}: {odds}."
    return f"Strongly toward {side}: {odds}."


def simulate(pool, start_slot: int, end_slot: int, entry: float, stop: float, target: float | None,
             replays: int = 10_000, seed: int = SEED, window: int = 2) -> dict[str, Any]:
    """Replay bars start_slot..end_slot-1 from `entry`. See the module docstring."""
    risk = entry - stop
    target_r = (target - entry) / risk if target is not None else None
    rng = random.Random(seed)
    choices = []
    for s in range(start_slot, end_slot):
        near = [r for k in range(max(0, s - window), min(SLOTS, s + window + 1)) for r in pool[k]]
        choices.append(near)
    hit_t = hit_s = green = 0
    rs, closes = [], []
    for _ in range(replays):
        px, outcome = entry, None
        for near in choices:
            _, h, lo, c = rng.choice(near)
            if px * lo <= stop:
                outcome = "stop"
                break
            if target is not None and px * h >= target:
                outcome = "target"
                break
            px *= c
        if outcome == "stop":
            hit_s += 1
            rs.append(-1.0)
        elif outcome == "target":
            hit_t += 1
            green += 1
            rs.append(target_r)
        else:
            green += px > entry
            rs.append((px - entry) / risk)
            closes.append(px)
    closes.sort()
    n = float(replays)
    p_t, p_s = hit_t / n, hit_s / n
    return {
        "replays": replays, "seed": seed, "bars": end_slot - start_slot,
        "target_first": round(p_t, 4), "stop_first": round(p_s, 4),
        "neither": round(1 - p_t - p_s, 4), "green": round(green / n, 4),
        # where the replays that hit neither side ended
        "open_end_median": round(statistics.median(closes), 4) if closes else None,
        "open_end_p05": round(_pctl(closes, 0.05), 4) if closes else None,
        "open_end_p95": round(_pctl(closes, 0.95), 4) if closes else None,
        "avg_r": round(statistics.fmean(rs), 3), "target_r": round(target_r, 2) if target_r else None,
        "verdict": verdict(p_t, p_s, target_r),
    }


def history(sessions: list[list[Bar]], entry_slot: int, end_slot: int, stop_pct: float,
            target_pct: float | None) -> dict[str, Any]:
    """The same plan on each past session: buy the close of the bar before `entry_slot`, then the
    real bars until `end_slot`, stop checked first."""
    n_t = n_s = n_n = 0
    rs = []
    for bars in sessions:
        entry = bars[entry_slot - 1].close
        stop = entry * (1 - stop_pct)
        target = entry * (1 + target_pct) if target_pct is not None else None
        risk, outcome, px = entry - stop, None, entry
        for b in bars[entry_slot:end_slot]:
            if b.low <= stop:
                outcome = "stop"
                break
            if target is not None and b.high >= target:
                outcome = "target"
                break
            px = b.close
        if outcome == "stop":
            n_s += 1
            rs.append(-1.0)
        elif outcome == "target":
            n_t += 1
            rs.append((target - entry) / risk)
        else:
            n_n += 1
            rs.append((px - entry) / risk)
    return {"sessions": len(sessions), "target": n_t, "stop": n_s, "neither": n_n,
            "avg_r": round(statistics.fmean(rs), 3) if rs else None}


def replay(bars: list[Bar], day: date, entry_time: datetime, exit_time: datetime | None,
           entry: float, stop: float, target: float | None, replays: int = 10_000,
           seed: int = SEED, window: int = 2) -> dict[str, Any] | None:
    """Odds and history for one long plan; None when the plan or the data doesn't allow it
    (fewer than MIN_SESSIONS full sessions, a stop not under the entry, no bars left)."""
    if not (stop < entry) or (target is not None and target <= entry):
        return None
    sessions = full_sessions(bars, day)
    if len(sessions) < MIN_SESSIONS:
        return None
    close = datetime.combine(day, market_hours.close_time(day), tzinfo=ET)
    until = min(exit_time, close) if exit_time else close
    start_slot, end_slot = _bars_from(day, entry_time), min(SLOTS, _bars_from(day, until))
    if not (0 < start_slot < end_slot):
        return None
    out = simulate(ratio_pool(sessions), start_slot, end_slot, entry, stop, target, replays, seed, window)
    out["history"] = history(sessions, start_slot, end_slot, 1 - stop / entry,
                             target / entry - 1 if target is not None else None)
    out["until_et"] = until.astimezone(ET).strftime("%H:%M")
    out["note"] = "simulation from the stock's own recent 5-minute bars, not a prediction"
    return out


class ReplayOdds:
    """Review context: time-machine odds for an intraday candidate's plan. Loads ~60 sessions
    of 5-minute bars once per symbol per day from the market data (read-only), then replays."""

    def __init__(self, md, replays: int = 2_000, days: int = 85) -> None:
        self.md, self.replays, self.days = md, replays, days
        self._bars: dict[tuple[str, date], list[Bar]] = {}

    def _history(self, symbol: str, day: date) -> list[Bar]:
        key = (symbol.upper(), day)
        if key not in self._bars:
            bars: list[Bar] = []
            if hasattr(self.md, "intraday_bars"):
                start = datetime.combine(day - timedelta(days=self.days), market_hours.OPEN, tzinfo=ET)
                end = datetime.combine(day, market_hours.OPEN, tzinfo=ET)
                try:
                    bars = self.md.intraday_bars([symbol], start, end).get(symbol.upper(), [])
                except Exception:  # noqa: BLE001 - context only
                    log.info("time machine: no 5-minute history for %s", symbol)
            if not bars:
                from ctbot.research import bars as cache

                bars = cache.load("5minute", symbol)
            self._bars = {k: v for k, v in self._bars.items() if k[1] == day}
            self._bars[key] = bars
        return self._bars[key]

    def for_plan(self, symbol: str, plan: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
        """Odds for a same-day plan with a stop (models' exit plans); None otherwise."""
        if not plan or plan.get("stop") is None or not plan.get("ref_price") or not plan.get("exit_at"):
            return None
        day = market_hours.trading_date(now)
        exit_at = datetime.fromisoformat(plan["exit_at"])
        if market_hours.trading_date(exit_at) != day:
            return None  # held overnight: a one-session replay doesn't describe it
        target = plan.get("target")
        return replay(self._history(symbol, day), day, now, exit_at, float(plan["ref_price"]),
                      float(plan["stop"]), float(target) if target is not None else None,
                      replays=self.replays)


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay a long day-trade plan on cached 5-minute bars")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--date", required=True, help="the plan's day, YYYY-MM-DD")
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--stop", type=float, required=True)
    ap.add_argument("--target", type=float)
    ap.add_argument("--entry-time", required=True, help="HH:MM New York time")
    ap.add_argument("--until", default=None, help="HH:MM New York time (default: the close)")
    ap.add_argument("--replays", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    from ctbot.research import bars as cache

    day = date.fromisoformat(a.date)
    hh, mm = (int(x) for x in a.entry_time.split(":"))
    entry_time = datetime.combine(day, time(hh, mm), tzinfo=ET)
    until = None
    if a.until:
        uh, um = (int(x) for x in a.until.split(":"))
        until = datetime.combine(day, time(uh, um), tzinfo=ET)
    out = replay(cache.load("5minute", a.symbol), day, entry_time, until, a.entry, a.stop, a.target,
                 a.replays, a.seed)
    if out is None:
        raise SystemExit(f"cannot replay: need {MIN_SESSIONS}+ full sessions of 5-minute bars before {day} "
                         f"(ctbot-fetch-bars --interval 5minute), a stop under the entry and bars left")
    if a.json:
        print(json.dumps(out, indent=2))
        return
    h = out["history"]
    print(f"{out['replays']:,} replays of {a.symbol.upper()} {a.entry_time} to {out['until_et']} ET "
          f"({out['bars']} five-minute bars), seed {out['seed']}.")
    print(f"  target first {out['target_first']:6.1%}\n  stop first   {out['stop_first']:6.1%}\n"
          f"  neither      {out['neither']:6.1%}   still open at {out['until_et']}\n"
          f"  green        {out['green']:6.1%}")
    if out["open_end_median"] is not None:
        print(f"  Still open at {out['until_et']}: median {out['open_end_median']}, 90% between "
              f"{out['open_end_p05']} and {out['open_end_p95']}.")
    print(f"  Average R {out['avg_r']:+.3f}")
    print(f"History: the same plan on {h['sessions']} real sessions: {h['target']} target, {h['stop']} stop, "
          f"{h['neither']} neither. Average R {h['avg_r']:+.3f}")
    print(out["verdict"])
    print("A simulation from the stock's own recent bars, not a prediction.")
