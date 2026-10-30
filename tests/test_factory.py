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
    # 9 exit variants of each rule are one trial: only each group's best variant can pass
    assert rep["gates"]["groups"] == 2 and len(rep["passed"]) == 1 and rep["passed"][0].startswith("fx_oracle_")
    coin = [r for n, r in rep["candidates"].items() if n.startswith("fx_coin_")]
    assert not any(r["passed"] for r in coin)
    assert all("survives_3x_costs" in rep["candidates"][n]["checks"] for n in rep["passed"])
    # the book trades one oracle version per rule and universe, and only if it made money 2023-now
    assert len(rep["live"]) == 1 and rep["live"][0] in rep["passed"]
    assert rep["candidates"][rep["live"][0]]["holdout"]["cagr"] > 0
    # a real edge works at the neighbouring exit settings too
    assert rep["candidates"][rep["passed"][0]]["plateau"] >= 0.8


def test_plateau_counts_only_neighbouring_exit_settings():
    rows = {c["name"]: c for c in fy.candidates() if c["family"] == "high52" and c["filter"] == "any"
            and c["universe"] == "etfs"}
    grid = sorted(rows.values(), key=lambda r: (r["giveback"], r["max_hold"]))  # 3 x 3
    corner, centre = grid[0], grid[4]
    sr = {n: -1.0 for n in rows} | {grid[8]["name"]: 1.0}  # only the far corner is profitable
    assert fy._plateau(corner, rows, sr) == 0.0  # 3 neighbours, the far corner is not one of them
    assert fy._plateau(centre, rows, sr) == round(1 / 8, 2)


def test_backfill_fetches_only_short_histories(tmp_path, monkeypatch):
    from ctbot.market_data import robinhood_mcp_client
    from ctbot.models import Bar
    from ctbot.research import bars

    monkeypatch.setattr(bars, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(bars.load, "__defaults__", (tmp_path,))
    monkeypatch.setattr(bars.save, "__defaults__", (tmp_path,))
    idx = pd.bdate_range("2000-01-03", periods=fy.MIN_HISTORY)
    bars.save("day", "DEEP", [Bar("DEEP", t.to_pydatetime(), 1, 1, 1, 1, 1) for t in idx])
    fetched = []
    monkeypatch.setattr(bars, "fetch", lambda syms, *a: fetched.extend(syms))
    monkeypatch.setattr(robinhood_mcp_client, "ReadOnlyRobinhoodClient", lambda **k: type("C", (), {"call_many": None})())
    assert fy.backfill(["DEEP", "NEW", "NEW"]) == ["NEW"] and fetched == ["NEW"]


def test_selection_years_start_at_the_first_position():
    idx = pd.bdate_range("2009-12-28", periods=10)
    r = pd.Series([0.0, 0.0, 0.0, 0.01, 0.0, -0.02, 0.0, 0.0, 0.0, 0.0], idx)
    assert list(fy._selection(r)) == [0.01, 0.0, -0.02, 0.0, 0.0, 0.0, 0.0]
    assert fy._selection(pd.Series(0.0, idx)).eq(0).all()
