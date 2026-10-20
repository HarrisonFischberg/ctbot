"""Ranking-model research: point-in-time factors, purged walk-forward, fills, selection stats.

All data here is synthetic. `synthetic_panel(signal=...)` plants a slowly drifting per-stock
drift that trailing returns can detect; with signal=0 the prices are pure noise.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ctbot.research import factors as fx
from ctbot.research import ranker as rk
from ctbot.research.registry import HoldoutLocked, Registry
from ctbot.research.validation import benjamini_hochberg, pbo


def synthetic_panel(n: int = 30, days: int = 1500, signal: float = 0.0008, seed: int = 7) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2012-01-02", periods=days)
    alpha = np.zeros(n)
    panel = {}
    rets = np.zeros((days, n))
    for t in range(days):
        alpha = 0.995 * alpha + rng.normal(0, 1.0, n) * np.sqrt(1 - 0.995 ** 2)
        rets[t] = signal * alpha + rng.normal(0.0003, 0.015, n)
    mkt = rng.normal(0.0004, 0.01, days)
    for j in range(n + 1):
        r = mkt if j == n else rets[:, j] + mkt * 0.5
        close = 50 * np.exp(np.cumsum(r))
        gap = rng.normal(0, 0.002, days)
        opn = np.r_[close[0], close[:-1]] * np.exp(gap)
        hi = np.maximum(opn, close) * (1 + np.abs(rng.normal(0, 0.004, days)))
        lo = np.minimum(opn, close) * (1 - np.abs(rng.normal(0, 0.004, days)))
        name = "SPY" if j == n else f"S{j:02d}"
        panel[name] = pd.DataFrame({"open": opn, "high": hi, "low": lo, "close": close,
                                    "volume": np.full(days, 1e6)}, index=idx)
    return panel


SMALL = rk.RankerParams(train_days=300, retrain_every=21, top_k=5, horizon=5, rebalance_every=5, min_adv=1e6)


def test_factors_are_point_in_time():
    panel = synthetic_panel(n=8, days=400)
    cut = panel["SPY"].index[300]
    full = fx.compute(panel)
    trunc = fx.compute({s: df[df.index <= cut] for s, df in panel.items()})
    for name in full:
        a, b = full[name].loc[cut], trunc[name].loc[cut]
        pd.testing.assert_series_equal(a, b, check_names=False, obj=name)


def test_labels_start_at_next_open():
    panel = synthetic_panel(n=6, days=300)
    a = rk.prepare(panel, SMALL)
    raw = pd.DataFrame(a.open).shift(-1 - 5) / pd.DataFrame(a.open).shift(-1) - 1
    y = rk.labels(a, 5)
    t = 100
    expected = (raw.iloc[t].where(a.eligible[t]).rank(pct=True) - 0.5).to_numpy()
    np.testing.assert_allclose(y[t], expected)
    assert np.isnan(y[-6:]).all()  # the last h+1 rows are not known yet


def test_walk_forward_is_purged():
    a = rk.prepare(synthetic_panel(n=10, days=900), SMALL)
    _, fits = rk.walk_forward_scores(a, SMALL)
    assert fits
    pos = {d.date().isoformat(): k for k, d in enumerate(a.dates)}
    for f in fits:
        assert pos[f["used_from"]] - pos[f["through"]] >= SMALL.horizon + 1


def test_scores_do_not_change_when_the_future_changes():
    panel = synthetic_panel(n=10, days=900)
    a = rk.prepare(panel, SMALL)
    s1, _ = rk.walk_forward_scores(a, SMALL)
    cut = 800
    altered = {k: df.copy() for k, df in panel.items()}
    for df in altered.values():
        df.iloc[cut + 1:, :4] *= 1.5  # rewrite every price after the cut
    s2, _ = rk.walk_forward_scores(rk.prepare(altered, SMALL), SMALL)
    np.testing.assert_allclose(np.nan_to_num(s1[: cut + 1]), np.nan_to_num(s2[: cut + 1]))


def test_planted_signal_is_found_and_noise_is_not():
    for signal, expect_edge in ((0.0015, True), (0.0, False)):
        a = rk.prepare(synthetic_panel(n=30, days=1500, signal=signal), SMALL)
        scores, _ = rk.walk_forward_scores(a, SMALL)
        ic = rk.information_coefficient(a, scores, 5, "2000-01-01", "2100-01-01")["ic_mean"]
        if expect_edge:
            assert ic > 0.03
        else:
            assert abs(ic) < 0.03


def test_fills_happen_at_the_next_open():
    panel = synthetic_panel(n=4, days=300)
    a = rk.prepare(panel, SMALL)
    scores = np.full(a.open.shape, np.nan)
    j = a.symbols.index("S01")
    scores[200, j] = 1.0
    p = rk.RankerParams(top_k=1, n_drop=1, rebalance_every=1)
    r = rk.simulate(a, p, scores, start_idx=199)
    k200 = r.dates.index(a.dates[200].date())
    assert r.exposure[k200] == 0.0           # decided at day 200's close: nothing held that day
    assert r.exposure[k200 + 1] > 0.99      # bought at day 201's open
    r2 = rk.simulate(a, p, np.where(np.isnan(scores), np.nan, 0.0) * 0, start_idx=199)
    assert r2.traded >= 0


def test_topk_dropout_replaces_at_most_n_drop():
    s = np.array([0.9, 0.8, 0.7, 0.6, 0.5, 0.1, 0.05])
    assert rk.topk_dropout(s, set(), 3, 1) == {0, 1, 2}
    held = {4, 5, 6}  # all held names now rank badly
    out = rk.topk_dropout(s, held, 3, 1)
    assert len(out) == 3 and len(out - held) == 1 and 0 in out
    s2 = s.copy()
    s2[6] = np.nan  # a held name lost its data: always sold
    assert 6 not in rk.topk_dropout(s2, held, 3, 1)


def test_experiment_runs_end_to_end(tmp_path):
    panel = synthetic_panel(n=20, days=1300, signal=0.0015)
    base = rk.RankerParams(train_days=250, retrain_every=21, min_adv=1e6)
    reg = Registry(tmp_path / "trials.jsonl")
    old = rk.SPLITS
    rk.SPLITS = {"dev": ("2000-01-01", "2014-12-31"), "validation": ("2015-01-01", "2015-12-31"),
                 "holdout": ("2016-01-01", "2100-01-01")}
    try:
        rep = rk.run(panel, sorted(panel), base, include_holdout=False, registry=reg)
    finally:
        rk.SPLITS = old
    assert "holdout" not in rep["splits"]
    res = rep["results"]
    assert res["baseline: SPY buy & hold"]["dev"]["exposure"] > 0.95
    assert "pbo" in rep["selection"] and 0 <= rep["selection"]["pbo"]["pbo"] <= 1
    assert reg.verify() and reg.trial_count("ranker-2026-10") == len(res)
    assert "PBO" in rk.format_report(rep)


def test_registry_chain_and_holdout_lock(tmp_path):
    reg = Registry(tmp_path / "t.jsonl")
    kw = dict(experiment="e", strategy="s", params={"k": 3}, periods={}, metrics={"sharpe": 1.0},
              data_fingerprint="x")
    reg.record(split="dev", **kw)
    reg.record(split="holdout", **kw)
    with pytest.raises(HoldoutLocked):
        reg.record(split="holdout", **kw)
    rec = reg.record(split="holdout", force_holdout=True, **kw)
    assert rec["holdout_rerun"] is True
    assert reg.verify()
    lines = reg.path.read_text().splitlines()
    reg.path.write_text("\n".join([lines[0].replace('"sharpe": 1.0', '"sharpe": 2.0'), *lines[1:]]) + "\n")
    assert not reg.verify()


def test_pbo_and_fdr():
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 0.01, (1000, 12))
    assert pbo(noise)["pbo"] > 0.2               # choosing among noise is luck
    skilled = noise.copy()
    skilled[:, 0] += 0.003
    assert pbo(skilled)["pbo"] < 0.05
    keep = benjamini_hochberg({"a": 0.001, "b": 0.02, "c": 0.4, "d": 0.9}, q=0.10)
    assert keep == {"a": True, "b": True, "c": False, "d": False}


def test_lab_lag_delays_execution_by_one_close():
    from datetime import date as d

    from ctbot.research import lab as lb

    lab = lb.Lab.__new__(lb.Lab)
    lab.days = [d(2020, 1, 1) + pd.Timedelta(days=k) for k in range(260)]
    lab.days = [x.date() if hasattr(x, "date") else x for x in lab.days]
    px = [100.0] * 231 + [110.0] * 29          # +10% jump on day 231
    lab.px = {"SPY": px}
    lab.state = {}

    def buy_on_jump_day(lab_, i):              # decides on day 230's close, before the jump
        return {"SPY": 1.0} if i >= 230 else {}

    same = lb.simulate(lab, buy_on_jump_day, lab.days[0], lab.days[-1], lag=0)
    late = lb.simulate(lab, buy_on_jump_day, lab.days[0], lab.days[-1], lag=1)
    assert same.final > 5450      # filled at day 230's close (100): caught the jump
    assert late.final < 5000.0    # filled at day 231's close (110): costs only


def test_shadow_strategy_matches_research_scoring(tmp_path):
    import json
    from datetime import datetime
    from datetime import time as dtime

    from ctbot.market_hours import ET
    from ctbot.models import Bar, Direction, Quote
    from ctbot.strategies.base import StrategyContext
    from ctbot.strategies.ranker_signal import RankerSignal

    panel = synthetic_panel(n=12, days=800, signal=0.0015)
    p = rk.RankerParams(train_days=300, min_adv=1e6, top_k=3)
    model = rk.fit_final(rk.prepare(panel, p), p)
    path = tmp_path / "model.json"
    path.write_text(json.dumps(model))

    class MD:
        def daily_bars(self, sym, n):
            df = panel[sym]
            return [Bar(sym, datetime.combine(d.date(), dtime(16), tzinfo=ET), *row) for d, row in
                    zip(df.index[-n:], df[list(fx.FIELDS)].to_numpy()[-n:])]

        def quotes(self, syms):
            return {s: Quote(s, 10.0, 10.01, 10.0) for s in syms}

    strat = RankerSignal(path, max_model_age_days=10_000)
    # first trading day after the panel ends that is a rebalance day
    day = pd.bdate_range(panel["SPY"].index[-1] + pd.Timedelta(days=1), periods=10)
    day = next(d.date() for d in day if strat.rebalance_day(d.date(), p.rebalance_every))
    now = datetime.combine(day, dtime(9, 40), tzinfo=ET)
    ctx = StrategyContext(MD(), tuple(panel), (), day, frozenset(), now)
    sigs = strat.generate(ctx)
    longs = [s for s in sigs if s.direction is Direction.LONG]
    assert len(longs) == 3 and all(s.evidence["systematic"] for s in longs)
    assert all(s.evidence["stop"] == 8.5 for s in longs)
    assert strat.generate(ctx) == []  # once per day
    # research scores for the same (last completed) day pick the same names
    a = rk.prepare(panel, p)
    x = np.nan_to_num(a.features[-1]) @ np.array(model["weights"])
    x[~a.eligible[-1]] = np.nan
    x[a.symbols.index("SPY")] = np.nan
    top = {a.symbols[j] for j in rk.topk_dropout(x, set(), 3, 1)}
    assert {s.symbol for s in longs} == top
    # outside the morning window, nothing
    late = StrategyContext(MD(), tuple(panel), (), day, frozenset(), datetime.combine(day, dtime(15, 0), tzinfo=ET))
    assert RankerSignal(path, max_model_age_days=10_000).generate(late) == []
    assert RankerSignal(tmp_path / "missing.json").generate(ctx) == []
