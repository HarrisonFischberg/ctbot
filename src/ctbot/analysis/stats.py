"""Core statistics shared by price-based strategies. Pure functions; no I/O."""

from __future__ import annotations

import numpy as np


def log_prices(closes: np.ndarray) -> np.ndarray:
    return np.log(np.asarray(closes, dtype=float))


def zscore(series: np.ndarray, window: int | None = None) -> float:
    """Z-score of the last value vs. the (trailing-window) mean and std."""
    x = np.asarray(series, dtype=float)
    if window:
        x = x[-window:]
    std = x.std(ddof=1)
    if std == 0 or np.isnan(std):
        return 0.0
    return float((x[-1] - x.mean()) / std)


def total_return(closes: np.ndarray, lookback: int, skip: int = 0) -> float:
    """Return over `lookback` bars ending `skip` bars ago (e.g. 12-1 momentum)."""
    c = np.asarray(closes, dtype=float)
    end = len(c) - 1 - skip
    start = end - lookback
    if start < 0:
        return float("nan")
    return float(c[end] / c[start] - 1)


def sma(closes: np.ndarray, window: int) -> float:
    c = np.asarray(closes, dtype=float)
    return float(c[-window:].mean()) if len(c) >= window else float("nan")


def realized_vol(closes: np.ndarray, window: int = 20) -> float:
    """Annualized close-to-close volatility."""
    r = np.diff(np.log(np.asarray(closes, dtype=float)))[-window:]
    return float(r.std(ddof=1) * np.sqrt(252)) if len(r) > 1 else float("nan")


def half_life(series: np.ndarray) -> float:
    """Ornstein-Uhlenbeck half-life in bars, from AR(1) on first differences."""
    s = np.asarray(series, dtype=float)
    lagged = s[:-1]
    delta = np.diff(s)
    X = np.column_stack([np.ones_like(lagged), lagged])
    _, theta = np.linalg.lstsq(X, delta, rcond=None)[0]
    if theta >= 0:
        return float("inf")  # not mean-reverting
    return float(-np.log(2) / theta)
