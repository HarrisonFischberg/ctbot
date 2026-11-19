"""Core-position backtest (2026-11-19): the live trend-gated core against always-on index cores.
Run from the ctbot repo root. SPY/QQQ from 1999-09; QLD is the real fund from 2006-06 and
2x QQQ daily minus financing (T-bill) and a 0.95% fee before that (as research/lab.stress).
Weights decided on a close apply to the next day. 2 bps on traded value; idle cash earns T-bills.
Ten variants plus two buy-and-hold yardsticks, all declared here before any result was read."""
import json
import math

import numpy as np
import pandas as pd

from ctbot.research.lab import TBILL

ld = lambda s: pd.read_csv(f"data/bars_cache/day/{s}.csv").assign(d=lambda x: pd.to_datetime(x.ts.str[:10])).set_index("d").close  # noqa: E731
spy, qqq, qld_real = ld("SPY"), ld("QQQ"), ld("QLD")
idx = spy.index
r_spy, r_qqq = spy.pct_change().fillna(0), qqq.reindex(idx).pct_change().fillna(0)
tb = pd.Series([TBILL.get(d.year, 0.03) / 252 for d in idx], idx)
r_qld = qld_real.reindex(idx).pct_change()
r_qld = r_qld.where(r_qld.notna(), 2 * r_qqq - tb - 0.0095 / 252)
qq = qqq.reindex(idx)
on = (qq > qq.rolling(170).mean()).astype(float)                 # QQQ above its 170-day average
vol_qld = r_qld.rolling(20).std(ddof=0) * math.sqrt(252)
cap = (0.30 / vol_qld).clip(upper=1.0).fillna(1.0)               # live vol cap on the QLD core


def qld_stopped(stop: float) -> pd.Series:
    """The QLD core with the live 10%-from-entry stop: out at the close that breaches it, back in
    at the next close while the trend is still on (what the paper book did on 2026-10-30)."""
    held, entry, out, lvl = False, 0.0, [], (1 + r_qld).cumprod().to_numpy()
    for i in range(len(idx)):
        if held and (on.iloc[i] == 0 or lvl[i] <= entry * (1 - stop)):
            held = False
        elif not held and on.iloc[i] == 1:
            held, entry = True, lvl[i]
        out.append(float(held))
    return pd.Series(out, idx)


Z = pd.Series(0.0, idx)
V = {  # name -> (w_spy, w_qqq, w_qld) decided at each close
    "live: 40% QQQ + 40% QLD, both only while QQQ is above its 170-day": (Z, 0.4 * on, 0.4 * on * cap),
    "live with the 10% stop the paper book applies to QLD": (Z, 0.4 * on, 0.4 * qld_stopped(0.10) * cap),
    "always 50% SPY": (Z + 0.5, Z, Z),
    "always 50% QQQ": (Z, Z + 0.5, Z),
    "always 80% SPY": (Z + 0.8, Z, Z),
    "always 80% QQQ": (Z, Z + 0.8, Z),
    "always 40% QQQ + 40% QLD while QQQ is above its 170-day": (Z, Z + 0.4, 0.4 * on * cap),
    "always 40% QQQ + 40% more QQQ while above its 170-day": (Z, 0.4 + 0.4 * on, Z),
    "always 40% SPY + 40% QLD while QQQ is above its 170-day": (Z + 0.4, Z, 0.4 * on * cap),
    "80% QQQ only while above its 170-day (no 2x fund)": (Z, 0.8 * on, Z),
    "yardstick: 100% SPY": (Z + 1, Z, Z),
    "yardstick: 100% QQQ": (Z, Z + 1, Z),
}
PERIODS = {"1999-2009": ("1999-09-15", "2009-12-31"), "2010-2019": ("2010-01-01", "2019-12-31"),
           "2020-2026": ("2020-01-01", "2026-12-31"), "all": ("1999-09-15", "2026-12-31"),
           "Nov 5-17 2026": ("2026-11-05", "2026-11-17")}
out = {}
for name, ws in V.items():
    w = [x.shift(1).fillna(0) for x in ws]
    turn = sum(x.diff().abs().fillna(0) for x in w)
    ret = w[0] * r_spy + w[1] * r_qqq + w[2] * r_qld + (1 - sum(w)).clip(lower=0) * tb - turn * 0.0002
    gap = (1 + ret).cumprod() / (1 + r_spy).cumprod()
    out[name] = {}
    for p, (a, b) in PERIODS.items():
        d, g = ret[a:b], gap[a:b]
        eq = (1 + d).cumprod()
        out[name][p] = {
            "ret": float(eq.iloc[-1] - 1), "cagr": float(eq.iloc[-1] ** (252 / len(d)) - 1),
            "max_dd": float((eq / eq.cummax() - 1).min()), "sharpe": float((d - tb[a:b]).mean() / d.std() * math.sqrt(252)),
            "worst_3m_vs_spy": float((g / g.shift(63) - 1).min()) if len(d) > 63 else None,
            "share_10d_trailing_spy_2pts": float(((g / g.shift(10) - 1) < -0.02).mean()) if len(d) > 10 else None,
            "in_market": float(sum(x[a:b] for x in w).mean())}
import os
os.makedirs("data/research", exist_ok=True)
json.dump(out, open("data/research/profit_fixes_core.json", "w"), indent=1)
for p in PERIODS:
    print(f"\n{p}")
    for n, o in out.items():
        x = o[p]
        print(f"  {n:<68} ret {x['ret']*100:7.1f}% cagr {x['cagr']*100:6.1f}% dd {x['max_dd']*100:6.1f}% sh {x['sharpe']:5.2f} "
              f"in {x['in_market']*100:4.0f}% w3m {'' if x['worst_3m_vs_spy'] is None else round(x['worst_3m_vs_spy']*100,1)} "
              f"t10 {'' if x['share_10d_trailing_spy_2pts'] is None else round(x['share_10d_trailing_spy_2pts']*100,1)}")
# context for idea 2: what SPY does after a big down day (one look, counted)
f5 = spy.shift(-5) / spy - 1
big = r_spy <= -0.03
print("\nSPY next-5-day return after a 3%+ down day:", round(f5[big].mean() * 100, 2), "% n", int(big.sum()),
      "| all days", round(f5.mean() * 100, 2), "% | t", round((f5[big].mean() - f5.mean()) / (f5[big].std() / math.sqrt(big.sum())), 2))
