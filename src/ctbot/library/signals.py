"""Entry rules for the strategy library, on completed daily bars only.

Each family maps one symbol's daily frame (open, high, low, close, volume; index = dates) and the
SPY frame to a boolean Series: True on day t means "buy at the next session's open". A rule may
use only bars up to and including t, so the backtest and the live bot (which decides each morning
on yesterday's completed bars) see the same thing. Exits are shared by every family (see
`library.backtest`): a starting stop at 2x the 14-day average range (capped), a trailing stop, and
a family-specific longest hold.

The families are standard, published ideas, picked before any test was run; nothing here was
tuned on the data. Parameters are the textbook defaults.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Family:
    key: str
    title: str
    tagline: str
    max_hold: int          # trading days
    rule: Callable[[pd.DataFrame, pd.DataFrame], pd.Series]
    source: str


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _uptrend(d: pd.DataFrame) -> pd.Series:
    return d["close"] > _sma(d["close"], 200)


def _rsi(c: pd.Series, n: int) -> pd.Series:
    diff = c.diff()
    up = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(d: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = d["close"].shift()
    tr = pd.concat([d["high"] - d["low"], (d["high"] - prev).abs(), (d["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def high52(d, spy):
    return (d["close"] >= d["high"].rolling(252, min_periods=252).max().shift()) & _uptrend(d)


def donchian(d, spy):
    return d["close"] > d["high"].rolling(55, min_periods=55).max().shift()


def pullback5(d, spy):
    return (d["close"] / d["close"].shift(5) - 1 < -0.05) & _uptrend(d)


def bollinger(d, spy):
    m, sd = _sma(d["close"], 20), d["close"].rolling(20, min_periods=20).std()
    return (d["close"] < m - 2 * sd) & _uptrend(d)


def nr7(d, spy):
    rng = d["high"] - d["low"]
    return (rng <= rng.rolling(7, min_periods=7).min()) & (d["close"] > _sma(d["close"], 50))


def gap_reversal(d, spy):
    return (d["open"] < d["low"].shift() * 0.99) & (d["close"] > d["open"]) & _uptrend(d)


def golden_cross(d, spy):
    f, s = _sma(d["close"], 50), _sma(d["close"], 200)
    return (f > s) & (f.shift() <= s.shift())


def macd(d, spy):
    line = d["close"].ewm(span=12, adjust=False).mean() - d["close"].ewm(span=26, adjust=False).mean()
    sig = line.ewm(span=9, adjust=False).mean()
    return (line > sig) & (line.shift() <= sig.shift()) & _uptrend(d)


def three_down(d, spy):
    down = d["close"] < d["close"].shift()
    return down & down.shift(1, fill_value=False) & down.shift(2, fill_value=False) & _uptrend(d)


def volume_breakout(d, spy):
    return (d["close"] > d["high"].rolling(20, min_periods=20).max().shift()) & \
        (d["volume"] > 2 * _sma(d["volume"], 20).shift())


def rel_strength(d, spy):
    sp = spy["close"].reindex(d.index)
    edge = d["close"] / d["close"].shift(63) - sp / sp.shift(63)
    return (edge > 0.10) & (d["close"] > d["high"].rolling(20, min_periods=20).max().shift())


def rsi2(d, spy):
    return (_rsi(d["close"], 2) < 10) & _uptrend(d)


FAMILIES: dict[str, Family] = {f.key: f for f in [
    Family("high52", "52-Week High Breakout", "Buys when the price closes at a new 52-week high in an uptrend.", 40,
           high52, "George & Hwang 2004, 52-week high momentum"),
    Family("donchian", "Channel Breakout", "Buys a close above the highest high of the last 55 days (turtle rule).", 40,
           donchian, "Donchian channel / Turtle Traders system 2"),
    Family("pullback5", "Pullback in an Uptrend", "Buys a 5%+ drop over a week while the price is above its 200-day average.", 10,
           pullback5, "short-term reversal (Jegadeesh 1990; Lehmann 1990) with a trend filter"),
    Family("bollinger", "Bollinger Band Dip", "Buys a close below the lower Bollinger band while in a long uptrend.", 10,
           bollinger, "Bollinger 2001, mean reversion within trend"),
    Family("nr7", "Quiet-Day Setup (NR7)", "Buys after the narrowest daily range in 7 days, above the 50-day average.", 10,
           nr7, "Crabel 1990, narrow-range expansion"),
    Family("gap_reversal", "Gap-Down Reversal", "Buys a stock that gapped 1%+ below yesterday's low but closed up on the day.", 5,
           gap_reversal, "gap-fill / opening reversal literature"),
    Family("golden_cross", "Golden Cross", "Buys when the 50-day average crosses above the 200-day average.", 120,
           golden_cross, "classic moving-average crossover"),
    Family("macd", "MACD Turn", "Buys when MACD crosses above its signal line in an uptrend.", 20,
           macd, "Appel 1979, MACD"),
    Family("three_down", "Three Down Days", "Buys after three lower closes in a row while above the 200-day average.", 5,
           three_down, "Connors & Alvarez 2009, consecutive down days"),
    Family("volume_breakout", "Volume Breakout", "Buys a new 20-day high on more than twice the usual volume.", 20,
           volume_breakout, "price-volume breakout (Lee & Swaminathan 2000)"),
    Family("rel_strength", "Relative Strength Leader", "Buys a stock beating the S&P 500 by 10%+ over 3 months at a new 20-day high.", 40,
           rel_strength, "relative-strength momentum (Jegadeesh & Titman 1993)"),
    Family("rsi2", "2-Day RSI Dip", "Buys when the 2-day RSI drops under 10 while above the 200-day average.", 5,
           rsi2, "Connors & Alvarez 2009, RSI(2)"),
]}

UNIVERSES = {
    "etfs": ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "SMH", "GLD", "TLT"],
    "stocks": None,  # the research lab's large-cap list (ctbot.research.lab.STOCKS)
}
UNIVERSE_TITLE = {"etfs": "index & sector funds", "stocks": "large-cap stocks"}


def universe(key: str) -> list[str]:
    if key == "stocks":
        from ctbot.research.lab import STOCKS

        return list(STOCKS)
    return list(UNIVERSES[key])


def entry_name(family: str, uni: str) -> str:
    return f"lib_{family}_{uni}"
