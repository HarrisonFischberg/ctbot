"""Overfitting controls for strategy selection: PBO (CSCV), Benjamini-Hochberg and Hansen's SPA.

Idea and test design follow Vibe-Trading's `agent/src/quantlib/multipletesting.py` (MIT,
HKUDS/Vibe-Trading) and the papers it cites; the code here is a small numpy rewrite so CTbot
needs no scipy (SPA alone uses the `arch` package, imported only when called):

* Bailey, Borwein, Lopez de Prado & Zhu (2017), "The Probability of Backtest Overfitting":
  split the history into S blocks, and for every way of choosing half of them as in-sample,
  pick the best trial in-sample and look at its rank out-of-sample. PBO is the share of splits
  where that pick lands in the bottom half out-of-sample. ~0.5 means selection is luck.
* Benjamini & Hochberg (1995): false-discovery-rate control over many trials' p-values.

The Deflated Sharpe Ratio already lives in `ctbot.research.lab.deflated_sharpe`.
"""

from __future__ import annotations

import itertools
import math
from statistics import NormalDist

import numpy as np


def pbo(returns: np.ndarray, n_blocks: int = 10) -> dict[str, float]:
    """returns: T x N matrix of per-period returns, one column per trial (strategy/parameter).
    Returns {"pbo", "n_splits", "median_logit"}. Needs N >= 2 and T >= n_blocks."""
    r = np.asarray(returns, dtype=float)
    t, n = r.shape
    if n < 2 or t < n_blocks or n_blocks % 2:
        raise ValueError("need >= 2 trials, an even n_blocks and at least n_blocks rows")
    blocks = np.array_split(np.arange(t), n_blocks)
    logits = []
    for is_idx in itertools.combinations(range(n_blocks), n_blocks // 2):
        ins = np.concatenate([blocks[i] for i in is_idx])
        oos = np.concatenate([blocks[i] for i in range(n_blocks) if i not in is_idx])
        best = int(np.argmax(_sharpe(r[ins])))
        oos_sr = _sharpe(r[oos])
        # relative rank of the in-sample winner out-of-sample, in (0, 1)
        rank = (np.sum(oos_sr < oos_sr[best]) + 0.5 * (np.sum(oos_sr == oos_sr[best]) - 1) + 1) / (n + 1)
        logits.append(math.log(rank / (1 - rank)))
    arr = np.array(logits)
    return {"pbo": float(np.mean(arr <= 0)), "n_splits": len(arr), "median_logit": float(np.median(arr))}


def _sharpe(x: np.ndarray) -> np.ndarray:
    sd = x.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(sd > 0, x.mean(axis=0) / sd, 0.0)
    return s


def sharpe_pvalue(returns: np.ndarray) -> float:
    """One-sided p-value that the mean return is <= 0 (normal approximation of the t-stat)."""
    x = np.asarray(returns, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) < 3 or x.std(ddof=1) == 0:
        return 1.0
    t = x.mean() / x.std(ddof=1) * math.sqrt(len(x))
    return 1 - NormalDist().cdf(t)


def benjamini_hochberg(pvalues: dict[str, float], q: float = 0.10) -> dict[str, bool]:
    """Which trials survive FDR control at level q."""
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    m, cutoff = len(items), 0
    for k, (_, p) in enumerate(items, start=1):
        if p <= q * k / m:
            cutoff = k
    keep = {name for name, _ in items[:cutoff]}
    return {name: name in keep for name in pvalues}


def spa_pvalue(returns: np.ndarray, block: int = 10, reps: int = 1000, seed: int = 0) -> float:
    """Hansen's (2005) Superior Predictive Ability test via `arch`: p-value that none of the N
    columns (T x N daily returns) beats cash, after accounting for having tried all N. A stationary
    block bootstrap keeps volatility clustering and the trials' correlation."""
    from arch.bootstrap import SPA  # research jobs only: the trading process never imports arch

    r = np.nan_to_num(np.asarray(returns, dtype=float))
    spa = SPA(np.zeros(len(r)), -r, block_size=block, reps=reps, seed=seed)  # losses: cash 0, trials -return
    spa.compute()
    return float(spa.pvalues["consistent"])
