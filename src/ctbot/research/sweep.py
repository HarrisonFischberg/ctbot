"""`ctbot-sweep`: vectorbt parameter sweep for the QQQ trend core, with overfitting discipline.

Sweeps the moving-average length on 2010-2019 only, picks the best in-sample Sharpe, then
reports every length out-of-sample (2020-now). A real edge shows a broad plateau of good
lengths; a lone spike is curve-fitting. Needs the `research` extra.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

OUT = Path("data/lab/sweep_trend.json")


def main() -> None:
    import numpy as np
    import pandas as pd
    import vectorbt as vbt

    from ctbot.research import bars as cache

    vbt.settings.returns["year_freq"] = "252 days"  # trading days; the default 365 inflates CAGR/Sharpe

    p = argparse.ArgumentParser(description="Sweep the trend-core moving average length")
    p.add_argument("--symbol", default="QQQ")
    p.add_argument("--lengths", default="50:301:10", help="start:stop:step")
    a = p.parse_args()
    s0, s1, st = (int(x) for x in a.lengths.split(":"))
    lengths = list(range(s0, s1, st))
    bars = cache.load("day", a.symbol)
    close = pd.Series([b.close for b in bars], index=pd.DatetimeIndex([b.ts.date() for b in bars])).sort_index()
    rows = []
    for n in lengths:
        sma = close.rolling(n).mean()
        above = (close > sma).astype(int)
        entries, exits = above.diff() == 1, above.diff() == -1
        res = {"length": n}
        for name, lo, hi in (("in_sample", "2010-11-01", "2019-12-31"), ("out_of_sample", "2020-01-01", None)):
            c = close[lo:hi]
            pf = vbt.Portfolio.from_signals(c, entries[lo:hi], exits[lo:hi], fees=0.0002, freq="1D",
                                            init_cash=5000)
            res[name] = {"sharpe": float(pf.sharpe_ratio()), "cagr": float(pf.annualized_return()),
                         "max_dd": float(pf.max_drawdown())}
        rows.append(res)
    best = max(rows, key=lambda r: r["in_sample"]["sharpe"])
    oos = np.array([r["out_of_sample"]["sharpe"] for r in rows])
    rep = {"symbol": a.symbol, "chosen_in_sample": best["length"], "rows": rows,
           "oos_sharpe_median": float(np.median(oos)), "oos_sharpe_of_chosen": best["out_of_sample"]["sharpe"]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rep, indent=2))
    print(f"{'len':>5}{'IS Sharpe':>11}{'IS CAGR':>9}{'OOS Sharpe':>12}{'OOS CAGR':>10}{'OOS DD':>8}")
    for r in rows:
        i, o = r["in_sample"], r["out_of_sample"]
        mark = "  <- chosen on IS" if r is best else ""
        print(f"{r['length']:>5}{i['sharpe']:>11.2f}{i['cagr'] * 100:>8.1f}%{o['sharpe']:>12.2f}{o['cagr'] * 100:>9.1f}%"
              f"{o['max_dd'] * 100:>7.1f}%{mark}")
    print(f"chosen on 2010-19: {best['length']}d · its 2020-26 Sharpe {best['out_of_sample']['sharpe']:.2f} "
          f"· median 2020-26 Sharpe across all lengths {np.median(oos):.2f}")


if __name__ == "__main__":
    main()
