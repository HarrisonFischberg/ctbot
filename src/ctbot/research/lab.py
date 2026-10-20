"""Strategy lab: daily-bar portfolio strategies, one simulator, one leaderboard.

Each strategy is a function (lab, day index) -> target weights {symbol: fraction of equity}
decided on that day's close. The simulator rebalances at the close when a weight changes by
more than `band`, charges costs on traded value (2 bps/side ETFs, 4 bps stocks), and credits
idle cash at `cash_apy` (Robinhood Gold sweep is ~3.4-3.6% APY; default 0 to stay conservative).
Prices are split-adjusted closes without dividends, so every result (and SPY/QQQ buy & hold)
understates total return by the dividend yield.

`ctbot-lab` runs the catalog for an in-sample (2010-2019) and out-of-sample (2020-now)
period, ranks by out-of-sample Sharpe, and writes data/lab/leaderboard.json for the dashboard.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ctbot.research import bars as cache
from ctbot.research.backtest import cost_bps

OUT = Path("data/lab/leaderboard.json")
LAG = 0  # execution delay in days for simulate(); `ctbot-lab --lag 1` sets it
PERIODS = {"in_sample": (date(2010, 11, 1), date(2019, 12, 31)),
           "out_of_sample": (date(2020, 1, 1), date(2100, 1, 1))}
SECTORS = ["XLK", "XLF", "XLV", "XLY", "XLI", "XLP", "XLU", "XLE", "XLB"]
GTAA = ["SPY", "QQQ", "EFA", "EEM", "VNQ", "GLD", "TLT", "DBC"]
LEVERED = ["QLD", "TQQQ", "SSO", "UPRO"]
STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "V", "MA", "UNH", "XOM", "LLY",
          "JNJ", "PG", "HD", "COST", "ABBV", "MRK", "PEP", "KO", "WMT", "BAC", "CVX", "ORCL", "CRM", "AMD", "NFLX",
          "ADBE", "CSCO", "INTC", "QCOM", "TXN", "INTU", "AMAT", "MU", "LRCX", "PFE", "TMO", "ABT", "DIS", "NKE",
          "MCD", "SBUX", "LOW", "CAT", "DE", "GE", "BA", "HON", "UPS", "GS", "MS", "C", "WFC", "SCHW", "BLK", "ISRG"]
RSI2_ETFS = ["SPY", "IWM", "DIA", "XLK", "XLF", "XLV", "XLY", "XLI", "XLP", "XLU", "XLE", "XLB", "SMH"]


class Lab:
    """Aligned daily closes for a symbol set (trading days = SPY's)."""

    def __init__(self, symbols: list[str]) -> None:
        raw = {s: {b.ts.date(): b.close for b in cache.load("day", s)} for s in symbols}
        self.days = [d for d in sorted(raw.get("SPY", {})) if d >= date(2010, 1, 1)]  # common history
        self.px: dict[str, list[float | None]] = {s: [raw[s].get(d) for d in self.days] for s in symbols}
        self.state: dict[str, Any] = {}

    def closes(self, s: str, i: int, n: int) -> list[float] | None:
        w = self.px[s][max(0, i - n + 1): i + 1]
        return w if len(w) == n and all(x is not None for x in w) else None

    def sma(self, s: str, i: int, n: int) -> float | None:
        w = self.closes(s, i, n)
        return sum(w) / n if w else None

    def ret(self, s: str, i: int, n: int) -> float | None:
        w = self.closes(s, i, n + 1)
        return w[-1] / w[0] - 1 if w else None

    def vol(self, s: str, i: int, n: int) -> float | None:
        w = self.closes(s, i, n + 1)
        if not w:
            return None
        r = [b / a - 1 for a, b in zip(w, w[1:])]
        return statistics.pstdev(r) * math.sqrt(252)

    def month_end(self, i: int) -> bool:
        return i + 1 < len(self.days) and self.days[i + 1].month != self.days[i].month


def _rsi2(c: list[float]) -> float:
    g = [max(c[k] - c[k - 1], 0) for k in (-2, -1)]
    l_ = [max(c[k - 1] - c[k], 0) for k in (-2, -1)]
    return 100.0 if sum(l_) == 0 else 100 - 100 / (1 + sum(g) / sum(l_))


# ---- strategy catalog --------------------------------------------------------------------
Weights = dict[str, float]


def hold(sym: str) -> Callable[[Lab, int], Weights]:
    return lambda lab, i: {sym: 1.0}


def trend(sym: str, n: int = 200, size: float = 1.0, hold: str | None = None) -> Callable[[Lab, int], Weights]:
    """In `hold` (default: sym) while sym closes above its n-day average; cash otherwise.
    hold="QLD"/"TQQQ" keeps the signal on QQQ but holds the 2x/3x fund (real ETF data)."""
    def f(lab: Lab, i: int) -> Weights:
        sma = lab.sma(sym, i, n)
        return {hold or sym: size} if sma and lab.px[sym][i] > sma else {}
    return f


def vol_capped_trend(signal: str, hold: str, target: float, n: int = 170) -> Callable[[Lab, int], Weights]:
    """Trend on `signal`, hold `hold` scaled so its recent volatility is about `target` (max 1)."""
    def f(lab: Lab, i: int) -> Weights:
        sma, v = lab.sma(signal, i, n), lab.vol(hold, i, 20)
        if not sma or not v or lab.px[signal][i] <= sma:
            return {}
        return {hold: min(1.0, target / v)}
    return f


def gtaa(assets: list[str], n: int = 200) -> Callable[[Lab, int], Weights]:
    """Faber's global tactical allocation: equal slices; each slice in its asset only while
    the asset is above its n-day average (checked daily here; the paper uses month-end)."""
    def f(lab: Lab, i: int) -> Weights:
        w = 1.0 / len(assets)
        return {a: w for a in assets if lab.sma(a, i, n) and lab.px[a][i] > lab.sma(a, i, n)}
    return f


def risk_parity(assets: list[str], lookback: int = 60) -> Callable[[Lab, int], Weights]:
    """Monthly inverse-volatility weights (fully invested)."""
    def f(lab: Lab, i: int) -> Weights:
        key = f"rp{id(assets)}"
        if lab.month_end(i) or key not in lab.state:
            inv = {a: 1 / v for a in assets if (v := lab.vol(a, i, lookback))}
            tot = sum(inv.values())
            lab.state[key] = {a: x / tot for a, x in inv.items()} if tot else {}
        return lab.state[key]
    return f


def stock_momentum(top: int = 10, lookback: int = 126, skip: int = 21) -> Callable[[Lab, int], Weights]:
    """Monthly: top-N of today's large caps by 6-month return skipping the last month, only
    while SPY is above its 200-day. SURVIVORSHIP-BIASED: the list is today's winners."""
    def f(lab: Lab, i: int) -> Weights:
        if lab.month_end(i) or "sm" not in lab.state:
            spy_ok = lab.sma("SPY", i, 200) and lab.px["SPY"][i] > lab.sma("SPY", i, 200)
            ranked = []
            for s in STOCKS:
                w = lab.closes(s, i - skip, lookback + 1) if i - skip > 0 else None
                if w:
                    ranked.append((w[-1] / w[0] - 1, s))
            ranked.sort(reverse=True)
            lab.state["sm"] = {s: 1.0 / top for _, s in ranked[:top]} if spy_ok else {}
        return lab.state["sm"]
    return f


def vol_target_trend(sym: str, target: float = 0.15, n: int = 200) -> Callable[[Lab, int], Weights]:
    """Trend filter plus volatility-managed sizing (Moreira & Muir): weight = target / 20d vol, max 1."""
    def f(lab: Lab, i: int) -> Weights:
        sma, v = lab.sma(sym, i, n), lab.vol(sym, i, 20)
        if not sma or not v or lab.px[sym][i] <= sma:
            return {}
        return {sym: min(1.0, target / v)}
    return f


def vol_target_trend_fixed(sym: str, target: float = 0.25, n: int = 200) -> Callable[[Lab, int], Weights]:
    """As vol_target_trend, but the size is set on entry and held until the trend exit
    (how the live trend_core trades: one buy, one sell, no daily resizing)."""
    def f(lab: Lab, i: int) -> Weights:
        key = f"vtf{sym}{target}"
        sma, v = lab.sma(sym, i, n), lab.vol(sym, i, 20)
        held = lab.state.get(key)
        if sma and lab.px[sym][i] > sma:
            if held is None and v:
                lab.state[key] = held = min(1.0, target / v)
            return {sym: held} if held else {}
        lab.state.pop(key, None)
        return {}
    return f


def dual_momentum(assets: list[str], lookback: int = 126) -> Callable[[Lab, int], Weights]:
    """Monthly: hold the best `lookback`-day performer if its return is positive, else cash."""
    def f(lab: Lab, i: int) -> Weights:
        key = f"dm{id(assets)}"
        if lab.month_end(i) or key not in lab.state:
            rets = [(lab.ret(a, i, lookback), a) for a in assets]
            rets = [(r, a) for r, a in rets if r is not None]
            best = max(rets) if rets else None
            lab.state[key] = {best[1]: 1.0} if best and best[0] > 0 else {}
        return lab.state[key]
    return f


def sector_rotation(top: int = 3, lookback: int = 126) -> Callable[[Lab, int], Weights]:
    """Monthly: equal-weight the top sectors by 6-month return that are above their 200-day."""
    def f(lab: Lab, i: int) -> Weights:
        if lab.month_end(i) or "sr" not in lab.state:
            ranked = sorted(((lab.ret(s, i, lookback), s) for s in SECTORS if lab.ret(s, i, lookback) is not None),
                            reverse=True)
            picks = [s for r, s in ranked[:top] if lab.sma(s, i, 200) and lab.px[s][i] > lab.sma(s, i, 200)]
            lab.state["sr"] = {s: 1.0 / top for s in picks}
        return lab.state["sr"]
    return f


def rsi2_tom(slot: float = 0.25, max_pos: int = 6, use_rsi2: bool = True, use_tom: bool = True,
             exclude: tuple[str, ...] = ()) -> Callable[[Lab, int], Weights]:
    """The live rsi2_etf + turn_of_month (SPY) models as a book of fixed-size slots
    (either half alone with use_rsi2 / use_tom)."""
    def f(lab: Lab, i: int) -> Weights:
        book: dict[str, str] = lab.state.setdefault("book", {})
        for s, why in list(book.items()):
            c = lab.closes(s, i, 5)
            if why == "rsi2" and c and c[-1] > sum(c) / 5:
                del book[s]
            elif why == "tom" and lab.state.get("tom_exit") == i:
                del book[s]
        d = lab.days[i]
        nxt = [x for x in lab.days[i + 1:i + 4]]
        if use_tom and len(nxt) >= 2 and nxt[0].month == d.month and nxt[1].month != d.month and "SPY" not in book:
            book["SPY"] = "tom"
            first_next = i + 2
            lab.state["tom_exit"] = first_next + 2
        for s in (RSI2_ETFS if use_rsi2 else []):
            if s in book or s in exclude or len(book) >= max_pos:
                continue
            c, sma = lab.closes(s, i, 3), lab.sma(s, i, 200)
            if c and sma and c[-1] > sma and _rsi2(c) < 10:
                book[s] = "rsi2"
        return {s: slot for s in book}
    return f


def blend(parts: list[tuple[float, Callable[[Lab, int], Weights]]]) -> Callable[[Lab, int], Weights]:
    def f(lab: Lab, i: int) -> Weights:
        out: Weights = {}
        for scale, part in parts:
            for s, w in part(lab, i).items():
                out[s] = out.get(s, 0.0) + scale * w
        total = sum(out.values())
        return {s: w / total for s, w in out.items()} if total > 1 else out
    return f


def catalog() -> dict[str, Callable[[Lab, int], Weights]]:
    return {
        "lev: trend QQQ 170d -> hold QLD (2x)": trend("QQQ", 170, hold="QLD"),
        "lev: trend QQQ 170d -> hold TQQQ (3x)": trend("QQQ", 170, hold="TQQQ"),
        "lev: trend SPY 200d -> hold SSO (2x)": trend("SPY", 200, hold="SSO"),
        "lev: hold QLD (2x)": hold("QLD"),
        "lev: hold TQQQ (3x)": hold("TQQQ"),
        "lev: trend QQQ 170d -> QLD capped at 30% vol": vol_capped_trend("QQQ", "QLD", 0.30),
        "lev: trend QQQ 170d -> TQQQ capped at 35% vol": vol_capped_trend("QQQ", "TQQQ", 0.35),
        "GTAA 8 assets (Faber)": gtaa(GTAA),
        "risk parity SPY/TLT/GLD/IEF": risk_parity(["SPY", "TLT", "GLD", "IEF"]),
        "dual momentum QLD/TLT/GLD": dual_momentum(["QLD", "TLT", "GLD"]),
        "stock momentum top10 + SPY trend (biased)": stock_momentum(),
        "hold SPY": hold("SPY"),
        "hold QQQ": hold("QQQ"),
        "trend SPY 200d": trend("SPY"),
        "trend QQQ 200d": trend("QQQ"),
        "LIVE core: trend QQQ 170d": trend("QQQ", 170),
        "LIVE: 40% QQQ trend + 40% QLD capped (+20% cash)": blend([(0.4, trend("QQQ", 170)),
                                                                  (0.4, vol_capped_trend("QQQ", "QLD", 0.30))]),
        "trend QQQ 100d": trend("QQQ", 100),
        "vol-target QQQ 15% + trend": vol_target_trend("QQQ", 0.15),
        "vol-target QQQ 25% + trend": vol_target_trend("QQQ", 0.25),
        "vol-target QQQ 25% + trend, sized at entry": vol_target_trend_fixed("QQQ", 0.25),
        "vol-target QQQ 20% + trend, sized at entry": vol_target_trend_fixed("QQQ", 0.20),
        "dual momentum SPY/QQQ/IWM/GLD/TLT": dual_momentum(["SPY", "QQQ", "IWM", "GLD", "TLT"]),
        "dual momentum QQQ/GLD/TLT": dual_momentum(["QQQ", "GLD", "TLT"]),
        "sector rotation top3 + trend": sector_rotation(3),
        "rsi2 + turn-of-month book": rsi2_tom(),
        "LIVE: 50% trend QQQ + rsi2/TOM": blend([(0.5, trend("QQQ")), (1.0, rsi2_tom())]),
        "80% vol-target QQQ 25% + rsi2/TOM": blend([(0.8, vol_target_trend("QQQ", 0.25)), (0.2 / 0.25, rsi2_tom())]),
        "100% vol-target QQQ 25%, idle cash -> rsi2/TOM": blend([(1.0, vol_target_trend("QQQ", 0.25)), (0.5, rsi2_tom())]),
        "50% trend QQQ + 50% dual momentum": blend([(0.5, trend("QQQ")), (0.5, dual_momentum(["SPY", "QQQ", "IWM", "GLD", "TLT"]))]),
    }


# ---- simulator ---------------------------------------------------------------------------

@dataclass
class Result:
    final: float
    cagr: float
    vol: float
    sharpe: float | None
    sortino: float | None
    max_dd: float
    calmar: float | None
    exposure: float
    turnover: float
    worst_year: float
    pct_up_months: float


def simulate(lab: Lab, strat: Callable[[Lab, int], Weights], start: date, end: date,
             capital: float = 5000.0, cash_apy: float = 0.0, band: float = 0.02,
             curve_out: list | None = None, lag: int | None = None) -> Result:
    """lag=0 trades at the close the signal was computed on (the live models decide 15:30-15:44 ET
    on the live price, so this is close to how they trade); lag=1 trades at the next close, a
    stricter check that the result does not depend on same-close execution."""
    lag = LAG if lag is None else lag
    queue: list[Weights] = []
    lab.state = {}
    idx = [i for i, d in enumerate(lab.days) if start <= d <= end and i >= 200]
    shares: dict[str, float] = {}
    cash, curve, expo, traded = capital, [], [], 0.0
    daily_cash = (1 + cash_apy) ** (1 / 252) - 1
    for i in idx:
        px = {s: lab.px[s][i] for s in lab.px if lab.px[s][i] is not None}
        cash *= 1 + daily_cash
        eq = cash + sum(q * px.get(s, 0) for s, q in shares.items())
        queue.append(strat(lab, i))
        if len(queue) <= lag:
            curve.append((lab.days[i], eq))
            expo.append(1 - cash / eq if eq else 0)
            continue
        target = queue.pop(0)
        for s in set(shares) | set(target):
            if s not in px:
                continue
            want = target.get(s, 0.0) * eq
            have = shares.get(s, 0.0) * px[s]
            if abs(want - have) <= band * eq and not (want == 0 and have > 0):
                continue
            delta = want - have
            c = abs(delta) * cost_bps(s) / 1e4
            cash -= delta + c
            traded += abs(delta)
            shares[s] = shares.get(s, 0.0) + delta / px[s]
            if shares[s] * px[s] < 1e-6:
                shares.pop(s)
        eq = cash + sum(q * px.get(s, 0) for s, q in shares.items())
        curve.append((lab.days[i], eq))
        expo.append(1 - cash / eq if eq else 0)
    if curve_out is not None:
        curve_out.extend(curve)
    yrs = max((curve[-1][0] - curve[0][0]).days / 365.25, 1e-9)
    rets = [b[1] / a[1] - 1 for a, b in zip(curve, curve[1:])]
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0
    down = statistics.pstdev([min(r, 0) for r in rets]) if rets else 0
    peak, mdd = 0.0, 0.0
    for _, v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    years, months = {}, {}
    for d, v in curve:
        years.setdefault(d.year, [v, v])[1] = v
        months.setdefault((d.year, d.month), [v, v])[1] = v
    yr = [b / a - 1 for a, b in years.values()]
    mo = [b / a - 1 for a, b in months.values()]
    cagr = (curve[-1][1] / capital) ** (1 / yrs) - 1
    mean = statistics.mean(rets) if rets else 0
    return Result(
        final=curve[-1][1], cagr=cagr, vol=sd * math.sqrt(252),
        sharpe=mean / sd * math.sqrt(252) if sd else None,
        sortino=mean / down * math.sqrt(252) if down else None,
        max_dd=mdd, calmar=cagr / -mdd if mdd < 0 else None,
        exposure=statistics.mean(expo), turnover=traded / capital / yrs,
        worst_year=min(yr) if yr else 0.0, pct_up_months=sum(m > 0 for m in mo) / len(mo) if mo else 0.0,
    )


def run(cash_apy: float = 0.0) -> dict[str, Any]:
    syms = sorted({"SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "SMH", *SECTORS, *RSI2_ETFS, *GTAA, *LEVERED,
                   "IEF", "SHY", *STOCKS})
    lab = Lab(syms)
    rows, full_rets = [], {}
    for name, strat in catalog().items():
        row: dict[str, Any] = {"strategy": name}
        for p, (s, e) in PERIODS.items():
            row[p] = simulate(lab, strat, s, e, cash_apy=cash_apy).__dict__
        curve: list = []
        simulate(lab, strat, date(2010, 11, 1), date(2100, 1, 1), cash_apy=cash_apy, curve_out=curve)
        full_rets[name] = [b[1] / a[1] - 1 for a, b in zip(curve, curve[1:])]
        row["curve"] = [[d.isoformat(), round(v, 2)] for d, v in curve[::5]] + [[curve[-1][0].isoformat(), round(curve[-1][1], 2)]]
        rows.append(row)
    # Deflated Sharpe over the whole catalog: how likely is each result to beat the best of
    # this many tries by pure luck?
    srs = [statistics.mean(r) / statistics.pstdev(r) for r in full_rets.values() if statistics.pstdev(r)]
    sr_var = statistics.pvariance(srs) if len(srs) > 1 else 0.0
    bench = full_rets["hold QQQ"]
    excess = {k: [a - b for a, b in zip(v, bench)] for k, v in full_rets.items() if k != "hold QQQ"}
    ex_srs = [statistics.mean(r) / statistics.pstdev(r) for r in excess.values() if statistics.pstdev(r)]
    ex_var = statistics.pvariance(ex_srs) if len(ex_srs) > 1 else 0.0
    for row in rows:
        row["deflated"] = deflated_sharpe(full_rets[row["strategy"]], len(rows), sr_var)
        # vs QQQ: does the strategy's daily excess over holding QQQ survive the multiple-testing haircut?
        row["deflated_vs_qqq"] = (deflated_sharpe(excess[row["strategy"]], len(excess), ex_var)
                                  if row["strategy"] in excess else None)
    rows.sort(key=lambda r: r["out_of_sample"]["sharpe"] or -9, reverse=True)
    pbo_stats = None
    n = min(len(v) for v in full_rets.values())
    if n > 500 and len(full_rets) > 2:
        import numpy as np

        from ctbot.research.validation import pbo

        pbo_stats = pbo(np.array([v[-n:] for v in full_rets.values()]).T, n_blocks=10)
    wf = {
        "trend QQQ (length)": walk_forward(lab, lambda n: trend("QQQ", n), list(range(100, 301, 20)), cash_apy=cash_apy),
        "trend QQQ -> QLD 30% cap (length)": walk_forward(lab, lambda n: vol_capped_trend("QQQ", "QLD", 0.30, n),
                                                          list(range(100, 301, 20)), cash_apy=cash_apy),
    }
    return {"walk_forward": wf, "stress": stress(), "pbo": pbo_stats, "lag_days": LAG,
            "generated_at": datetime.now(timezone.utc).isoformat(), "cash_apy": cash_apy,
            "periods": {p: [s.isoformat(), min(e, lab.days[-1]).isoformat()] for p, (s, e) in PERIODS.items()},
            "note": "daily closes, split-adjusted, dividends excluded; costs 2 bps/side ETFs",
            "rows": rows}


def main() -> None:
    p = argparse.ArgumentParser(description="Rank portfolio strategies on cached daily bars")
    p.add_argument("--cash-apy", type=float, default=0.0, help="yield on idle cash, e.g. 0.035")
    p.add_argument("--lag", type=int, default=0, help="trade N closes after the signal (1 = stricter check)")
    a = p.parse_args()
    global LAG
    LAG = a.lag
    rep = run(a.cash_apy)
    out = OUT if LAG == 0 else OUT.with_name(f"leaderboard_lag{LAG}.json")  # the dashboard reads OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(f"cash yield {a.cash_apy:.1%} · ranked by out-of-sample Sharpe · dividends excluded")
    print(f"{'strategy':<46}{'OOS CAGR':>9}{'MaxDD':>8}{'Sharpe':>7}{'DSR':>6}{'vsQQQ':>6} |{'IS CAGR':>8}{'MaxDD':>8}{'Sharpe':>7}")
    for r in rep["rows"]:
        o, i = r["out_of_sample"], r["in_sample"]
        print(f"{r['strategy'][:45]:<46}{o['cagr'] * 100:>8.1f}%{o['max_dd'] * 100:>7.1f}%{o['sharpe'] or 0:>7.2f}"
              f"{r['deflated']['dsr']:>6.2f}{(r['deflated_vs_qqq'] or {}).get('psr0', float('nan')):>6.2f}"
              f" |{i['cagr'] * 100:>7.1f}%{i['max_dd'] * 100:>7.1f}%{i['sharpe'] or 0:>7.2f}")
    print("DSR = probability the 2010-26 Sharpe beats the best of this many tries by luck (>0.95 strong)")
    print("vsQQQ = probability the strategy's daily excess return over holding QQQ is truly positive")
    for wname, srows in rep["stress"].items():
        print(f"stress {wname}: " + " | ".join(f"{r['strategy']} {r['cagr'] * 100:+.1f}%/{r['max_dd'] * 100:.0f}%" for r in srows))
    for name, w in rep["walk_forward"].items():
        print(f"walk-forward {name}: {w['from']}..{w['to']} CAGR {w['cagr'] * 100:+.1f}% maxDD {w['max_dd'] * 100:.1f}% "
              f"Sharpe {w['sharpe'] or 0:.2f} · picks {w['picks']}")
    if rep.get("pbo"):
        print(f"PBO across the catalog: {rep['pbo']['pbo']:.2f} ({rep['pbo']['n_splits']} splits; ~0.5 = picking the "
              f"top Sharpe here is luck)")
    print(f"written: {out}")


if __name__ == "__main__":
    main()


# ---- robustness analytics ------------------------------------------------------------------

def deflated_sharpe(returns: list[float], n_trials: int, sr_var: float) -> dict[str, float]:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014): the probability that the true
    Sharpe exceeds what the best of `n_trials` unskilled strategies would show by luck.
    returns are daily; sr_var is the variance of the daily Sharpe estimates across trials."""
    from statistics import NormalDist

    nd = NormalDist()
    t = len(returns)
    mu, sd = statistics.mean(returns), statistics.pstdev(returns)
    if sd == 0 or t < 30:
        return {"psr0": 0.0, "dsr": 0.0, "sr_star_annual": 0.0}
    sr = mu / sd
    skew = sum(((r - mu) / sd) ** 3 for r in returns) / t
    kurt = sum(((r - mu) / sd) ** 4 for r in returns) / t
    gamma = 0.5772156649
    n = max(n_trials, 2)
    sr_star = math.sqrt(max(sr_var, 0.0)) * ((1 - gamma) * nd.inv_cdf(1 - 1 / n) + gamma * nd.inv_cdf(1 - 1 / (n * math.e)))
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr ** 2, 1e-12))

    def psr(threshold: float) -> float:
        return nd.cdf((sr - threshold) * math.sqrt(t - 1) / denom)

    return {"psr0": psr(0.0), "dsr": psr(sr_star), "sr_star_annual": sr_star * math.sqrt(252)}


def walk_forward(lab: Lab, family: Callable[[int], Callable[[Lab, int], Weights]], grid: list[int],
                 first_year: int = 2015, train_years: int = 5, cash_apy: float = 0.0) -> dict[str, Any]:
    """Each year, pick the grid value with the best Sharpe over the previous `train_years`,
    then trade that year with it. Years are chained (equity carried over; positions re-entered
    at the start of each year). Every traded year is out-of-sample for its choice."""
    last = lab.days[-1].year
    eq, picks, curve = 5000.0, {}, []
    for y in range(first_year, last + 1):
        tr_s, tr_e = date(y - train_years, 1, 1), date(y - 1, 12, 31)
        scores = []
        for g in grid:
            r = simulate(lab, family(g), tr_s, tr_e, cash_apy=cash_apy)
            scores.append((r.sharpe or -9, g))
        best = max(scores)[1]
        picks[y] = best
        seg: list = []
        simulate(lab, family(best), date(y, 1, 1), date(y, 12, 31), capital=eq, cash_apy=cash_apy, curve_out=seg)
        if seg:
            curve += seg
            eq = seg[-1][1]
    yrs = (curve[-1][0] - curve[0][0]).days / 365.25
    peak, mdd = 0.0, 0.0
    for _, v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rets = [b[1] / a[1] - 1 for a, b in zip(curve, curve[1:])]
    sd = statistics.pstdev(rets)
    return {"picks": picks, "cagr": (eq / 5000) ** (1 / yrs) - 1, "max_dd": mdd,
            "sharpe": statistics.mean(rets) / sd * math.sqrt(252) if sd else None,
            "from": curve[0][0].isoformat(), "to": curve[-1][0].isoformat()}


# ---- crash stress test (synthetic leverage from QQQ, 1999-) --------------------------------
TBILL = {1999: .046, 2000: .058, 2001: .034, 2002: .016, 2003: .010, 2004: .014, 2005: .032, 2006: .047,
         2007: .044, 2008: .014, 2009: .0015, 2010: .0014, 2011: .0005, 2012: .0009, 2013: .0006, 2014: .0003,
         2015: .0005, 2016: .0032, 2017: .0093, 2018: .0194, 2019: .0206, 2020: .0037, 2021: .0005, 2022: .0202,
         2023: .0507, 2024: .0497, 2025: .042, 2026: .038}  # approximate 3-month T-bill yields


def stress(windows: dict[str, tuple[str, str]] | None = None) -> dict[str, Any]:
    """Trend family on QQQ from 1999 with synthetic L-x daily returns (L*r - (L-1)*T-bill -
    0.95%/yr fee); covers the 2000-02 crash and 2008 that the real 2x/3x funds' 2010+ data lacks.
    Signal on the prior close; idle cash earns the T-bill rate."""
    windows = windows or {"1999-2009 crashes": ("1999-09-15", "2009-12-31"), "1999-2026 full": ("1999-09-15", "2100-01-01")}
    bars_ = sorted(cache.load("day", "QQQ"), key=lambda b: b.ts)
    days, c = [b.ts.date() for b in bars_], [b.close for b in bars_]
    tests = {"hold QQQ": (1, None, None), "trend QQQ 170d": (1, 170, None), "hold 3x (no filter)": (3, None, None),
             "trend 170d -> 2x": (2, 170, None), "trend 170d -> 3x": (3, 170, None),
             "trend 170d -> 2x capped 30% vol": (2, 170, 0.30), "trend 170d -> 3x capped 35% vol": (3, 170, 0.35)}
    out: dict[str, Any] = {}
    for wname, (s, e) in windows.items():
        rows = []
        for name, (lev, n, cap) in tests.items():
            eq, peak, mdd, rets = 1.0, 1.0, 0.0, []
            for i in range(1, len(days)):
                d = days[i]
                if not (s <= d.isoformat() <= e):
                    continue
                tb = TBILL.get(d.year, 0.02) / 252
                r = c[i] / c[i - 1] - 1
                rl = lev * r - (lev - 1) * tb - (0.0095 / 252 if lev > 1 else 0)
                w = 1.0
                if n:
                    w = 1.0 if i - 1 >= n and c[i - 1] > sum(c[i - n:i]) / n else 0.0
                if cap and w and i > 21:
                    v = statistics.pstdev([c[k] / c[k - 1] - 1 for k in range(i - 20, i)]) * math.sqrt(252) * lev
                    w = min(1.0, cap / v) if v else 1.0
                dr = w * rl + (1 - w) * tb
                eq *= 1 + dr
                rets.append(dr)
                peak = max(peak, eq)
                mdd = min(mdd, eq / peak - 1)
            yrs = len(rets) / 252
            sd = statistics.pstdev(rets)
            rows.append({"strategy": name, "cagr": eq ** (1 / yrs) - 1, "max_dd": mdd,
                         "sharpe": statistics.mean(rets) / sd * math.sqrt(252) if sd else None, "growth_of_5000": 5000 * eq})
        out[wname] = rows
    return out
