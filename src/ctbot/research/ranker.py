"""Cross-sectional ranking model: Qlib-style factors, purged walk-forward ridge, TopkDropout.

What it does (all deterministic, no LLM):

1. Features: `factors.compute` (Alpha158 subset + 12-1 momentum), ranked across symbols per day.
2. Label: the return from the NEXT session's open to the open h sessions later, ranked across
   symbols. A decision made after the close of day t therefore never uses a price it could not
   have traded on.
3. Purged walk-forward: the model used on decision day i is fitted only on rows t <= i - h - 1,
   whose labels were fully known by day i's open; it is refitted every `retrain_every` days on a
   rolling `train_days` window. Every prediction is out-of-sample for the model that made it.
4. Portfolio (Qlib's TopkDropout, `qlib/contrib/strategy/signal_strategy.py`, MIT): hold the
   top k by score, replace at most `n_drop` names per rebalance, equal weight, optional SPY > 200-day
   regime filter (cash otherwise). Orders decided at the close fill at the next open, with the
   per-side costs CTbot's other backtests use (2 bps ETFs, 4 bps stocks) plus `extra_cost_bps`.
5. Evaluation on fixed chronological splits (dev / validation / locked holdout), with baselines
   on the same simulator: SPY, equal-weight universe, 12-1 momentum alone; ablations that drop
   feature groups; IC, turnover, exposure, concentration, regime breakdown; PBO across trials.

Universe caveat: the stock list is today's large caps, so results are survivorship-biased
upward. Treat absolute returns as optimistic; the comparison with the baselines (which share the
bias) is the informative part.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ctbot.research import factors as fx
from ctbot.research.backtest import ETFS

MODEL_VERSION = "ranker-1"
REPORT = Path("data/research/ranker_report.json")
MODEL_FILE = Path("data/research/ranker_model.json")
SPLITS = {"dev": ("2000-01-01", "2017-12-31"), "validation": ("2018-01-01", "2022-12-31"),
          "holdout": ("2023-01-01", "2100-01-01")}
FEATURE_GROUPS = {
    "kbar": ["KMID", "KLEN", "KSFT"],
    "short_term": ["ROC5", "MA5", "STD5", "MAX5", "LOW5", "RSV5", "CNTD5"],
    "volume": ["CORR20", "VSTD20", "VMA20"],
    "momentum": ["MOM252_21", "ROC60", "MA60"],
}


@dataclass(frozen=True)
class RankerParams:
    horizon: int = 5            # label: open(t+1) -> open(t+1+h)
    train_days: int = 756       # rolling training window (~3 years)
    retrain_every: int = 21
    ridge: float = 10.0
    top_k: int = 3              # CTbot's max_open_positions
    n_drop: int = 1
    rebalance_every: int = 5
    regime_filter: bool = False  # cash while SPY is below its 200-day average
    min_price: float = 5.0
    min_adv: float = 20e6       # 20-day average dollar volume, as RiskLimits.min_avg_dollar_volume
    extra_cost_bps: float = 0.0
    exclude_features: tuple[str, ...] = ()


@dataclass
class Arrays:
    dates: pd.DatetimeIndex
    symbols: list[str]
    open: np.ndarray
    close: np.ndarray
    features: np.ndarray        # T x N x F, ranked, NaN where unknown
    feature_names: list[str]
    raw: dict[str, np.ndarray] = field(default_factory=dict)
    eligible: np.ndarray | None = None
    regime_ok: np.ndarray | None = None


def fingerprint(panel: dict[str, pd.DataFrame]) -> str:
    h = hashlib.sha256()
    for s in sorted(panel):
        df = panel[s]
        h.update(f"{s}:{len(df)}:{df.index[0].date()}:{df.index[-1].date()}:{df['close'].sum():.4f}".encode())
    return h.hexdigest()[:16]


def prepare(panel: dict[str, pd.DataFrame], params: RankerParams, universe: list[str] | None = None) -> Arrays:
    feats = fx.compute(panel)
    cal = panel["SPY"].index if "SPY" in panel else fx.wide(panel, "close").index
    syms = [s for s in (universe or sorted(panel)) if s in panel]
    names = [k for k in feats if k not in params.exclude_features]
    o = fx.wide(panel, "open").reindex(index=cal, columns=syms)
    c = fx.wide(panel, "close").reindex(index=cal, columns=syms)
    v = fx.wide(panel, "volume").reindex(index=cal, columns=syms)
    ranked = np.stack([fx.cross_sectional_rank(feats[k].reindex(index=cal, columns=syms)).to_numpy()
                       for k in names], axis=-1)
    adv = (c * v).rolling(20, min_periods=20).mean()
    eligible = ((c >= params.min_price) & (adv >= params.min_adv)).to_numpy().copy()
    eligible &= ~np.isnan(ranked).all(axis=-1)
    regime = np.ones(len(cal), dtype=bool)
    if "SPY" in panel:
        spy = panel["SPY"]["close"].reindex(cal)
        regime = (spy > spy.rolling(200, min_periods=200).mean()).to_numpy()
    return Arrays(cal, syms, o.to_numpy(), c.to_numpy(), ranked, names,
                  raw={"MOM252_21": feats["MOM252_21"].reindex(index=cal, columns=syms).to_numpy()},
                  eligible=eligible, regime_ok=regime)


def labels(a: Arrays, h: int) -> np.ndarray:
    """Ranked forward return open(t+1) -> open(t+1+h); NaN where not yet known."""
    o = pd.DataFrame(a.open)
    fwd = o.shift(-1 - h) / o.shift(-1) - 1
    fwd = fwd.where(pd.DataFrame(a.eligible))
    return (fwd.rank(axis=1, pct=True) - 0.5).to_numpy()


def walk_forward_scores(a: Arrays, p: RankerParams, start_idx: int = 252) -> tuple[np.ndarray, list[dict]]:
    """Out-of-sample scores (T x N, NaN before the first model) and the fitted-model log."""
    y = labels(a, p.horizon)
    t_len, n, f = a.features.shape
    x = np.nan_to_num(a.features, nan=0.0)
    scores = np.full((t_len, n), np.nan)
    fits: list[dict] = []
    first = start_idx + p.train_days + p.horizon + 1
    for i in range(first, t_len, p.retrain_every):
        cut = i - p.horizon - 1                      # last row whose label is known at day i's open
        lo = max(start_idx, cut - p.train_days + 1)
        xs, ys = x[lo:cut + 1].reshape(-1, f), y[lo:cut + 1].reshape(-1)
        ok = ~np.isnan(ys) & a.eligible[lo:cut + 1].reshape(-1)
        if ok.sum() < 50 * f:
            continue
        xs, ys = xs[ok], ys[ok]
        w = np.linalg.solve(xs.T @ xs + p.ridge * len(ys) / 1000 * np.eye(f), xs.T @ ys)
        hi = min(i + p.retrain_every, t_len)
        blk = x[i:hi] @ w
        blk[~a.eligible[i:hi]] = np.nan
        scores[i:hi] = blk
        fits.append({"from": str(a.dates[lo].date()), "through": str(a.dates[cut].date()),
                     "used_from": str(a.dates[i].date()), "rows": int(ok.sum())})
    return scores, fits


def fit_final(a: Arrays, p: RankerParams) -> dict[str, Any]:
    """Model trained on the latest window, for the shadow live strategy (`ranker_signal`)."""
    y = labels(a, p.horizon)
    t_len, n, f = a.features.shape
    cut = t_len - 1 - p.horizon - 1
    lo = max(0, cut - p.train_days + 1)
    x = np.nan_to_num(a.features[lo:cut + 1], nan=0.0).reshape(-1, f)
    ys = y[lo:cut + 1].reshape(-1)
    ok = ~np.isnan(ys) & a.eligible[lo:cut + 1].reshape(-1)
    w = np.linalg.solve(x[ok].T @ x[ok] + p.ridge * ok.sum() / 1000 * np.eye(f), x[ok].T @ ys[ok])
    return {"version": MODEL_VERSION, "params": asdict(p), "features": a.feature_names,
            "weights": [round(float(v), 8) for v in w], "trained_from": str(a.dates[lo].date()),
            "trained_through": str(a.dates[cut].date()), "universe": a.symbols,
            "created": datetime.now(UTC).isoformat(timespec="seconds")}


# ---- portfolio simulation -------------------------------------------------------------------

def topk_dropout(scores: np.ndarray, held: set[int], k: int, n_drop: int) -> set[int]:
    """Qlib TopkDropout (signal_strategy.py): of the held names plus the best new ones, sell the
    held names in the bottom n_drop of the combined ranking and buy as many new names as needed
    to get back to k. Held names that lost their score (no data, illiquid) are always sold."""
    valid = [j for j in np.argsort(-np.nan_to_num(scores, nan=-np.inf)) if not np.isnan(scores[j])]
    last = [j for j in valid if j in held]                     # held, best first
    today = [j for j in valid if j not in held][: n_drop + max(0, k - len(last))]
    comb = sorted(last + today, key=lambda j: -scores[j])
    sell = {j for j in last if j in comb[-n_drop:]} if n_drop else set()
    buy = today[: len(sell) + k - len(last)]
    return (set(last) - sell) | set(buy)


def _cost(a: Arrays, j: int, p: RankerParams) -> float:
    return ((2.0 if a.symbols[j] in ETFS else 4.0) + p.extra_cost_bps) / 1e4


@dataclass
class SimResult:
    dates: list[date]
    equity: list[float]
    exposure: list[float]
    traded: float
    trades: list[dict]


def simulate(a: Arrays, p: RankerParams, scores: np.ndarray | None, mode: str = "topk",
             start_idx: int = 0) -> SimResult:
    """Decide at close i, fill at open i+1. Equity marked at each open. mode: topk | equal | spy."""
    t_len, n = a.open.shape
    shares = np.zeros(n)
    entry: dict[int, tuple[float, int]] = {}
    last_px = np.full(n, np.nan)
    cash, pending = 1.0, None
    dates, eq, expo, trades, traded = [], [], [], [], 0.0
    held: set[int] = set()
    spy = a.symbols.index("SPY") if "SPY" in a.symbols else None
    valid_rows = ~np.isnan(a.open)
    last_row = np.where(valid_rows.any(axis=0), t_len - 1 - np.argmax(valid_rows[::-1], axis=0), -1)
    for i in range(start_idx, t_len):
        px = a.open[i]
        have = ~np.isnan(px)
        last_px[have] = px[have]
        mark = np.where(have, px, last_px)
        equity = cash + float(np.nansum(shares * mark))
        if pending is not None:
            for j in range(n):
                tgt = pending.get(j, 0.0) * equity
                cur = shares[j] * mark[j] if shares[j] else 0.0
                if abs(tgt - cur) < 1e-9 * max(equity, 1):
                    continue
                if not have[j]:
                    if not (tgt == 0 and shares[j] and last_row[j] < i):
                        continue
                    px = px.copy()
                    px[j] = last_px[j]  # data ended (delisted / renamed): exit at the last price
                c = _cost(a, j, p)
                delta = tgt - cur
                cash -= delta + abs(delta) * c
                traded += abs(delta)
                if delta > 0 and shares[j] == 0:
                    entry[j] = (px[j] * (1 + c), i)
                shares[j] += delta / px[j]
                if tgt == 0:
                    shares[j] = 0.0
                    if j in entry:
                        e_px, e_i = entry.pop(j)
                        trades.append({"symbol": a.symbols[j], "net_ret": px[j] * (1 - c) / e_px - 1,
                                       "days": i - e_i, "exit": str(a.dates[i].date())})
            held = {j for j in range(n) if shares[j] > 0}
            pending = None
            equity = cash + float(np.nansum(shares * mark))
        dates.append(a.dates[i].date())
        eq.append(equity)
        expo.append(1 - cash / equity if equity else 0.0)
        if i + 1 >= t_len or (i - start_idx) % p.rebalance_every:
            continue
        if mode == "spy":
            pending = {spy: 1.0}
        elif p.regime_filter and not a.regime_ok[i]:
            pending = {}
        elif mode == "equal":
            names = [j for j in range(n) if a.eligible[i, j] and a.symbols[j] not in ETFS]
            pending = {j: 1.0 / len(names) for j in names} if names else {}
        else:
            row = scores[i].copy()
            row[[a.symbols[j] in ETFS for j in range(n)]] = np.nan  # rank single stocks only
            if np.isnan(row).all():
                continue
            keep = topk_dropout(row, held, p.top_k, p.n_drop)
            pending = {j: 1.0 / len(keep) for j in keep} if keep else {}
    return SimResult(dates, eq, expo, traded, trades)


# ---- metrics --------------------------------------------------------------------------------

def metrics(r: SimResult, lo: str, hi: str, regime: dict[date, bool] | None = None) -> dict[str, Any] | None:
    idx = [k for k, d in enumerate(r.dates) if lo <= d.isoformat() <= hi]
    if len(idx) < 60:
        return None
    eq = np.array([r.equity[k] for k in idx])
    rets = eq[1:] / eq[:-1] - 1
    yrs = max((r.dates[idx[-1]] - r.dates[idx[0]]).days / 365.25, 1e-9)
    sd = rets.std(ddof=1)
    down = np.sqrt(np.mean(np.minimum(rets, 0) ** 2))
    peak = np.maximum.accumulate(eq)
    trades = [t for t in r.trades if lo <= t["exit"] <= hi]
    tr = np.array([t["net_ret"] for t in trades]) if trades else np.array([])
    top5 = np.sort(rets)[-5:].sum() if len(rets) >= 5 else 0.0
    out = {
        "from": r.dates[idx[0]].isoformat(), "to": r.dates[idx[-1]].isoformat(), "days": len(idx),
        "total_return": float(eq[-1] / eq[0] - 1), "cagr": float((eq[-1] / eq[0]) ** (1 / yrs) - 1),
        "vol": float(sd * math.sqrt(252)), "sharpe": float(rets.mean() / sd * math.sqrt(252)) if sd else None,
        "sortino": float(rets.mean() / down * math.sqrt(252)) if down else None,
        "max_dd": float((eq / peak - 1).min()),
        "exposure": float(np.mean([r.exposure[k] for k in idx])),
        "trades": len(trades), "win_rate": float((tr > 0).mean()) if len(tr) else None,
        "expectancy_bps": float(tr.mean() * 1e4) if len(tr) else None,
        "avg_hold_days": float(np.mean([t["days"] for t in trades])) if trades else None,
        "best5_days_share_of_log_return": float(np.log1p(np.sort(rets)[-5:]).sum() / np.log(eq[-1] / eq[0]))
        if eq[-1] > eq[0] and top5 > 0 else None,
    }
    if regime:
        for flag, name in ((True, "spy_above_200d"), (False, "spy_below_200d")):
            sel = [rets[k] for k in range(len(rets)) if regime.get(r.dates[idx[k]]) is flag]
            out[f"ann_return_{name}"] = float(np.mean(sel) * 252) if len(sel) > 20 else None
    return out


def turnover(r: SimResult) -> float:
    yrs = max((r.dates[-1] - r.dates[0]).days / 365.25, 1e-9)
    return r.traded / float(np.mean(r.equity)) / yrs


def information_coefficient(a: Arrays, scores: np.ndarray, h: int, lo: str, hi: str) -> dict[str, float | None]:
    y = labels(a, h)
    ics = []
    for i, d in enumerate(a.dates):
        ds = d.date().isoformat()
        if not (lo <= ds <= hi):
            continue
        s, l = scores[i], y[i]
        ok = ~np.isnan(s) & ~np.isnan(l)
        if ok.sum() >= 5:
            ics.append(pd.Series(s[ok]).rank().corr(pd.Series(l[ok]).rank()))
    if len(ics) < 20:
        return {"ic_mean": None, "ic_t": None, "n": len(ics)}
    arr = np.array(ics)
    # overlapping h-day labels: shrink the t-stat by sqrt(h) as a rough autocorrelation correction
    return {"ic_mean": float(arr.mean()), "ic_t": float(arr.mean() / arr.std(ddof=1) * math.sqrt(len(arr) / h)),
            "n": len(arr)}


# ---- experiment -----------------------------------------------------------------------------

def trials(base: RankerParams) -> dict[str, tuple[str, RankerParams]]:
    """Name -> (mode, params). The full list is fixed before looking at results."""
    from dataclasses import replace

    t: dict[str, tuple[str, RankerParams]] = {
        "baseline: SPY buy & hold": ("spy", base),
        "baseline: equal-weight stocks": ("equal", base),
        "baseline: 12-1 momentum top-k": ("momentum", base),
        "baseline: 12-1 momentum top-k + regime": ("momentum", replace(base, regime_filter=True)),
    }
    for k in (3, 5):
        for h in (5, 21):
            for reg in (False, True):
                p = replace(base, top_k=k, horizon=h, rebalance_every=h, regime_filter=reg)
                t[f"ranker k={k} h={h}{' +regime' if reg else ''}"] = ("ranker", p)
    for g, cols in FEATURE_GROUPS.items():
        t[f"ablation: ranker k=3 h=5 without {g}"] = ("ranker", replace(base, exclude_features=tuple(cols)))
    return t


def run(panel: dict[str, pd.DataFrame], universe: list[str], base: RankerParams | None = None,
        include_holdout: bool = False, registry=None, experiment: str = "ranker-2026-10") -> dict[str, Any]:
    base = base or RankerParams()
    fp = fingerprint(panel)
    a0 = prepare(panel, base, universe)
    regime = {d.date(): bool(ok) for d, ok in zip(a0.dates, a0.regime_ok)}
    splits = dict(SPLITS)
    if not include_holdout:
        splits.pop("holdout")
    results, daily = {}, {}
    cache: dict[tuple, Arrays] = {}
    for name, (mode, p) in trials(base).items():
        key = p.exclude_features
        a = cache.get(key) or cache.setdefault(key, prepare(panel, p, universe) if key else a0)
        scores = fits = None
        if mode == "ranker":
            scores, fits = walk_forward_scores(a, p)
        elif mode == "momentum":
            scores = np.where(a.eligible, a.raw["MOM252_21"], np.nan)
        start = 252 + base.train_days + 21 + 1  # same first day for every trial
        sim = simulate(a, p, scores, mode="topk" if mode in ("ranker", "momentum") else mode, start_idx=start)
        row: dict[str, Any] = {"mode": mode, "params": asdict(p), "turnover_per_year": turnover(sim)}
        for split, (lo, hi) in splits.items():
            row[split] = metrics(sim, lo, hi, regime)
            if scores is not None and mode == "ranker":
                row[split + "_ic"] = information_coefficient(a, scores, p.horizon, lo, hi)
        if fits:
            row["n_model_fits"] = len(fits)
        results[name] = row
        daily[name] = pd.Series(np.array(sim.equity[1:]) / np.array(sim.equity[:-1]) - 1, index=sim.dates[1:])
        if registry is not None:
            for split in splits:
                if row.get(split):
                    registry.record(experiment=experiment, strategy=name, params=asdict(p), split=split,
                                    periods={"from": row[split]["from"], "to": row[split]["to"]},
                                    metrics=row[split], data_fingerprint=fp, version=MODEL_VERSION)
    sel = _selection_stats(daily, results, registry, experiment)
    return {"generated": datetime.now(UTC).isoformat(timespec="seconds"), "data_fingerprint": fp,
            "universe_size": len(a0.symbols), "first_date": str(a0.dates[0].date()),
            "last_date": str(a0.dates[-1].date()), "splits": splits, "holdout_scored": include_holdout,
            "results": results, "selection": sel}


def _selection_stats(daily: dict[str, pd.Series], results: dict, registry, experiment: str) -> dict[str, Any]:
    from ctbot.research.lab import deflated_sharpe
    from ctbot.research.validation import benjamini_hochberg, pbo, sharpe_pvalue

    lo, hi = SPLITS["dev"][0], SPLITS["validation"][1]
    names = [n for n in daily if not n.startswith("baseline: SPY")]
    mat = pd.DataFrame({n: daily[n] for n in names})
    mat = mat[(mat.index >= date.fromisoformat(lo)) & (mat.index <= date.fromisoformat(hi))].dropna()
    out: dict[str, Any] = {"selection_period": [lo, hi], "n_trials_this_run": len(names)}
    if len(mat) < 200:
        return out
    out["pbo"] = pbo(mat.to_numpy(), n_blocks=10)
    sharpes = (mat.mean() / mat.std(ddof=1)).to_numpy()
    n_trials = max(len(names), registry.trial_count(experiment) if registry else 0)
    best = mat.columns[int(np.argmax(sharpes))]
    out["best_in_selection"] = best
    out["dsr_best"] = deflated_sharpe(list(mat[best]), n_trials, float(np.var(sharpes)))
    # Excess over equal weight, so the FDR test asks "better than owning the universe?"
    ew = mat.get("baseline: equal-weight stocks")
    if ew is not None:
        pv = {n: sharpe_pvalue((mat[n] - ew).to_numpy()) for n in mat if n != "baseline: equal-weight stocks"}
        out["excess_vs_equal_weight_pvalues"] = pv
        out["fdr_survivors_q10"] = [n for n, ok in benjamini_hochberg(pv, 0.10).items() if ok]
    return out


def default_universe() -> list[str]:
    from ctbot.research.lab import STOCKS

    return ["SPY", *STOCKS]


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward ranking-model research on cached daily bars.")
    ap.add_argument("--holdout", action="store_true",
                    help="also score the locked final holdout (once per trial; recorded)")
    ap.add_argument("--extra-cost-bps", type=float, default=0.0, help="added per side to every trade")
    ap.add_argument("--write-model", action="store_true",
                    help="write data/research/ranker_model.json for the shadow strategy")
    ap.add_argument("--no-registry", action="store_true")
    args = ap.parse_args()
    from ctbot.research.registry import Registry

    universe = default_universe()
    panel = fx.load_panel(universe)
    if "SPY" not in panel or len(panel) < 10:
        raise SystemExit("not enough cached daily bars: run ctbot-fetch-bars --interval day first")
    base = RankerParams(extra_cost_bps=args.extra_cost_bps)
    rep = run(panel, universe, base, include_holdout=args.holdout,
              registry=None if args.no_registry else Registry())
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(rep, indent=2, default=str))
    if args.write_model:
        MODEL_FILE.write_text(json.dumps(fit_final(prepare(panel, base, universe), base), indent=2))
    print(format_report(rep))


def format_report(rep: dict[str, Any]) -> str:
    lines = [f"data {rep['first_date']}..{rep['last_date']}, {rep['universe_size']} symbols, "
             f"fingerprint {rep['data_fingerprint']}; holdout scored: {rep['holdout_scored']}"]
    for split in rep["splits"]:
        lines.append(f"\n[{split}] {'name':48s} {'CAGR':>7s} {'Sharpe':>6s} {'MaxDD':>7s} {'Expo':>5s} "
                     f"{'Trades':>6s} {'Exp bps':>7s} {'IC':>6s}")
        for name, row in rep["results"].items():
            m = row.get(split)
            if not m:
                continue
            ic = (row.get(split + "_ic") or {}).get("ic_mean")
            lines.append(f"[{split}] {name:48s} {m['cagr']:7.1%} {m['sharpe'] or 0:6.2f} {m['max_dd']:7.1%} "
                         f"{m['exposure']:5.0%} {m['trades']:6d} {m['expectancy_bps'] or 0:7.1f} "
                         f"{ic if ic is not None else float('nan'):6.3f}")
    s = rep["selection"]
    if "pbo" in s:
        lines.append(f"\nPBO {s['pbo']['pbo']:.2f} over {s['pbo']['n_splits']} splits; best in selection: "
                     f"{s['best_in_selection']} (DSR {s['dsr_best']['dsr']:.2f})")
        lines.append(f"beats equal weight after FDR (q=10%): {s.get('fdr_survivors_q10') or 'none'}")
    return "\n".join(lines)
