"""Strategy library: point-in-time rules, next-open fills, shared exits, live = backtest."""

from datetime import datetime
from datetime import time as dtime

import numpy as np
import pandas as pd
import pytest

from ctbot.library import backtest as lbt
from ctbot.library import signals as sg


def _panel(n=700, syms=("AAA", "BBB", "SPY"), seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n)
    out = {}
    for k, s in enumerate(syms):
        c = 50 * np.exp(np.cumsum(rng.normal(0.0004 * (k + 1), 0.015, n)))
        o = c * (1 + rng.normal(0, 0.004, n))
        h = np.maximum(o, c) * (1 + rng.uniform(0, 0.01, n))
        lo = np.minimum(o, c) * (1 - rng.uniform(0, 0.01, n))
        out[s] = pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": rng.uniform(1e6, 3e6, n)}, idx)
    return out


@pytest.mark.parametrize("key", list(sg.FAMILIES))
def test_rules_only_use_the_past(key):
    p = _panel()
    fam, spy, a = sg.FAMILIES[key], p["SPY"], p["AAA"]
    full = fam.rule(a, spy)
    for cut in (400, 550, 699):
        part = fam.rule(a.iloc[:cut], spy.iloc[:cut])
        assert (part == full.iloc[:cut]).all(), f"{key} changes its past signals when later bars arrive"


def test_fills_at_the_next_open_and_exits_by_the_shared_rules():
    idx = pd.bdate_range("2020-01-01", periods=40)  # 14 days of history for the ATR stop
    flat = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1e6}, idx)
    a = flat.copy()
    a.loc[idx[21], ["open", "high", "low", "close"]] = [101, 120, 100.5, 118]   # day after the signal
    a.loc[idx[22], ["open", "high", "low", "close"]] = [118, 119, 104, 105]     # falls through the trail
    fam = sg.Family("t", "t", "t", 30, lambda d, spy: pd.Series(d.index == idx[20], index=d.index), "test")
    r, trades = lbt.simulate({"AAA": a}, flat, fam, etf=True)
    (t,) = trades
    assert t["opened"] == str(idx[21].date()) and t["entry"] == 101  # signal on day 20's close, bought day 21's open
    # trail raised after day 21 to 120 x 0.92 = 110.4; day 22's low 104 crosses it
    assert t["exit"] == pytest.approx(110.4) and t["closed"] == str(idx[22].date())


def test_longest_hold_sells_at_the_close_of_its_last_day():
    idx = pd.bdate_range("2020-01-01", periods=40)
    df = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1e6}, idx)
    fam = sg.Family("t", "t", "t", 3, lambda d, spy: pd.Series(d.index == idx[20], index=d.index), "test")
    _, (t,) = lbt.simulate({"AAA": df}, df, fam, etf=True)
    assert (t["opened"], t["closed"], t["days"]) == (str(idx[21].date()), str(idx[24].date()), 3)  # bought day 21, live exit_at = 3 sessions later


def test_live_rule_matches_the_backtest_signal():
    from ctbot.market_hours import ET
    from ctbot.models import Bar, Direction, Quote
    from ctbot.strategies.base import StrategyContext
    from ctbot.strategies.rule import RuleStrategy

    p = _panel(n=700, syms=("SPY", "QQQ", "IWM"))
    fam = sg.FAMILIES["three_down"]
    sigs = {s: fam.rule(df, p["SPY"]) for s, df in p.items()}
    day_i = next(i for i in range(400, 699) if any(v.iloc[i] for v in sigs.values()))
    today = p["SPY"].index[day_i + 1].date()

    class MD:
        def daily_bars(self, sym, n):
            df = p[sym].iloc[: day_i + 2]  # includes today's bar: the strategy must ignore it
            return [Bar(sym, datetime.combine(d.date(), dtime(16), tzinfo=ET), *row)
                    for d, row in zip(df.index[-n:], df.to_numpy()[-n:])]

        def quotes(self, syms):
            return {s: Quote(s, 10.0, 10.01, 10.0) for s in syms}

    strat = RuleStrategy("three_down", "etfs")
    ctx = StrategyContext(MD(), tuple(p), (), today, frozenset(), datetime.combine(today, dtime(9, 40), tzinfo=ET))
    got = {s.symbol for s in strat.generate(ctx) if s.direction is Direction.LONG}
    assert got == {s for s, v in sigs.items() if v.iloc[day_i]}
    assert strat.generate(ctx) == []  # once a day


def test_library_entries_are_configured_watch_only(tmp_path):
    from ctbot.strategies.registry import build_strategies, load_strategy_config

    f = tmp_path / "s.toml"
    f.write_text('[trend_core]\nenabled = true\n')
    cfg = load_strategy_config(f)
    lib = [n for n in cfg if n.startswith("lib_")]
    assert len(lib) == len(sg.FAMILIES) * len(sg.UNIVERSES)
    ws = {w.strategy.name: w for w in build_strategies(cfg)}
    assert ws["lib_rsi2_etfs"].mode == "shadow" and ws["lib_rsi2_etfs"].standalone
    f.write_text('[library]\nenabled = false\n[trend_core]\nenabled = true\n')
    assert not [n for n in load_strategy_config(f) if n.startswith("lib_")]


def test_full_run_on_synthetic_prices_admits_nothing_from_noise(monkeypatch):
    from ctbot.research import factors as fx

    syms = sg.UNIVERSES["etfs"] + ["AAPL", "MSFT", "NVDA", "AMZN", "JPM", "XOM"]
    rng = np.random.default_rng(11)
    idx = pd.bdate_range("2000-01-03", "2023-06-30")
    data = {}
    for s in syms:
        c = 50 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, len(idx))))
        o = c * (1 + rng.normal(0, 0.003, len(idx)))
        data[s] = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.004, "low": np.minimum(o, c) * 0.996,
                                "close": c, "volume": 2e6}, idx)
    monkeypatch.setattr(fx, "load_panel", lambda symbols, root=None: {s: data[s] for s in symbols if s in data})
    rep = lbt.run(holdout=False, registry=None)
    assert rep["n_trials"] == 24 and set(rep["entries"]["lib_rsi2_etfs"]) >= {"dev", "validation", "checks"}
    assert len(rep["admitted"]) <= 2  # pure noise: at most a stray false positive at q=10%
    ok, why = lbt.reason(rep, "lib_rsi2_etfs")
    assert why.startswith("passed" if ok else "failed")
