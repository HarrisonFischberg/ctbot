"""Backtest of four proposed fixes (2026-11-19). Run from the ctbot repo root:

    .venv/bin/python docs/research/profit-fixes/study.py

Reads data/bars_cache/day (split-adjusted OHLC, no dividends). Every variant is declared here
before any result is read, and the count is printed, so nothing is tuned after the fact.
Selection period 2011-2019, confirmation period 2020 to the end of the data.

Rules: signals use the day's close and fill at that close (the live swing models decide
15:30-15:44 ET). A stop fills at the stop, or at the open when the day gaps below it. The
trailing high is updated after the stop check (neutral); PESS=1 updates it first (assumes
every day's high came before its low, the worst case for a trail).
Costs per side: 2 bps ETFs, 4 bps stocks. Idle cash earns nothing.
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DAY = Path("data/bars_cache/day")
OUT = Path(os.environ.get("OUT", "data/research/profit_fixes.json"))
PESS = os.environ.get("PESS") == "1"
ETF_ALL = {"SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLC",
           "SMH", "GLD", "TLT"}
RSI2_ETFS = ["SPY", "IWM", "DIA", "XLK", "XLF", "XLV", "XLY", "XLI", "XLP", "XLU", "XLE", "XLB", "SMH"]  # QQQ excluded live
IS = ("2011-01-01", "2019-12-31")
OOS = ("2020-01-01", "2100-01-01")


def load(sym: str) -> pd.DataFrame:
    d = pd.read_csv(DAY / f"{sym}.csv")
    d.index = pd.to_datetime(d["ts"].str[:10])
    return d[["open", "high", "low", "close", "volume"]]


universe = [s for s in Path("config/universe.txt").read_text().split()
            if not s.startswith("#") and s.isupper() and (DAY / f"{s}.csv").exists()]
universe = sorted(set(universe) & {p.stem for p in DAY.glob("*.csv")})
raw = {s: load(s) for s in universe}
idx = raw["SPY"].index[raw["SPY"].index >= "2010-01-04"]
idx = idx[idx <= min(raw[s].index[-1] for s in ("XLF", "AAPL"))]  # SPY/QQQ files run later than the rest
P = {k: pd.DataFrame({s: raw[s][k] for s in universe}).reindex(idx) for k in ("open", "high", "low", "close", "volume")}
O, H, L, C, V = (P[k] for k in ("open", "high", "low", "close", "volume"))
SYMS = list(C.columns)
T, N = C.shape
cost = np.array([(2.0 if s in ETF_ALL else 4.0) / 1e4 for s in SYMS])

# ---- indicators (each row uses data up to and including that day's close) -------------------
sma = lambda n: C.rolling(n).mean()  # noqa: E731
d1 = C.diff()
g, ls = d1.clip(lower=0).rolling(2).mean(), (-d1).clip(lower=0).rolling(2).mean()
rsi2 = (100 - 100 / (1 + g / ls)).where(ls > 0, 100.0).where(d1.notna().rolling(2).sum() == 2)
tr = np.maximum(H - L, np.maximum((H - C.shift()).abs(), (L - C.shift()).abs()))
atr_pct = (tr.rolling(14).mean() / C)
lp = np.log(C)
z20 = (lp - lp.rolling(20).mean()) / lp.rolling(20).std(ddof=1)
theta = lp.diff().rolling(59).cov(lp.shift()) / lp.shift().rolling(59).var()
half_life = (-math.log(2) / theta).where(theta < 0, np.inf)
rv20 = lp.diff().rolling(20).std(ddof=1) * math.sqrt(252)
s5, s50, s100, s150, s200 = sma(5), sma(50), sma(100), sma(150), sma(200)

# rsi2_etf (strategies/swing.py)
ent_rsi2 = ((C > s200) & (rsi2 < 10)).reindex(columns=SYMS, fill_value=False)
ent_rsi2.loc[:, [s for s in SYMS if s not in RSI2_ETFS]] = False
# mean_reversion (strategies/mean_reversion.py with config/strategies.toml params)
ent_mr = (z20 <= -2.0) & (C >= s100 * 0.9) & (half_life <= 20)
# momentum: top 3 by 120-day return (skipping 5 days) per unit of 20-day volatility
mret = C.shift(5) / C.shift(125) - 1
mscore = (mret / rv20).where((mret >= 0.05) & (C >= s50))
ent_mom = mscore.rank(axis=1, ascending=False, method="first") <= 3
# trend_template: stage-2 gate, breakout >= 75, top 5 by (breakout, distance above the 200-day)
gate = (C > s50) & (C > s150) & (C > s200) & (s50 > s150) & (s150 > s200) & (s200 > s200.shift(22))
brk = 50 + 25 * (V > 1.2 * V.rolling(30).mean()) + 25 * (C > 0.75 * H.rolling(252).max())
tscore = (brk * 1000 + (C / s200 - 1)).where(gate & (brk >= 75))
ent_tt = tscore.rank(axis=1, ascending=False, method="first") <= 5

# name -> (entry, model exit while held, max hold days)
SETS = {
    "rsi2_etf": (ent_rsi2, C > s5, 10),
    "mean_reversion": (ent_mr, z20 >= -0.5, 20),
    "momentum": (ent_mom, (~ent_mom) & (C < s50), 250),
    "trend_template": (ent_tt, (~ent_tt) & (C < s50), 250),
}
# exits: (initial stop kind, size, trails?)
EXITS = {
    "model exit, 10% disaster stop": ("pct", 0.10, False),
    "3.5% trail (main until Nov)": ("pct", 0.035, True),
    "4% trail (main now)": ("pct", 0.04, True),
    "8% trail (base book)": ("pct", 0.08, True),
    "2 ATR trail": ("atr", 2.0, True),
    "3 ATR trail": ("atr", 3.0, True),
    "4 ATR trail": ("atr", 4.0, True),
    "2 ATR fixed stop": ("atr", 2.0, False),
    "3 ATR fixed stop": ("atr", 3.0, False),
}
spy_ret = C["SPY"].pct_change()
FILTERS = {  # True = skip new dip-buys today
    "skip when SPY fell 2%+ today": (spy_ret <= -0.02).to_numpy()[:, None] & np.ones((1, N), bool),
    "skip when SPY 20-day vol > 25%": (rv20["SPY"] > 0.25).to_numpy()[:, None] & np.ones((1, N), bool),
    "skip when the stock's ATR is in its top fifth": (atr_pct > atr_pct.rolling(252).quantile(0.8).shift()).to_numpy(),
}
REENTRY = {  # after a stop-out, for 10 trading days
    "close above the stop-out day's high": lambda i, s, w: Cn[i, s] > w["hi"],
    "close back above the entry price": lambda i, s, w: Cn[i, s] > w["entry"],
    "SPY above its 5-day average and close above yesterday's high":
        lambda i, s, w: spy_ok[i] and Cn[i, s] > Hn[i - 1, s],
}
On, Hn, Ln, Cn, ATRn = (x.to_numpy(float) for x in (O, H, L, C, atr_pct))
spy_ok = (C["SPY"] > s5["SPY"]).to_numpy()
SLOTS = 10


def run(setname: str, exitname: str, filt: str | None = None, reentry: str | None = None, cooloff: int = 0):
    ent, mex, maxhold = SETS[setname]
    ent, mex = ent.to_numpy(bool), mex.to_numpy(bool)
    kind, size, trails = EXITS[exitname]
    skip = FILTERS[filt] if filt else None
    rule = REENTRY[reentry] if reentry else None
    pos: dict[int, dict] = {}
    watch: dict[int, dict] = {}
    block: dict[int, int] = {}
    daily, trades, w = np.zeros(T), [], 1.0 / SLOTS

    def enter(i: int, s: int, re: bool) -> None:
        px = Cn[i, s]
        dist = size if kind == "pct" else min(0.25, size * ATRn[i, s])
        pos[s] = {"e": px, "i": i, "stop": px * (1 - dist), "peak": px, "dist": dist, "re": re}
        daily[i] -= w * cost[s]

    for i in range(1, T):
        for s, p in list(pos.items()):
            o, h, lo, c, prev = On[i, s], Hn[i, s], Ln[i, s], Cn[i, s], Cn[i - 1, s]
            if np.isnan(c) or np.isnan(prev):
                continue
            if PESS and trails:
                p["peak"] = max(p["peak"], h)
                p["stop"] = max(p["stop"], p["peak"] * (1 - p["dist"]))
            px = why = None
            if o <= p["stop"]:
                px, why = o, "stop"
            elif lo <= p["stop"]:
                px, why = p["stop"], "stop"
            else:
                if trails:
                    p["peak"] = max(p["peak"], h)
                    p["stop"] = max(p["stop"], p["peak"] * (1 - p["dist"]))
                if mex[i, s] or i - p["i"] >= maxhold:
                    px, why = c, "model"
            daily[i] += w * ((px or c) / prev - 1)
            if px:
                daily[i] -= w * cost[s]
                trades.append((p["i"], i, s, px * (1 - cost[s]) / (p["e"] * (1 + cost[s])) - 1, why, p["re"]))
                del pos[s]
                if why == "stop":
                    block[s] = i + cooloff
                    if rule:
                        watch[s] = {"hi": h, "entry": p["e"], "until": i + 10}
        if rule:
            for s, wt in list(watch.items()):
                if i > wt["until"] or s in pos:
                    del watch[s]
                elif i > wt["until"] - 10 and len(pos) < SLOTS and rule(i, s, wt) and not np.isnan(ATRn[i, s]):
                    enter(i, s, True)
                    del watch[s]
        for s in np.flatnonzero(ent[i]):
            if len(pos) >= SLOTS:
                break
            if s in pos or block.get(s, -1) >= i:  # no same-day rebuy, as live outside aggressive mode
                continue
            if skip is not None and skip[i, s]:
                continue
            if np.isnan(ATRn[i, s]) or np.isnan(Cn[i, s]):
                continue
            enter(i, s, False)
    return daily, trades


def stats(daily: np.ndarray, trades: list, period: tuple[str, str]) -> dict:
    m = np.asarray((idx >= period[0]) & (idx <= period[1]))
    d = daily[m]
    tr_ = [t for t in trades if m[t[0]]]
    r = np.array([t[3] for t in tr_])
    eq = np.cumprod(1 + d)
    return {
        "n": len(r), "bps": float(r.mean() * 1e4) if len(r) else None,
        "win": float((r > 0).mean()) if len(r) else None,
        "stopped": float(np.mean([t[4] == "stop" for t in tr_])) if tr_ else None,
        "days_held": float(np.mean([t[1] - t[0] for t in tr_])) if tr_ else None,
        "ann": float(d.mean() * 252), "sharpe": float(d.mean() / d.std() * math.sqrt(252)) if d.std() else None,
        "max_dd": float((eq / np.maximum.accumulate(eq) - 1).min()),
        "re_n": sum(t[5] for t in tr_), "re_bps": float(np.mean([t[3] for t in tr_ if t[5]]) * 1e4) if any(t[5] for t in tr_) else None,
    }


def paired_t(a: np.ndarray, b: np.ndarray, period: tuple[str, str]) -> float:
    m = np.asarray((idx >= period[0]) & (idx <= period[1]))
    d = a[m] - b[m]
    return float(d.mean() / d.std() * math.sqrt(len(d))) if d.std() else 0.0


def main() -> None:
    res: dict = {"data": {"from": str(idx[0].date()), "to": str(idx[-1].date()), "symbols": N, "pessimistic": PESS},
                 "stops": [], "filters": [], "reentry": []}
    base_exit = "4% trail (main now)"
    runs = {}
    for sn in SETS:  # test 1a: 4 strategies x 9 exits
        for en in EXITS:
            runs[sn, en] = run(sn, en)
    for sn in SETS:
        b = runs[sn, base_exit][0]
        for en in EXITS:
            d, t = runs[sn, en]
            res["stops"].append({"strategy": sn, "exit": en, "is": stats(d, t, IS), "oos": stats(d, t, OOS),
                                 "t_vs_live_is": paired_t(d, b, IS), "t_vs_live_oos": paired_t(d, b, OOS)})
    for sn in ("rsi2_etf", "mean_reversion"):  # test 1b: 2 dip strategies x 3 filters x 2 exits
        for en in ("model exit, 10% disaster stop", base_exit):
            b = runs[sn, en][0]
            for fn in FILTERS:
                d, t = run(sn, en, filt=fn)
                res["filters"].append({"strategy": sn, "exit": en, "filter": fn, "is": stats(d, t, IS),
                                       "oos": stats(d, t, OOS), "t_vs_nofilter_is": paired_t(d, b, IS),
                                       "t_vs_nofilter_oos": paired_t(d, b, OOS)})
    for sn in SETS:  # test 2: 4 strategies x 2 exits x (5-day cool-off + 3 re-entry rules)
        for en in (base_exit, "3 ATR trail"):
            b = runs[sn, en][0]
            for name, kw in [("wait 5 days after a stop-out", {"cooloff": 5})] + [(k, {"reentry": k, "cooloff": 0}) for k in REENTRY]:
                d, t = run(sn, en, **kw)
                res["reentry"].append({"strategy": sn, "exit": en, "rule": name, "is": stats(d, t, IS),
                                       "oos": stats(d, t, OOS), "t_vs_none_is": paired_t(d, b, IS),
                                       "t_vs_none_oos": paired_t(d, b, OOS)})
    # pooled, pre-stated question: do the models' own exits beat the live 4% trail? (one test, not 36)
    m_is, m_oos = (np.asarray((idx >= a) & (idx <= b)) for a, b in (IS, OOS))
    res["pooled"] = {}
    for label, group in (("all four", list(SETS)), ("dip-buys", ["rsi2_etf", "mean_reversion"]),
                         ("trend-followers", ["momentum", "trend_template"])):
        for alt in ("model exit, 10% disaster stop", "8% trail (base book)", "4 ATR trail"):
            d = sum(runs[sn, alt][0] - runs[sn, base_exit][0] for sn in group) / len(group)
            res["pooled"][f"{label}: {alt} minus 4% trail"] = {
                k: {"ann_gain": float(d[m].mean() * 252), "t": float(d[m].mean() / d[m].std() * math.sqrt(m.sum()))}
                for k, m in (("is", m_is), ("oos", m_oos))}
    # test 4: is there stock-picking skill, or just market exposure? Regress each book's daily
    # return on the equal-weight universe (today's 88 names, so survivorship lifts both sides alike).
    ew = C.pct_change().mean(axis=1).fillna(0).to_numpy()
    res["swing"] = []
    for sn in SETS:
        for en in ("model exit, 10% disaster stop", base_exit):
            row = {"strategy": sn, "exit": en}
            for k, m in (("is", m_is), ("oos", m_oos)):
                y, x = runs[sn, en][0][m], ew[m]
                beta = float(np.cov(y, x)[0, 1] / x.var())
                e = y - beta * x
                row[k] = {"beta": beta, "alpha_ann": float(e.mean() * 252), "t_alpha": float(e.mean() / e.std() * math.sqrt(len(e))),
                          "universe_ann": float(x.mean() * 252)}
            res["swing"].append(row)
    res["variants"] = {"stops": len(res["stops"]), "filters": len(res["filters"]), "reentry": len(res["reentry"])}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=1))
    print(json.dumps(res["variants"]), "->", OUT)


if __name__ == "__main__":
    sys.exit(main())
