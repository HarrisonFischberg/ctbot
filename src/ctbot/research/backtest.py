"""Backtest long-only intraday / overnight strategies on cached bars, under the account's rules.

Rules modeled (from the live constraints of the MCP account):
  * long only, fixed $50 per trade (20% of $250), at most 3 open positions, one per symbol
  * pattern-day-trader limit: at most 3 day trades (same-day round trips) per 5 business days
  * costs: a per-side charge (spread + slippage) in basis points, by symbol type
  * no lookahead: a signal on bar i's close fills at bar i+1's open; filters use prior days only;
    if a bar touches both stop and target, the stop is assumed to fill first

Strategies are fixed rules chosen before looking at results (no parameter search), and every
result is reported separately for an in-sample and a later out-of-sample period.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ctbot.market_hours import ET
from ctbot.models import Bar
from ctbot.research import bars as bar_cache

ETFS = {"SPY", "QQQ", "IWM", "XLF", "XLE", "XLK", "DIA"}
STAKE = 50.0
CAPITAL = 250.0
MAX_POSITIONS = 3
MAX_DAY_TRADES_5D = 3


def cost_bps(symbol: str) -> float:
    """Per-side cost: half-spread + slippage. Conservative for these liquid names."""
    return 2.0 if symbol in ETFS else 4.0


@dataclass(frozen=True)
class Trade:
    strategy: str
    symbol: str
    entry_ts: datetime
    entry_px: float
    exit_ts: datetime
    exit_px: float
    reason: str
    score: float

    @property
    def day_trade(self) -> bool:
        return self.entry_ts.astimezone(ET).date() == self.exit_ts.astimezone(ET).date()

    @property
    def net_ret(self) -> float:
        c = cost_bps(self.symbol) / 1e4
        return (self.exit_px * (1 - c)) / (self.entry_px * (1 + c)) - 1


# ---- data ---------------------------------------------------------------------

def by_day(bars: list[Bar]) -> dict[date, list[Bar]]:
    out: dict[date, list[Bar]] = defaultdict(list)
    for b in bars:
        out[b.ts.astimezone(ET).date()].append(b)
    return dict(out)


def daily_closes(symbol: str) -> dict[date, Bar]:
    return {b.ts.date(): b for b in bar_cache.load("day", symbol)}


def sma_before(daily: dict[date, Bar], days: list[date], d: date, n: int) -> float | None:
    """SMA of the n daily closes strictly before d."""
    prior = [x for x in days if x < d][-n:]
    return sum(daily[x].close for x in prior) / n if len(prior) == n else None


def run_exit(bars: list[Bar], i: int, entry: float, stop: float | None, target: float | None,
             last_exit_idx: int) -> tuple[datetime, float, str]:
    """Scan bars after entry bar i: stop, target, else exit at the open of bar last_exit_idx."""
    for j in range(i, min(last_exit_idx, len(bars))):
        b = bars[j]
        if stop is not None and b.low <= stop:
            return b.ts, min(stop, b.open), "stop"  # gap below stop fills at the open
        if target is not None and b.high >= target:
            return b.ts, max(target, b.open), "target"
    k = min(last_exit_idx, len(bars) - 1)
    return bars[k].ts, bars[k].open, "time"


def vwap_series(bars: list[Bar]) -> list[float]:
    pv = v = 0.0
    out = []
    for b in bars:
        typical = (b.high + b.low + b.close) / 3
        pv += typical * b.volume
        v += b.volume
        out.append(pv / v if v else b.close)
    return out


# ---- strategies -----------------------------------------------------------------
# Each takes (symbol, day, that day's 5-min bars, context) and returns candidate Trades.

EXIT_BAR = 77  # 15:55 bar open, i.e. sell ~5 minutes before the close


def orb(sym: str, d: date, bars: list[Bar], ctx: dict) -> list[Trade]:
    """30-minute opening-range breakout with relative-volume and VWAP confirmation."""
    if len(bars) < 78:
        return []
    rng = bars[:6]
    hi, lo = max(b.high for b in rng), min(b.low for b in rng)
    rvol = ctx["rvol"].get((sym, d))
    if rvol is None or rvol < 1.2:
        return []
    vw = vwap_series(bars)
    for i in range(6, 30):  # signals 10:00-12:00
        b = bars[i]
        if b.close > hi and b.close > vw[i]:
            entry = bars[i + 1].open
            stop = max(lo, entry * 0.98)  # risk at most 2%
            target = entry + 2 * (entry - stop)
            ts, px, why = run_exit(bars, i + 1, entry, stop, target, EXIT_BAR)
            return [Trade("orb", sym, bars[i + 1].ts, entry, ts, px, why, rvol)]
    return []


def vwap_revert(sym: str, d: date, bars: list[Bar], ctx: dict) -> list[Trade]:
    """Buy a >=0.75% dip below VWAP in a name above its 50-day average; exit at VWAP."""
    if len(bars) < 78 or not ctx["uptrend"].get((sym, d)):
        return []
    vw = vwap_series(bars)
    for i in range(12, 66):  # signals 10:30-15:00
        dist = bars[i].close / vw[i] - 1
        if dist <= -0.0075:
            entry = bars[i + 1].open
            ts, px, why = run_exit(bars, i + 1, entry, entry * 0.985, vw[i], EXIT_BAR)
            return [Trade("vwap_revert", sym, bars[i + 1].ts, entry, ts, px, why, -dist)]
    return []


def overnight(sym: str, d: date, bars: list[Bar], ctx: dict) -> list[Trade]:
    """Buy into the close (15:55 bar open), sell at the next session's open. Not a day trade."""
    nxt = ctx["next_day_bars"].get((sym, d))
    if len(bars) < 78 or not nxt or not ctx["uptrend"].get((sym, d)):
        return []
    entry = bars[EXIT_BAR].open
    return [Trade("overnight", sym, bars[EXIT_BAR].ts, entry, nxt[0].ts, nxt[0].open, "next_open", 0.0)]


STRATEGIES: dict[str, Callable] = {"orb": orb, "vwap_revert": vwap_revert, "overnight": overnight}


def build_context(intraday: dict[str, dict[date, list[Bar]]]) -> dict:
    ctx: dict = {"rvol": {}, "uptrend": {}, "next_day_bars": {}}
    for sym, days in intraday.items():
        order = sorted(days)
        first30 = {d: sum(b.volume for b in days[d][:6]) for d in order}
        daily = daily_closes(sym)
        ddays = sorted(daily)
        for k, d in enumerate(order):
            prev = [first30[x] for x in order[max(0, k - 20):k]]
            if len(prev) == 20 and statistics.mean(prev) > 0:
                ctx["rvol"][(sym, d)] = first30[d] / statistics.mean(prev)
            sma = sma_before(daily, ddays, d, 50)
            prior = [x for x in ddays if x < d]
            if sma and prior:
                ctx["uptrend"][(sym, d)] = daily[prior[-1]].close > sma
            if k + 1 < len(order):
                ctx["next_day_bars"][(sym, d)] = days[order[k + 1]]
    return ctx


# ---- portfolio ------------------------------------------------------------------

def allocate(trades: list[Trade]) -> list[Trade]:
    """Greedy, in time order: max 3 open, one per symbol, <=3 day trades per 5 business days."""
    taken: list[Trade] = []
    day_trades: list[date] = []
    for t in sorted(trades, key=lambda t: (t.entry_ts, -t.score)):
        open_now = [x for x in taken if x.entry_ts <= t.entry_ts < x.exit_ts]
        if len(open_now) >= MAX_POSITIONS or any(x.symbol == t.symbol for x in open_now):
            continue
        if t.day_trade:
            d = t.entry_ts.astimezone(ET).date()
            window = [x for x in day_trades if 0 <= _bdays(x, d) < 5]
            if len(window) >= MAX_DAY_TRADES_5D:
                continue
            day_trades.append(d)
        taken.append(t)
    return taken


def _bdays(a: date, b: date) -> int:
    n, x = 0, a
    while x < b:
        x += timedelta(days=1)
        n += x.weekday() < 5
    return n


def metrics(trades: list[Trade], days: list[date]) -> dict:
    if not trades:
        return {"trades": 0}
    rets = [t.net_ret for t in trades]
    pnl_by_day: dict[date, float] = defaultdict(float)
    for t in trades:
        pnl_by_day[t.exit_ts.astimezone(ET).date()] += STAKE * t.net_ret
    daily = [pnl_by_day.get(d, 0.0) for d in days]
    eq, peak, mdd = CAPITAL, CAPITAL, 0.0
    for p in daily:
        eq += p
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    wins, losses = [r for r in rets if r > 0], [r for r in rets if r <= 0]
    sd = statistics.pstdev(daily) if len(daily) > 1 else 0.0
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades),
        "avg_net_bps": statistics.mean(rets) * 1e4,
        "avg_gross_bps": statistics.mean(t.exit_px / t.entry_px - 1 for t in trades) * 1e4,
        "median_net_bps": statistics.median(rets) * 1e4,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) else None,
        "pnl_usd": sum(STAKE * r for r in rets),
        "return_on_250": sum(STAKE * r for r in rets) / CAPITAL,
        "max_drawdown": mdd,
        "sharpe": (statistics.mean(daily) / sd * math.sqrt(252)) if sd else None,
        "day_trades": sum(t.day_trade for t in trades),
        "exits": dict(sorted({r: sum(t.reason == r for t in trades) for r in {t.reason for t in trades}}.items())),
    }


def spy_buy_hold(intraday: dict[str, dict[date, list[Bar]]], days: list[date]) -> float | None:
    spy = intraday.get("SPY")
    if not spy or not days:
        return None
    first, last = spy[days[0]][0].open, spy[days[-1]][-1].close
    return last / first - 1


def run(symbols: list[str], split: date) -> dict:
    intraday = {s: by_day(bar_cache.load("5minute", s)) for s in symbols}
    intraday = {s: d for s, d in intraday.items() if d}
    ctx = build_context(intraday)
    all_days = sorted({d for days in intraday.values() for d in days})
    periods = {"in_sample": [d for d in all_days if d < split], "out_of_sample": [d for d in all_days if d >= split]}
    report: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "symbols": sorted(intraday),
                    "split": split.isoformat(), "periods": {}, "rules": {
                        "stake": STAKE, "capital": CAPITAL, "max_positions": MAX_POSITIONS,
                        "max_day_trades_5d": MAX_DAY_TRADES_5D, "cost_bps_etf": cost_bps("SPY"),
                        "cost_bps_stock": cost_bps("KO")}}
    for pname, pdays in periods.items():
        pset = set(pdays)
        res = {"days": len(pdays), "from": pdays[0].isoformat(), "to": pdays[-1].isoformat(),
               "spy_buy_hold": spy_buy_hold(intraday, pdays), "strategies": {}}
        combined: list[Trade] = []
        for name, fn in STRATEGIES.items():
            ideas = [t for s, days in intraday.items() for d, b in days.items() if d in pset for t in fn(s, d, b, ctx)]
            combined += ideas
            res["strategies"][name] = {"raw_ideas": metrics(ideas, pdays), "with_account_rules": metrics(allocate(ideas), pdays)}
        res["strategies"]["combined"] = {"with_account_rules": metrics(allocate(combined), pdays)}
        report["periods"][pname] = res
    return report


def daily_overnight(symbols: list[str], split: date) -> dict:
    """Long-history check of the overnight rule on daily bars: buy at close, sell next open."""
    out = {}
    for sym in symbols:
        daily = daily_closes(sym)
        ds = sorted(daily)
        rets = {"in_sample": [], "out_of_sample": []}
        for k in range(50, len(ds) - 1):
            d, nd = ds[k], ds[k + 1]
            sma = sum(daily[x].close for x in ds[k - 50:k]) / 50  # prior 50 closes
            if daily[ds[k - 1]].close <= sma:
                continue
            c = cost_bps(sym) / 1e4
            r = daily[nd].open * (1 - c) / (daily[d].close * (1 + c)) - 1
            rets["in_sample" if d < split else "out_of_sample"].append(r)
        out[sym] = {p: {"n": len(r), "avg_net_bps": statistics.mean(r) * 1e4 if r else None,
                        "win_rate": sum(x > 0 for x in r) / len(r) if r else None} for p, r in rets.items()}
    return out


def main() -> None:
    from ctbot.config import Settings, load_universe

    p = argparse.ArgumentParser(description="Backtest intraday strategies on cached bars")
    p.add_argument("--split", type=date.fromisoformat, default=date(2026, 7, 1),
                   help="first out-of-sample day (5-minute data)")
    p.add_argument("--daily-split", type=date.fromisoformat, default=date(2020, 1, 1))
    p.add_argument("--out", type=Path, default=Path("data/backtests"))
    args = p.parse_args()
    symbols = load_universe(Settings.from_env().universe_file)
    report = run(symbols, args.split)
    report["overnight_daily_history"] = daily_overnight(symbols, args.daily_split)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"backtest_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    _print(report)
    print(f"\nfull report: {path}")


def _print(r: dict) -> None:
    def f(v, kind):
        if v is None:
            return "—"
        return {"pct": f"{v * 100:+.1f}%", "bps": f"{v:+.1f}", "usd": f"${v:+.2f}", "x": f"{v:.2f}"}[kind]

    for pname, res in r["periods"].items():
        print(f"\n== {pname}: {res['from']} .. {res['to']} ({res['days']} days) · SPY buy&hold {f(res['spy_buy_hold'], 'pct')}")
        print(f"{'strategy':<13}{'set':<8}{'trades':>7}{'win':>7}{'gross':>7}{'avg bps':>9}{'PF':>6}{'P&L':>9}{'on $250':>9}{'maxDD':>8}{'sharpe':>8}")
        for name, s in res["strategies"].items():
            for label, m in (("ideas", s.get("raw_ideas")), ("rules", s.get("with_account_rules"))):
                if not m:
                    continue
                if not m.get("trades"):
                    print(f"{name:<13}{label:<8}{0:>7}")
                    continue
                print(f"{name:<13}{label:<8}{m['trades']:>7}{m['win_rate'] * 100:>6.0f}%{f(m['avg_gross_bps'], 'bps'):>7}{f(m['avg_net_bps'], 'bps'):>9}"
                      f"{f(m['profit_factor'], 'x'):>6}{f(m['pnl_usd'], 'usd'):>9}{f(m['return_on_250'], 'pct'):>9}"
                      f"{f(m['max_drawdown'], 'pct'):>8}{f(m['sharpe'], 'x'):>8}")


if __name__ == "__main__":
    main()
