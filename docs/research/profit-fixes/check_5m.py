"""Which daily-bar assumption is closer to the truth for a 4% trail? Replays the 2026 entries
(Feb 24 to Oct 27) on real 5-minute bars and compares with the two daily-bar assumptions."""
import sys
import numpy as np
import pandas as pd
sys.path.insert(0, "docs/research/profit-fixes")
import study as S

EXIT = "4% trail (main now)"
for sn in S.SETS:
    ent, mex, maxhold = S.SETS[sn]
    mexn = mex.to_numpy(bool)
    out = {"5m": [], "neutral": [], "pess": []}
    for pess in (False, True):
        S.PESS = pess
        _, trades = S.run(sn, EXIT)
        tr = [t for t in trades if S.idx[t[0]] >= pd.Timestamp("2026-02-24")]
        out["pess" if pess else "neutral"] = [t[3] for t in tr]
        if pess:
            continue
        for i0, _, s, _, _, _ in tr:  # same entries, real intraday path
            sym = S.SYMS[s]
            try:
                b = pd.read_csv(f"data/bars_cache/5minute/{sym}.csv")
            except FileNotFoundError:
                continue
            b["d"] = pd.to_datetime(b.ts.str[:10])
            e = S.Cn[i0, s]
            stop, peak, px = e * 0.96, e, None
            for i in range(i0 + 1, S.T):
                day = b[b.d == S.idx[i]]
                for h, lo, o in zip(day.high, day.low, day.open):
                    if o <= stop or lo <= stop:
                        px = min(o, stop)
                        break
                    peak = max(peak, h)
                    stop = max(stop, peak * 0.96)
                if px is None and (mexn[i, s] or i - i0 >= maxhold):
                    px = S.Cn[i, s]
                if px is not None:
                    break
            if px is not None:
                out["5m"].append(px * (1 - S.cost[s]) / (e * (1 + S.cost[s])) - 1)
    print(f"{sn:<15}", " | ".join(f"{k}: n={len(v)} avg {np.mean(v)*1e4:+.1f} bps" for k, v in out.items()))
