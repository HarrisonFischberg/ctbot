"""Deterministic daily price/volume factors, point-in-time, as pure pandas.

The formulas are a small subset of Qlib's Alpha158 feature set (microsoft/qlib,
`qlib/contrib/data/loader.py`, MIT License, Copyright (c) Microsoft Corporation), rewritten for
pandas so CTbot needs no Qlib dependency. Names follow Alpha158 (ROC20 = Ref($close,20)/$close,
etc.). Every value on date t uses only bars dated t or earlier, so a feature row can be acted on
from the next session's open. `test_factors.py` checks this by truncating the history.

A panel is {symbol: DataFrame indexed by date with open, high, low, close, volume}.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from ctbot.research import bars as cache

FIELDS = ("open", "high", "low", "close", "volume")
EPS = 1e-12


def load_panel(symbols: Iterable[str], root=cache.CACHE_DIR) -> dict[str, pd.DataFrame]:
    """Daily bars from the research cache (data/bars_cache/day). Missing symbols are skipped."""
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        rows = cache.load("day", s, root)
        if not rows:
            continue
        df = pd.DataFrame(
            {f: [getattr(b, f) for b in rows] for f in FIELDS},
            index=pd.DatetimeIndex([pd.Timestamp(b.ts.date()) for b in rows], name="date"),
        )
        out[s.upper()] = df[~df.index.duplicated(keep="last")].sort_index()
    return out


def wide(panel: dict[str, pd.DataFrame], field: str) -> pd.DataFrame:
    """dates x symbols frame of one field, on the union of dates (NaN where a symbol has no bar)."""
    return pd.DataFrame({s: df[field] for s, df in panel.items()}).sort_index()


def _corr(a: pd.DataFrame, b: pd.DataFrame, n: int) -> pd.DataFrame:
    return a.rolling(n, min_periods=n).corr(b)


def compute(panel: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Factor name -> dates x symbols frame. All windows look backward only."""
    o, h, lo, c, v = (wide(panel, f) for f in FIELDS)
    ret1 = c / c.shift(1) - 1
    f: dict[str, pd.DataFrame] = {
        # k-bar shape of the latest session
        "KMID": (c - o) / o,
        "KLEN": (h - lo) / o,
        "KSFT": (2 * c - h - lo) / o,
    }
    for d in (5, 20, 60):
        f[f"ROC{d}"] = c.shift(d) / c
        f[f"MA{d}"] = c.rolling(d, min_periods=d).mean() / c
        f[f"STD{d}"] = c.rolling(d, min_periods=d).std() / c
        f[f"MAX{d}"] = h.rolling(d, min_periods=d).max() / c
        f[f"LOW{d}"] = lo.rolling(d, min_periods=d).min() / c
    for d in (5, 20):
        hi_d, lo_d = h.rolling(d, min_periods=d).max(), lo.rolling(d, min_periods=d).min()
        f[f"RSV{d}"] = (c - lo_d) / (hi_d - lo_d + EPS)
        f[f"CNTD{d}"] = (ret1 > 0).astype(float).rolling(d, min_periods=d).mean() - (
            ret1 < 0).astype(float).rolling(d, min_periods=d).mean()
    f["CORR20"] = _corr(c, np.log(v + 1), 20)
    f["VSTD20"] = v.rolling(20, min_periods=20).std() / (v + EPS)
    f["VMA20"] = v.rolling(20, min_periods=20).mean() / (v + EPS)
    # Not in Alpha158: classic 12-1 momentum (Jegadeesh-Titman) and 20-day realized volatility.
    f["MOM252_21"] = c.shift(21) / c.shift(252) - 1
    f["VOL20"] = ret1.rolling(20, min_periods=20).std() * np.sqrt(252)
    return {k: x.replace([np.inf, -np.inf], np.nan) for k, x in f.items()}


def cross_sectional_rank(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-date percentile rank centred on zero (-0.5..0.5); robust to outliers and scale."""
    return frame.rank(axis=1, pct=True) - 0.5


def stack(features: dict[str, pd.DataFrame], ranked: bool = True) -> pd.DataFrame:
    """Long frame indexed by (date, symbol), one column per factor."""
    cols = {k: (cross_sectional_rank(x) if ranked else x).stack(future_stack=True) for k, x in features.items()}
    out = pd.DataFrame(cols)
    out.index.names = ["date", "symbol"]
    return out
