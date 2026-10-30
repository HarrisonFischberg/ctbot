"""Turbulence switch (Kritzman & Li): flags a joint shock, uses only the past, and goes to cash."""

from datetime import date, timedelta

import numpy as np

from ctbot.research import lab as L


def _lab(n=700, shock=600, seed=1):
    rng = np.random.default_rng(seed)
    lab = L.Lab.__new__(L.Lab)
    lab.days = [date(2015, 1, 1) + timedelta(days=k) for k in range(n)]
    lab.state, lab.cache = {}, {}
    r = rng.normal(0, 0.01, (n, len(L.SECTORS)))
    r[shock] = -0.08  # every sector falls 8% together
    lab.px = {s: list(100 * np.exp(np.cumsum(r[:, j]))) for j, s in enumerate(L.SECTORS)}
    return lab


def test_shock_day_is_the_most_turbulent():
    lab = _lab()
    t = {k: L.turbulence(lab, k, L.SECTORS) for k in range(500, 700)}
    assert max(t, key=t.get) == 600
    assert L.turbulence(lab, 100, L.SECTORS) is None  # under a year of history


def test_switch_goes_to_cash_on_the_shock_and_only_uses_the_past():
    lab = _lab()
    f = L.turbulence_off(lambda lab, i: {"SPY": 1.0})
    assert f(lab, 600) == {} and f(lab, 599) == {"SPY": 1.0}
    flags = lab.cache[next(iter(lab.cache))]
    short = _lab(n=650)
    L.turbulence_off(lambda lab, i: {})(short, 0)
    assert short.cache[next(iter(short.cache))] == flags[:650]
