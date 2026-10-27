"""`ctbot-tearsheet`: quantstats HTML tearsheets (needs the `research` extra).

  ctbot-tearsheet                         # the paper book (data/equity_history.jsonl)
  ctbot-tearsheet --lab "trend QQQ 200d"  # a lab strategy, 2010-now, benchmark QQQ

Reports go to data/reports/; the dashboard links them.
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

REPORTS = Path("data/reports")


def _series(points):
    import pandas as pd

    s = pd.Series({pd.Timestamp(d): v for d, v in points}).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s.resample("1D").last().dropna().pct_change().dropna()


def book_returns(history: Path):
    import pandas as pd

    from ctbot.variants import _daily_returns  # leaves paper top-ups out of the returns

    return pd.Series({pd.Timestamp(d): r for d, r in _daily_returns(history).items()}, dtype=float).sort_index()


def lab_returns(name: str, cash_apy: float):
    from ctbot.research import lab as L

    strat = L.catalog()[name]
    lb = L.Lab(sorted({"SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "SMH", *L.SECTORS, *L.RSI2_ETFS, *L.GTAA,
                       *L.LEVERED, "IEF", "SHY", *L.STOCKS}))
    curve: list = []
    L.simulate(lb, strat, date(2010, 11, 1), date(2100, 1, 1), cash_apy=cash_apy, curve_out=curve)
    bench: list = []
    L.simulate(lb, L.hold("QQQ"), date(2010, 11, 1), date(2100, 1, 1), curve_out=bench)
    return _series(curve), _series(bench)


def main() -> None:
    import quantstats as qs

    p = argparse.ArgumentParser(description="quantstats tearsheet for the paper book or a lab strategy")
    p.add_argument("--lab", help="lab strategy name (see ctbot-lab)")
    p.add_argument("--cash-apy", type=float, default=0.035)
    a = p.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    if a.lab:
        rets, bench = lab_returns(a.lab, a.cash_apy)
        out = REPORTS / f"lab_{re.sub(r'[^a-z0-9]+', '_', a.lab.lower()).strip('_')}.html"
        qs.reports.html(rets, benchmark=bench, output=str(out), title=f"ctbot lab: {a.lab}",
                        benchmark_title="QQQ buy & hold")
    else:
        rets = book_returns(Path("data/equity_history.jsonl"))
        if len(rets) < 5:
            raise SystemExit(f"only {len(rets)} daily returns in the paper book so far; try again after a few sessions")
        out = REPORTS / "paper_book.html"
        qs.reports.html(rets, output=str(out), title="ctbot paper book")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
