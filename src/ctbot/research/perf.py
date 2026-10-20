"""`ctbot-perf`: how each strategy would have done over the last 3, 6 and 12 months.

For the Discover tab: one simulated curve per strategy in config/strategies.toml, as if you had
put money in it alone (the launcher's rules), plus S&P 500 and Nasdaq-100 for comparison.

- Daily models (trend cores, RSI2, turn-of-month) use the lab simulator on cached daily bars.
- Intraday models use the backtest's entry/exit rules on cached 5-minute bars (about 8 months
  of history, so they have no 12-month figure). orb_sip is a stand-in on our 88-stock list:
  the live model scans every large cap, which history can't replay.
- Costs are included; dividends are not (prices are split-adjusted closes). Past results are
  simulated, not live, and do not predict future returns.

Writes data/lab/performance.json. Reads only cached bars: no network.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ctbot.market_hours import ET
from ctbot.research import backtest as bt
from ctbot.research import bars as cache
from ctbot.research import lab

OUT = Path("data/lab/performance.json")
WINDOWS = {"3M": 91, "6M": 182, "1Y": 365}


def _window_stats(curve: list[tuple[date, float]]) -> dict[str, Any]:
    """Trailing returns and the worst drop inside each window, from a daily value curve."""
    if len(curve) < 2:
        return {}
    end_day, end_val = curve[-1]
    out: dict[str, Any] = {}
    for k, days in WINDOWS.items():
        start = end_day - timedelta(days=days)
        if curve[0][0] > start + timedelta(days=7):
            out[k] = None  # not enough history for this window
            continue
        part = [(d, v) for d, v in curve if d >= start]
        base = next((v for d, v in reversed(curve) if d <= start), part[0][1])
        peak, worst = base, 0.0
        for _, v in part:
            peak = max(peak, v)
            worst = min(worst, v / peak - 1)
        out[k] = {"return": end_val / base - 1, "worst_drop": worst}
    return out


def _thin(curve: list[tuple[date, float]], since: date) -> list[list]:
    part = [(d, v) for d, v in curve if d >= since]
    if not part:
        return []
    base = part[0][1]
    return [[d.isoformat(), round(1000 * v / base, 2)] for d, v in part]


def _daily_curve(fn, symbols: list[str], start: date) -> list[tuple[date, float]]:
    L = lab.Lab(sorted(set(symbols) | {"SPY"}))
    curve: list = []
    lab.simulate(L, fn, start, date(2100, 1, 1), capital=1000.0, curve_out=curve)
    return curve


def _intraday_curve(trades: list[bt.Trade], days: list[date], slot: float, max_open: int) -> list[tuple[date, float]]:
    """Daily value of $1,000 with `slot` of it per trade, at most `max_open` at once, one per symbol."""
    taken: list[bt.Trade] = []
    for t in sorted(trades, key=lambda t: (t.entry_ts, -t.score)):
        live = [x for x in taken if x.entry_ts <= t.entry_ts < x.exit_ts]
        if len(live) < max_open and all(x.symbol != t.symbol for x in live):
            taken.append(t)
    by_day: dict[date, float] = defaultdict(float)
    for t in taken:
        by_day[t.exit_ts.astimezone(ET).date()] += slot * t.net_ret
    value, curve = 1000.0, []
    for d in days:
        value *= 1 + by_day.get(d, 0.0)
        curve.append((d, value))
    return curve


def _orb_sip_trades(intraday: dict, stop_atr: float, top_k: int, last_entry: str) -> list[bt.Trade]:
    """The live orb_sip rules on our cached list: most active by first-bar volume vs 14 days,
    green first bar, buy through its high, stop stop_atr x ATR14, out at 15:55."""
    hh, mm = (int(x) for x in last_entry.split(":"))
    last_i = ((hh - 9) * 60 + mm - 30) // 5
    daily = {s: bt.daily_closes(s) for s in intraday}
    first_vol = {s: {d: b[0].volume for d, b in v.items() if b} for s, v in intraday.items()}
    out: list[bt.Trade] = []
    for d in sorted({d for v in intraday.values() for d in v}):
        cands = []
        for s, days in intraday.items():
            bars = days.get(d)
            if not bars or len(bars) < 78:
                continue
            prior = [first_vol[s][x] for x in sorted(first_vol[s]) if x < d][-14:]
            if len(prior) < 14 or not statistics.mean(prior):
                continue
            ds = [x for x in sorted(daily[s]) if x < d][-15:]
            if len(ds) < 15:
                continue
            trs = [max(daily[s][b].high, daily[s][a].close) - min(daily[s][b].low, daily[s][a].close)
                   for a, b in zip(ds, ds[1:])]
            if bars[0].close > bars[0].open:
                cands.append((bars[0].volume / statistics.mean(prior), s, bars, statistics.mean(trs)))
        for rvol, s, bars, atr in sorted(cands, reverse=True, key=lambda c: c[0])[:top_k]:
            trig = bars[0].high
            for i in range(1, min(last_i, 76)):
                if bars[i].high >= trig:
                    entry = max(trig, bars[i].open)
                    ts, px, why = bt.run_exit(bars, i + 1, entry, entry - stop_atr * atr, None, bt.EXIT_BAR)
                    out.append(bt.Trade("orb_sip", s, bars[i].ts, entry, ts, px, why, rvol))
                    break
    return out


_PANELS: dict[str, Any] = {}


def _rule_curve(family: str, universe: str, start: date) -> list[tuple[date, float]]:
    """A strategy-library entry, simulated by the library backtest (same exits as live)."""
    from ctbot.library import backtest as lbt
    from ctbot.library import signals as sg
    from ctbot.research import factors as fx

    for key in (universe, "SPY"):
        if key not in _PANELS:
            _PANELS[key] = fx.load_panel(sg.universe(key) if key != "SPY" else ["SPY"])
    spy = _PANELS["SPY"].get("SPY")
    if spy is None or not _PANELS[universe]:
        return []
    r, _ = lbt.simulate(_PANELS[universe], spy, sg.FAMILIES[family], etf=universe == "etfs")
    r = r[r.index >= pd.Timestamp(start)]
    eq = (1 + r).cumprod() * 1000
    return [(d.date(), float(v)) for d, v in eq.items()]


def run(strategies_file: Path, universe: list[str]) -> dict[str, Any]:
    from ctbot.strategies.registry import load_strategy_config

    cfg = load_strategy_config(strategies_file)
    today = date.today()
    warm = today - timedelta(days=400)  # start a little early so positions are on at the window start
    shown_since = today - timedelta(days=WINDOWS["1Y"])
    result: dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat(), "strategies": {}, "benchmarks": {}}

    intraday = None

    def intraday_bars():
        nonlocal intraday
        if intraday is None:
            intraday = {s: bt.by_day(cache.load("5minute", s)) for s in universe}
            intraday = {s: d for s, d in intraday.items() if d}
        return intraday

    for name, table in cfg.items():
        cls, p = str(table.get("class", name)), dict(table.get("params", {}))
        curve, kind, note = None, "simulated", ""
        try:
            if cls == "trend_core":
                sym, hold, n = p.get("symbol", "QQQ"), p.get("hold"), int(p.get("sma_days", 200))
                fn = lab.vol_capped_trend(sym, hold or sym, float(p["vol_cap"]), n) if p.get("vol_cap") \
                    else lab.trend(sym, n, hold=hold)
                curve = _daily_curve(fn, [sym, hold or sym], warm)
            elif cls == "rsi2_etf":
                curve = _daily_curve(lab.rsi2_tom(0.25, 4, use_tom=False, exclude=tuple(p.get("exclude", []))),
                                     lab.RSI2_ETFS, warm)
            elif cls == "turn_of_month":
                curve = _daily_curve(lab.rsi2_tom(0.25, 1, use_rsi2=False), ["SPY"], warm)
            elif cls in bt.STRATEGIES:
                bars = intraday_bars()
                ctx = bt.build_context(bars)
                days = sorted({d for v in bars.values() for d in v})
                trades = [t for s, dd in bars.items() for d, b in dd.items() for t in bt.STRATEGIES[cls](s, d, b, ctx)]
                curve, note = _intraday_curve(trades, days, 0.25, 4), "about 8 months of 5-minute history"
            elif cls == "rule":
                curve = _rule_curve(p["family"], p["universe"], warm)
            elif cls == "orb_sip":
                bars = intraday_bars()
                days = sorted({d for v in bars.values() for d in v})
                trades = _orb_sip_trades(bars, float(p.get("stop_atr", 0.10)), int(p.get("top_k", 20)),
                                         str(p.get("last_entry", "11:00")))
                curve, kind = _intraday_curve(trades, days, float(p.get("size_pct", 0.2)), 5), "stand-in"
                note = "simulated on our 88-stock list; the live version scans every large cap"
        except Exception as e:  # noqa: BLE001 - one strategy's data gap must not sink the report
            note = f"could not simulate: {type(e).__name__}"
        if not curve:
            result["strategies"][name] = {"kind": "none", "note": note or "no price history to simulate this one"}
            continue
        result["strategies"][name] = {"kind": kind, "note": note, "as_of": curve[-1][0].isoformat(),
                                      "windows": _window_stats(curve), "curve": _thin(curve, shown_since)}
    for label, sym in (("S&P 500", "SPY"), ("Nasdaq-100", "QQQ")):
        curve = _daily_curve(lab.hold(sym), [sym], warm)
        result["benchmarks"][sym] = {"label": label, "windows": _window_stats(curve), "curve": _thin(curve, shown_since)}
    return result


def main() -> None:
    from ctbot.config import Settings, load_universe

    argparse.ArgumentParser(description="3/6/12-month simulated performance per strategy").parse_args()
    s = Settings.from_env()
    report = run(s.strategies_file, load_universe(s.universe_file))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, default=str))
    fmt = lambda w: "  n/a " if not w else f"{w['return'] * 100:+6.1f}%"  # noqa: E731
    print(f"{'strategy':<16} {'3M':>7} {'6M':>7} {'1Y':>7}")
    for name, r in {**report["strategies"], **report["benchmarks"]}.items():
        w = r.get("windows") or {}
        print(f"{name:<16} {fmt(w.get('3M')):>7} {fmt(w.get('6M')):>7} {fmt(w.get('1Y')):>7}  {r.get('note', '')}")


if __name__ == "__main__":
    main()
