"""Market regime from daily closes: a rule-based label for Claude's review context.

The label follows maverick-mcp's threshold classifier (MIT, wshobson/maverick-mcp): the
20-day price slope, normalized by price, against 0.1% a day, and 20-day annualized volatility
against 25%. Trend versus the 200-day average, the drawdown from the 252-day high and where
today's volatility sits in its own past year are added as plain facts. Pure function; no I/O.
"""

from __future__ import annotations

import numpy as np

from ctbot.analysis import stats

SLOPE_THRESHOLD = 0.001  # 0.1% of price per day
VOL_THRESHOLD = 0.25     # annualized


def classify(closes) -> dict | None:
    c = np.asarray(closes, dtype=float)
    if len(c) < 60:
        return None
    slope = float(np.polyfit(np.arange(20), c[-20:], 1)[0] / c[-1])
    vol = stats.realized_vol(c, 20)
    if slope > SLOPE_THRESHOLD and vol < VOL_THRESHOLD:
        label = "bull"
    elif slope < -SLOPE_THRESHOLD and vol > VOL_THRESHOLD:
        label = "bear"
    else:
        label = "sideways"
    out = {
        "label": label,
        "slope_20d_per_day": round(slope, 5),
        "vol_20d": round(vol, 4),
        "drawdown_from_252d_high": round(float(c[-1] / c[-252:].max() - 1), 4),
    }
    if len(c) >= 200:
        out["above_sma200"] = bool(c[-1] > stats.sma(c, 200))
    r = np.diff(np.log(c))
    if len(r) >= 252:
        window = r[-252:]
        rolling = np.array([window[i - 20:i].std(ddof=1) for i in range(20, len(window) + 1)]) * np.sqrt(252)
        out["vol_percentile_1y"] = round(float((rolling < vol).mean()), 2)
    return out
