"""Backtest every strategy-library entry with the exits the live autopilot uses.

`ctbot-library-research` runs each family on each universe from the research price cache
(data/bars_cache/day) and writes data/research/library_report.json. The autopilot only trades a
library entry that this report admits.

Rules, the same for every entry (fixed before any result was seen):
* a signal on day t's close buys at day t+1's open; at most 5 positions, 20% of equity each,
  simultaneous signals ranked by 3-month return; one position per symbol;
* starting stop 2x the 14-day average true range below the fill, capped at MAX_LOSS; a trailing
  stop GIVEBACK below the highest high since entry (raised after each day, so a day's high never
  protects that same day); a gap through the stop sells at the open; the longest hold sells at the
  close of its last day (the live bot sells at 15:50 ET);
* costs 2 bps per side for funds, 4 bps for stocks; no dividends; idle cash earns nothing.

Splits match the ranker: choose on dev (to 2017) and validation (2018-2022); the holdout
(2023 onward) is scored only with --holdout, once per entry (the trial registry enforces it).
An entry is admitted when its validation excess over cash survives Benjamini-Hochberg at q=10%
across every entry in this run, its dev Sharpe is positive and it made 20+ validation trades.

Caveat: the stock universe is today's large caps (survivorship bias flatters stock entries).
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ctbot.library import signals as sg

REPORT = Path("data/research/library_report.json")
SPLITS = {"dev": ("2000-01-01", "2017-12-31"), "validation": ("2018-01-01", "2022-12-31"),
          "holdout": ("2023-01-01", "2100-01-01")}
MAX_POS, SLOT, MAX_LOSS, GIVEBACK, ATR_MULT = 5, 0.20, 0.08, 0.08, 2.0
ETF_COST, STOCK_COST = 2e-4, 4e-4
EXPERIMENT = "strategy_library"


def simulate(panel: dict[str, pd.DataFrame], spy: pd.DataFrame, family: sg.Family, etf: bool,
             giveback: float = GIVEBACK) -> tuple[pd.Series, list[dict[str, Any]]]:
    """Daily returns (on SPY's calendar) and the trade list for one entry."""
    dates = spy.index
    cost = ETF_COST if etf else STOCK_COST
    cols = {}
    for s, df in panel.items():
        d = df.reindex(dates)
        sig = family.rule(df, spy).reindex(dates).fillna(False).astype(bool)
        a = sg.atr(df).reindex(dates)
        mom = (df["close"] / df["close"].shift(63)).reindex(dates)
        cols[s] = (d["open"].to_numpy(), d["high"].to_numpy(), d["low"].to_numpy(), d["close"].to_numpy(),
                   sig.to_numpy(), a.to_numpy(), mom.to_numpy())
    cash, pos, trades = 1.0, {}, []
    equity = np.empty(len(dates))
    for i in range(len(dates)):
        # exits at today's prices
        for s in list(pos):
            o, h, lo, c = (cols[s][k][i] for k in range(4))
            p = pos[s]
            if np.isnan(o):
                continue  # no bar today (halt / delisting gap): hold
            px = None
            if o <= p["stop"]:
                px = o
            elif lo <= p["stop"]:
                px = p["stop"]
            elif p["held"] + 1 >= family.max_hold and not np.isnan(c):
                px = c  # the longest hold ends at that day's close (the live bot's 15:50 time exit)
            if px is not None:
                cash += p["qty"] * px * (1 - cost)
                trades.append({"symbol": s, "entry": p["entry"], "exit": px, "ret": px / p["entry"] * (1 - cost) ** 2 - 1,
                               "days": p["held"] + 1, "opened": str(dates[p["i"]].date()), "closed": str(dates[i].date())})
                del pos[s]
                continue
            p["held"] += 1
            p["peak"] = max(p["peak"], h)
            p["stop"] = max(p["stop"], p["peak"] * (1 - giveback))
        # entries: yesterday's signals at today's open
        if i > 0 and len(pos) < MAX_POS:
            eq = cash + sum(p["qty"] * _mark(cols[s], i, p) for s, p in pos.items())
            cands = [s for s, col in cols.items() if col[4][i - 1] and s not in pos and not np.isnan(col[0][i])
                     and not np.isnan(col[5][i - 1])]
            cands.sort(key=lambda s: -np.nan_to_num(cols[s][6][i - 1], nan=-9))
            for s in cands[: MAX_POS - len(pos)]:
                o, a, prev_c = cols[s][0][i], cols[s][5][i - 1], cols[s][3][i - 1]
                stake = min(SLOT * eq, cash)
                if stake < 0.01 * eq:
                    break
                dist = min(ATR_MULT * a / prev_c, MAX_LOSS)
                qty = stake * (1 - cost) / o
                cash -= stake
                p = {"qty": qty, "entry": o, "stop": o * (1 - dist), "peak": o, "held": 0, "i": i}
                h, lo = cols[s][1][i], cols[s][2][i]
                if lo <= p["stop"]:  # stopped out on the day it was bought (the low is assumed first)
                    cash += qty * p["stop"] * (1 - cost)
                    trades.append({"symbol": s, "entry": o, "exit": p["stop"], "days": 1,
                                   "ret": p["stop"] / o * (1 - cost) ** 2 - 1,
                                   "opened": str(dates[i].date()), "closed": str(dates[i].date())})
                    continue
                p["peak"] = max(o, h)  # the day's high after the open raises tomorrow's stop
                p["stop"] = max(p["stop"], p["peak"] * (1 - giveback))
                pos[s] = p
        equity[i] = cash + sum(p["qty"] * _mark(cols[s], i, p) for s, p in pos.items())
    eq = pd.Series(equity, index=dates)
    return eq.pct_change().fillna(0.0), trades


def _mark(col, i: int, p: dict) -> float:
    """Today's close, or the last close seen when the symbol has no bar today."""
    c = col[3][i]
    if not np.isnan(c):
        p["last"] = c
    return p.get("last", p["entry"])


def metrics(r: pd.Series, trades: list[dict[str, Any]], lo: str, hi: str) -> dict[str, Any]:
    x = r[(r.index >= lo) & (r.index <= hi)]
    t = [tr for tr in trades if lo <= tr["opened"] <= hi]
    if len(x) < 60:
        return {"days": len(x)}
    curve = (1 + x).cumprod()
    yrs = len(x) / 252
    dd = (curve / curve.cummax() - 1).min()
    sd = x.std(ddof=1)
    return {"days": len(x), "cagr": float(curve.iloc[-1] ** (1 / yrs) - 1) if curve.iloc[-1] > 0 else -1.0,
            "sharpe": float(x.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0, "max_dd": float(dd),
            "trades": len(t), "win_rate": (sum(tr["ret"] > 0 for tr in t) / len(t)) if t else None,
            "avg_trade": float(np.mean([tr["ret"] for tr in t])) if t else None,
            "exposure": float((x != 0).mean())}


def run(holdout: bool = False, registry=None) -> dict[str, Any]:
    from ctbot.research import factors as fx
    from ctbot.research.ranker import fingerprint
    from ctbot.research.validation import benjamini_hochberg, sharpe_pvalue

    spy = fx.load_panel(["SPY"]).get("SPY")
    if spy is None:
        raise SystemExit("no SPY daily bars in data/bars_cache/day: run ctbot-fetch-bars --interval day first")
    spy = spy[spy.index >= "1999-01-01"]
    splits = ["dev", "validation"] + (["holdout"] if holdout else [])
    rows, pvals, daily = {}, {}, {}
    spy_r = spy["close"].pct_change().fillna(0.0)
    bench = {k: metrics(spy_r, [], *SPLITS[k]) for k in splits}
    fps = {}
    for uni in sg.UNIVERSES:
        panel = fx.load_panel(sg.universe(uni))
        if not panel:
            continue
        fps[uni] = fingerprint(panel)
        for fam in sg.FAMILIES.values():
            name = sg.entry_name(fam.key, uni)
            r, trades = simulate(panel, spy, fam, etf=(uni == "etfs"))
            daily[name] = r
            rows[name] = {"family": fam.key, "universe": uni, "title": fam.title, "source": fam.source,
                          "max_hold": fam.max_hold, **{k: metrics(r, trades, *SPLITS[k]) for k in splits},
                          "recent_trades": trades[-5:]}
            v = r[(r.index >= SPLITS["validation"][0]) & (r.index <= SPLITS["validation"][1])]
            pvals[name] = sharpe_pvalue(v.to_numpy())
            if registry is not None:
                for k in splits:
                    registry.record(experiment=EXPERIMENT, strategy=name, split=k, data_fingerprint=fps[uni],
                                    params={"family": fam.key, "universe": uni, "max_pos": MAX_POS, "slot": SLOT,
                                            "max_loss": MAX_LOSS, "giveback": GIVEBACK, "atr_mult": ATR_MULT},
                                    periods={"from": SPLITS[k][0], "to": SPLITS[k][1]}, metrics=rows[name][k])
    survivors = benjamini_hochberg(pvals, 0.10) if pvals else {}
    admitted = []
    for name, row in rows.items():
        dev, val = row["dev"], row["validation"]
        checks = {"fdr": bool(survivors.get(name)), "dev_sharpe_positive": dev.get("sharpe", 0) > 0,
                  "enough_trades": val.get("trades", 0) >= 20}
        row["validation_pvalue"] = round(pvals[name], 5)
        row["checks"] = checks
        row["admitted"] = all(checks.values())
        if row["admitted"]:
            admitted.append(name)
    return {"generated": datetime.now(UTC).isoformat(timespec="seconds"), "splits": SPLITS, "holdout_scored": holdout,
            "data_fingerprints": fps, "rules": {"max_pos": MAX_POS, "slot": SLOT, "max_loss": MAX_LOSS,
                                                "giveback": GIVEBACK, "atr_mult": ATR_MULT},
            "benchmark_spy": bench, "entries": rows, "admitted": sorted(admitted), "n_trials": len(rows)}


def reason(report: dict[str, Any] | None, name: str) -> tuple[bool, str]:
    """(admitted, plain reason) for one library entry, for the autopilot and the page."""
    if not report or name not in report.get("entries", {}):
        return False, "not tested yet, so it can't trade (run ctbot-library-research to test it)"
    row = report["entries"][name]
    v = row.get("validation", {})
    stats = f"2018-22 test: {v.get('cagr', 0):+.1%} a year, smoothness {v.get('sharpe', 0):.2f}, {v.get('trades', 0)} trades"
    if row.get("admitted"):
        return True, f"passed its test ({stats})"
    failed = [k for k, ok in row.get("checks", {}).items() if not ok]
    words = {"fdr": "its results could be luck", "dev_sharpe_positive": "it lost money in 2000-17", "enough_trades": "too few trades to judge"}
    return False, f"failed its test: {', '.join(words.get(f, f) for f in failed)} ({stats})"


def load_report(path: Path = REPORT) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def main() -> None:
    from ctbot.research.registry import Registry

    ap = argparse.ArgumentParser(description="Backtest the strategy library (dev + validation; --holdout once)")
    ap.add_argument("--holdout", action="store_true", help="also score 2023 onward (once per entry)")
    ap.add_argument("--no-registry", action="store_true")
    args = ap.parse_args()
    rep = run(args.holdout, None if args.no_registry else Registry())
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(rep, indent=2, default=str))
    b = rep["benchmark_spy"]
    print(f"SPY: dev {b['dev'].get('cagr', 0):+.1%} Sharpe {b['dev'].get('sharpe', 0):.2f} | "
          f"validation {b['validation'].get('cagr', 0):+.1%} Sharpe {b['validation'].get('sharpe', 0):.2f}")
    print(f"{'entry':34} {'dev CAGR':>9} {'Sh':>5} {'val CAGR':>9} {'Sh':>5} {'MaxDD':>7} {'trades':>6} {'p':>7}  admitted")
    for name, r in sorted(rep["entries"].items(), key=lambda kv: -kv[1]["validation"].get("sharpe", -9)):
        d, v = r["dev"], r["validation"]
        print(f"{name:34} {d.get('cagr', 0):>+9.1%} {d.get('sharpe', 0):>5.2f} {v.get('cagr', 0):>+9.1%} "
              f"{v.get('sharpe', 0):>5.2f} {v.get('max_dd', 0):>7.1%} {v.get('trades', 0):>6} {r['validation_pvalue']:>7.4f}  "
              f"{'YES' if r['admitted'] else ''}")
    print(f"admitted {len(rep['admitted'])} of {rep['n_trials']} (BH q=10% on 2018-22, dev Sharpe > 0, 20+ trades)")
    print(f"written: {REPORT}")


if __name__ == "__main__":
    main()
