"""`ctbot-tearsheet`: quantstats HTML tearsheets (needs the `research` extra).

  ctbot-tearsheet                         # the paper book (data/equity_history.jsonl)
  ctbot-tearsheet --lab "trend QQQ 200d"  # a lab strategy, 2010-now, benchmark QQQ
  ctbot-tearsheet --book aggressive       # one paper book (config/variants.toml), benchmark the main book
  ctbot-tearsheet --factory <candidate>   # a factory candidate's backtest, 2000-now, benchmark SPY
  ctbot-tearsheet --all                   # the main book, every paper book and every factory live pick

Tearsheets are for looking, not for gating: quantstats assumes a zero risk-free rate, so its
Sharpe differs a little from the factory's.

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


def factory_returns(name: str):
    """A factory candidate's daily returns over its whole backtest, and SPY's over the same days."""
    from ctbot.library import backtest as lbt
    from ctbot.library import factory as fy
    from ctbot.library import signals as sg
    from ctbot.research import factors as fx

    c = (fy.load_report() or {}).get("candidates", {}).get(name)
    if c is None:
        raise SystemExit(f"{name} is not in {fy.REPORT}; run ctbot-factory first")
    spy = fx.load_panel(["SPY"])["SPY"]
    spy = spy[spy.index >= "1999-01-01"]
    r, _ = lbt.simulate(fx.load_panel(sg.universe(c["universe"])), spy, fy.family_of(c),
                        etf=(c["universe"] == "etfs"), giveback=c["giveback"])
    r = r[r.index >= r.index[r.to_numpy().nonzero()[0][0]]] if r.any() else r  # from the first trade
    return r, spy["close"].pct_change().reindex(r.index).fillna(0.0)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _book(qs, name: str | None) -> Path | None:
    from ctbot.variants import ROOT_NAME

    main_h = Path("data/equity_history.jsonl")
    rets = book_returns(main_h if name is None else main_h.parent / ROOT_NAME / name / "equity_history.jsonl")
    if len(rets) < 5:
        print(f"{name or 'main'}: only {len(rets)} daily returns so far; skipped")
        return None
    out = REPORTS / ("paper_book.html" if name is None else f"book_{_slug(name)}.html")
    bench = None if name is None else book_returns(main_h).reindex(rets.index).fillna(0.0)
    qs.reports.html(rets, benchmark=bench, output=str(out), title=f"ctbot paper book: {name or 'main'}",
                    benchmark_title="main book")
    return out


def _factory(qs, name: str) -> Path:
    rets, bench = factory_returns(name)
    out = REPORTS / f"factory_{_slug(name)}.html"
    qs.reports.html(rets, benchmark=bench, output=str(out), title=f"ctbot factory: {name}", benchmark_title="SPY")
    return out


def main() -> None:
    import quantstats as qs

    p = argparse.ArgumentParser(description="quantstats tearsheet for the paper book or a lab strategy")
    p.add_argument("--lab", help="lab strategy name (see ctbot-lab)")
    p.add_argument("--book", help="paper book name (config/variants.toml)")
    p.add_argument("--factory", help="factory candidate name (data/research/factory_report.json)")
    p.add_argument("--all", action="store_true", help="main book, every paper book, every factory live pick")
    p.add_argument("--cash-apy", type=float, default=0.035)
    a = p.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    if a.lab:
        rets, bench = lab_returns(a.lab, a.cash_apy)
        out = REPORTS / f"lab_{_slug(a.lab)}.html"
        qs.reports.html(rets, benchmark=bench, output=str(out), title=f"ctbot lab: {a.lab}",
                        benchmark_title="QQQ buy & hold")
    elif a.factory:
        out = _factory(qs, a.factory)
    elif a.all:
        from ctbot.library import factory as fy
        from ctbot.variants import load

        for out in [_book(qs, None), *(_book(qs, v.name) for v in load()),
                    *(_factory(qs, n) for n in (fy.load_report() or {}).get("live", []))]:
            if out:
                print(f"written: {out}")
        return
    else:
        from ctbot.variants import load

        if a.book and a.book not in {v.name for v in load()}:
            raise SystemExit(f"no paper book called {a.book!r} in config/variants.toml")
        out = _book(qs, a.book)
        if out is None:
            raise SystemExit("try again after a few sessions")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
