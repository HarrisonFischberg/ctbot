"""Strategy factory: a large grid, point-in-time filters, and gates that reject luck but keep a real edge."""

import numpy as np
import pandas as pd
import pytest

from ctbot.library import backtest as lbt
from ctbot.library import factory as fy
from ctbot.library import signals as sg


def _panel(n, syms, start="2015-01-01", seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    out = {}
    for s in syms:
        c = 50 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, n)))
        o = c * (1 + rng.normal(0, 0.004, n))
        h, lo = np.maximum(o, c) * 1.005, np.minimum(o, c) * 0.995
        out[s] = pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": rng.uniform(1e6, 3e6, n)}, idx)
    return out


def test_grid_has_hundreds_of_distinct_candidates():
    names = [c["name"] for c in fy.candidates()]
    assert len(names) == len(set(names)) == 864


@pytest.mark.parametrize("filt", [f for f in fy.FILTERS if fy.FILTERS[f]])
def test_filters_only_use_the_past(filt):
    p = _panel(700, ("AAA", "SPY"))
    full = fy.FILTERS[filt](p["AAA"], p["SPY"]).astype(bool)
    for cut in (400, 699):
        part = fy.FILTERS[filt](p["AAA"].iloc[:cut], p["SPY"].iloc[:cut]).astype(bool)
        assert (part == full.iloc[:cut]).all()


def test_cost_multiplier_costs_more():
    p = _panel(400, ("AAA", "SPY"))
    fam = sg.Family("t", "t", "t", 5, lambda d, spy: d["close"] < d["close"].shift(), "test")
    base, _ = lbt.simulate({"AAA": p["AAA"]}, p["SPY"], fam, etf=True)
    dear, _ = lbt.simulate({"AAA": p["AAA"]}, p["SPY"], fam, etf=True, cost_mult=3)
    assert (1 + dear).prod() < (1 + base).prod()


def test_luck_is_rejected_and_a_real_edge_passes(monkeypatch):
    p = _panel(6400, ("AAA", "BBB", "CCC", "SPY"), start="2000-01-03", seed=11)
    noise = np.random.default_rng(5)
    monkeypatch.setattr(sg, "FAMILIES", {
        # peeks at the next week: an edge no honest rule has, so the gates must let it through
        "oracle": sg.Family("oracle", "o", "o", 5, lambda d, spy: d["close"].shift(-5) > d["open"].shift(-1) * 1.04, "test"),
        "coin": sg.Family("coin", "c", "c", 5, lambda d, spy: pd.Series(noise.random(len(d)) < 0.05, d.index), "test"),
    })
    cands = [c for c in fy.candidates() if c["universe"] == "etfs" and c["filter"] == "any"]
    rep = fy.run({"etfs": {s: p[s] for s in ("AAA", "BBB", "CCC")}}, p["SPY"], cands)
    assert rep["n_candidates"] == 18 and rep["pbo"] is not None
    assert rep["passed"] and all(n.startswith("fx_oracle_") for n in rep["passed"])
    coin = [r for n, r in rep["candidates"].items() if n.startswith("fx_coin_")]
    assert not any(r["passed"] for r in coin)
    assert all("survives_3x_costs" in rep["candidates"][n]["checks"] for n in rep["passed"])
    # the book trades one oracle version per rule and universe, and only if it made money 2023-now
    assert len(rep["live"]) == 1 and rep["live"][0] in rep["passed"]
    assert rep["candidates"][rep["live"][0]]["holdout"]["cagr"] > 0
