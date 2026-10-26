"""Plain-English names, descriptions and settings for each strategy, for the dashboard.

The code keeps its technical parameter names (sma_days, stop_atr, ...); this module is the one
place that turns them into words an investor would use.
"""

from __future__ import annotations

from typing import Any

FUNDS = {"QQQ": "Nasdaq-100 (QQQ)", "SPY": "S&P 500 (SPY)", "QLD": "2× Nasdaq-100 (QLD)", "IWM": "Russell 2000 (IWM)",
         "DIA": "Dow Jones (DIA)", "TQQQ": "3× Nasdaq-100 (TQQQ)"}


def _pct(v: Any) -> str:
    return f"{float(v) * 100:g}%"


def _fund(v: Any) -> str:
    return FUNDS.get(str(v), str(v))


# per parameter: (label, formatter); None hides it (sizing is set by the amount you invest)
PARAMS: dict[str, tuple[str, Any] | None] = {
    "symbol": ("Trend signal from", _fund),
    "hold": ("Fund it buys", _fund),
    "sma_days": ("Uptrend test", lambda v: f"price above its {v}-day average (~{round(int(v) / 21)} months)"),
    "size_pct": None,
    "family": None,
    "universe": ("Trades", lambda v: {"etfs": "index & sector funds", "stocks": "60 large-cap stocks"}.get(v, v)),
    "vol_cap": ("Calm-market sizing", lambda v: f"shrinks the position when yearly swings exceed {_pct(v)}"),
    "disaster_stop": ("Emergency stop-loss", lambda v: f"sells if it falls {_pct(v)} below the buy price"),
    "max_model_age_days": ("Model must be newer than", lambda v: f"{v} days (retrain monthly)"),
    "stop_atr": ("Stop-loss", lambda v: f"tight: {_pct(v)} of the stock's typical daily move"),
    "top_k": ("Stocks checked each morning", lambda v: f"the {v} most unusually active"),
    "last_entry": ("Last buy of the day", lambda v: f"{v} AM ET" if int(str(v).split(':')[0]) < 12 else f"{v} ET"),
    "rsi_below": ("Dip needed", lambda v: f"2-day RSI under {v:g} (among its weakest 2-day stretches)"),
    "max_hold_days": ("Longest hold", lambda v: f"{v} trading days"),
    "exclude": ("Skips", lambda v: ", ".join(v) if v else "nothing"),
    "symbols": ("Funds", lambda v: ", ".join(_fund(x) for x in v)),
    "min_rvol": ("Volume needed", lambda v: f"{v:g}× its usual morning volume"),
    "max_risk_pct": ("Widest stop-loss", _pct),
    "reward_risk": ("Profit target", lambda v: f"{v:g}× the amount at risk"),
    "stop_pct": ("Stop-loss", _pct),
    "max_per_day": ("Most buys per day", str),
    "min_move": ("Morning rise needed", lambda v: f"up more than {_pct(v)} from yesterday's close at 10 AM ET"),
    "min_dip": ("Dip needed", lambda v: f"{_pct(v)} below the day's average price"),
    "lookback_days": ("Looks back", lambda v: f"{v} trading days"),
    "top_n": ("Holds the top", lambda v: f"{v} stocks"),
    "min_return": ("Needs at least", lambda v: f"{_pct(v)} gain over the look-back"),
    "window_days": ("Recent window", lambda v: f"{v} days"),
    "min_actors": ("Needs at least", lambda v: f"{v} buyers"),
    "min_breakout": ("Breakout strength needed", lambda v: f"{v} of 100"),
}

STRATEGIES: dict[str, dict[str, Any]] = {
    "trend_core": {
        "title": "Nasdaq-100 Trend",
        "tagline": "Owns the Nasdaq-100 while it's in an uptrend and moves to cash when it isn't.",
        "how": ["Checks once a day at 3:30 PM ET.",
                "Buys when the price is above its long-term average, sells everything when it closes below.",
                "Follows the rule exactly, with no AI review. Sits out long downturns (it held cash for most of 2022)."],
        "risk": "Medium", "holds": "Weeks to months",
    },
    "trend_core_leveraged": {
        "title": "Nasdaq-100 Trend, 2× leveraged",
        "tagline": "Same uptrend rule, but holds a fund that moves about twice as much as the Nasdaq-100.",
        "how": ["Buys QLD (2× Nasdaq-100) while the Nasdaq-100 is in an uptrend; cash otherwise.",
                "Buys less when markets get choppy, so its swings stay about the same.",
                "Bigger gains in rallies and bigger drops in sell-offs than the plain trend."],
        "risk": "High", "holds": "Weeks to months",
    },
    "rsi2_etf": {
        "title": "Buy the Dip",
        "tagline": "Buys index and sector funds after a sharp 2-day drop, only in an uptrend, and sells on the bounce.",
        "how": ["Looks at 14 funds (S&P 500, Dow, small caps, sectors) at 3:30 PM ET.",
                "Buys one that just had one of its weakest 2-day stretches while still above its 200-day average.",
                "Sells when it closes back above its 5-day average, or after 10 trading days."],
        "risk": "Medium", "holds": "A few days",
    },
    "turn_of_month": {
        "title": "Turn of the Month",
        "tagline": "Owns the S&P 500 around the end of each month, a window when stocks have tended to rise.",
        "how": ["Buys on the second-to-last trading day of the month, at 3:30 PM ET.",
                "Sells after the third trading day of the next month.",
                "In cash the rest of the time."],
        "risk": "Lower", "holds": "About 4 days a month",
    },
    "orb_sip": {
        "title": "Stocks in Play Breakout",
        "tagline": "Each morning finds the most unusually active large-cap stocks and buys the ones breaking out.",
        "how": ["Uses Robinhood's scanner at the open to find stocks trading far more than usual.",
                "Buys when one moves above its first 5-minute high, with a tight stop-loss.",
                "Always sells by 3:50 PM ET: no overnight risk, but every round trip is a day trade."],
        "risk": "High", "holds": "Minutes to hours (same day)",
    },
    "orb": {
        "title": "Morning Breakout",
        "tagline": "Buys stocks that break above their first half-hour range on heavy volume; sells the same day.",
        "how": ["Waits for the first 30 minutes to set a range (9:30-10:00 AM ET).",
                "Buys a break above it when volume is high and the price is above the day's average price.",
                "AI reviews each idea. Stop-loss at the range low, profit target at 2× the risk."],
        "risk": "High", "holds": "Minutes to hours (same day)",
    },
    "overnight": {
        "title": "Overnight Hold",
        "tagline": "Buys strong funds in the last minutes of the day and sells at the next morning's open.",
        "how": ["Buys at about 3:55 PM ET in funds above their 50-day average.",
                "Sells at 9:35 AM ET the next day.",
                "AI reviews each idea. Not a day trade, but gaps overnight can't be stopped out."],
        "risk": "Medium", "holds": "Overnight",
    },
    "intraday_momentum": {
        "title": "Last Half-Hour Momentum",
        "tagline": "When the S&P 500 is up in the first half hour, buys it for the last half hour of the day.",
        "how": ["Checks at 10:00 AM ET whether the S&P 500 fund is above yesterday's close.",
                "If it is, buys at about 3:30 PM ET and sells at 3:55 PM ET.",
                "A fixed rule from a published study, so no AI review. 1% stop-loss."],
        "risk": "Medium", "holds": "Minutes (same day)",
    },
    "vwap_revert": {
        "title": "Dip to Average Price",
        "tagline": "Buys a stock that dips below its average price for the day and sells when it gets back.",
        "how": ["Looks for a dip at least 0.75% below the day's volume-weighted average price.",
                "Only in stocks above their 50-day average; sells at that average price or a 1.5% stop."],
        "risk": "High", "holds": "Minutes to hours (same day)",
    },
    "momentum": {"title": "Momentum Leaders", "risk": "High", "holds": "Weeks",
                 "tagline": "Buys the stocks that have gone up the most over the last ~6 months.",
                 "how": ["Ranks the watchlist by its gain over the last ~6 months, skipping the last week.",
                         "Buys only the top few, and only while each is above its 50-day average."]},
    "mean_reversion": {"title": "Bounce Back", "risk": "Medium", "holds": "Days to weeks",
                       "tagline": "Buys stocks that fell unusually far below their recent average.",
                       "how": ["Measures how far a stock has fallen below its 20-day average, in typical daily moves.",
                               "Buys a big stretch down if the stock usually snaps back within a few weeks."]},
    "congressional": {"title": "Follow Congress", "risk": "Medium", "holds": "Weeks",
                      "tagline": "Buys stocks that several members of Congress recently disclosed buying.",
                      "how": ["Reads public STOCK Act trade disclosures (needs a Quiver data key).",
                              "Buys when at least 2 members bought the same stock in the last 30 days."]},
    "institutional": {"title": "Follow the Big Funds", "risk": "Medium", "holds": "Weeks to months",
                      "tagline": "Buys stocks that several large investment firms added in their latest filings.",
                      "how": ["Reads quarterly 13F filings from large funds (needs a Quiver data key).",
                              "Buys when at least 2 firms added the same stock recently. Filings lag by weeks."]},
    "copy_trading": {"title": "Copy Top Traders", "risk": "High", "holds": "Varies",
                     "tagline": "Copies buys from the best-ranked Collective2 traders on the watchlist.",
                     "how": ["Follows the traders on the Collective2 watchlist (see Research).",
                             "Without a paid Collective2 subscription it only sees their sells, so it rarely buys."]},
    "options_flow": {"title": "Big Options Bets", "risk": "High", "holds": "Days",
                     "tagline": "Follows unusually large options purchases (needs an options-flow data feed).",
                     "how": ["Watches for large call-option purchases, $100k or more each, in the last 3 days.",
                             ("Needs an options-flow data provider (TradingFlow, tradingflow_options; the free Cboe one, "
                              "cboe_options; or a paid feed); without one it never signals.")]},
    "trend_template": {"title": "Strong Uptrends", "risk": "Medium", "holds": "Weeks",
                       "tagline": "Backs stocks in a steady long-term uptrend near their yearly high.",
                       "how": [("Needs the price above its 50-, 150- and 200-day averages, stacked in that order, "
                                "with the 200-day average still rising."),
                               "Scores extra for heavy volume and for being within 25% of the 52-week high.",
                               "Votes with other models rather than buying alone; votes to sell below the 50-day average."]},
    "ranker": {"title": "Stock Ranking Model", "risk": "Medium", "holds": "Weeks",
               "tagline": "Holds the 3 large caps a trained price-pattern model ranks highest, swapping one a week.",
               "how": [("Scores each stock on about 25 price and volume patterns (momentum, trend, volatility, "
                        "volume) with weights learned from past data, retrained on a rolling window."),
                       "Decides each Monday morning on the previous close; replaces at most one holding per week.",
                       "Does nothing until the research command has trained a model. No AI review, so no API cost."]},
}

MODES = {"trade": "Active", "shadow": "Watching", "off": "Off"}


def _library(params: dict[str, Any]) -> dict[str, Any] | None:
    from ctbot.library import signals as sg

    fam = sg.FAMILIES.get(str(params.get("family")))
    if fam is None:
        return None
    uni = str(params.get("universe"))
    return {"title": f"{fam.title} ({sg.UNIVERSE_TITLE.get(uni, uni)})", "tagline": fam.tagline,
            "how": [fam.tagline, f"Idea from: {fam.source}.",
                    "Buys at the next open after the signal, up to 5 positions at 20% each.",
                    "Starting stop 2× the stock's typical daily range (at most 8%), then a trailing stop; "
                    f"sells after {fam.max_hold} trading days at the latest.",
                    "Watch-only until its backtest (ctbot-library-research) admits it."],
            "risk": "Medium" if uni == "etfs" else "High", "holds": f"Up to {fam.max_hold} trading days"}


def describe(name: str, cls: str | None, params: dict[str, Any]) -> dict[str, Any]:
    key = cls or name
    if key == "trend_core" and params.get("hold") and params.get("hold") != params.get("symbol"):
        key = "trend_core_leveraged"
    base = _library(params) if key == "rule" else STRATEGIES.get(key)
    base = base or {"title": name.replace("_", " ").title(), "tagline": "", "how": [], "risk": "Unknown",
                    "holds": "Varies"}
    settings = []
    for k, v in params.items():
        spec = PARAMS.get(k, (k.replace("_", " ").capitalize(), lambda x: str(x)))
        if spec is None:
            continue
        label, fmt = spec
        try:
            text = fmt(v)
        except (TypeError, ValueError):
            text = str(v)
        settings.append({"label": label, "value": text})
    return {**base, "settings": settings}
